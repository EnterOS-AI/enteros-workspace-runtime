"""Shared base A2A executor for SUBPROCESS / one-shot workspace runtimes.

WHY THIS EXISTS (tenant-agent BUG 3 meta-cause)
===============================================
Every subprocess-backed runtime adapter (openclaw, codex, google-adk, …) used to
REIMPLEMENT ``AgentExecutor.execute()`` in its own template repo, and each one
silently diverged from the session+history CONTRACT that the platform relies on:

  * the openclaw executor called ``extract_message_text`` ONLY and never injected
    ``metadata.history`` — so a 2nd turn arrived with no prior context and the
    agent re-greeted ("fresh session, clean slate"); and
  * it keyed the native session on the per-request ``context_id``, which the
    a2a-sdk mints FRESH every turn when the canvas does not thread one — so the
    runtime's own SessionManager opened a NEW session on every message.

Fixing this per-runtime is the wrong altitude: the next new runtime would drop
history/session again. This base moves the contract to the SHARED SSOT SDK so
EVERY subprocess runtime INHERITS one enforced behavior:

  1. HISTORY INJECTION — ``build_task_text(user_message, extract_history(context))``
     prepends the conversation transcript to the task text before the runtime is
     invoked, so continuity survives even when the runtime has no durable session.
  2. STABLE SESSION ID — ``derive_session_id`` keys on the WORKSPACE IDENTITY
     (``WORKSPACE_ID``), NOT the per-request ``context_id``, so the runtime's
     native SessionManager RESUMES the same session across turns. ``context_id``
     / ``task_id`` remain only as a fallback for the (rare) no-WORKSPACE_ID case.

Subclasses implement ONLY ``run_agent(task_text, session_id, context)`` — the
actual shell-out — and MAY override ``_decorate_message`` for per-runtime message
adornments (e.g. openclaw's ``MEDIA:`` lines). They MUST NOT override
``execute()``; the history+session contract lives here and is guarded by
``tests/test_subprocess_executor_contract.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from a2a.server.agent_execution import AgentExecutor

from molecule_runtime.executor_helpers import extract_attached_files
from molecule_runtime.platform_auth import get_workspace_id as _get_workspace_id
from molecule_runtime.shared_runtime import (
    brief_task,
    build_task_text,
    extract_history,
    extract_message_text,
    set_current_task,
)

logger = logging.getLogger(__name__)

# Optional vision enrichment: describe image attachments in-line so a text-only
# subprocess runtime still "sees" them. Degrade gracefully when the extra is not
# installed (mirrors the openclaw adapter's own guarded import).
try:  # pragma: no cover - trivial import guard
    from molecule_runtime.attachment_vision import append_image_descriptions
except Exception:  # pragma: no cover

    async def append_image_descriptions(text: str, files) -> str:  # type: ignore
        return text


def _resolve_workspace_id() -> str:
    """Return the validated WORKSPACE_ID env identity, or "" when unset/invalid.

    Unlike the OTEL span attribute (which uses an "unknown" sentinel), the empty
    string here is meaningful: it signals derive_session_id to fall back to the
    request-scoped ids rather than key every session on the literal "unknown".
    """
    try:
        wid = _get_workspace_id()
    except Exception:
        return ""
    wid = (wid or "").strip()
    return "" if wid == "unknown" else wid


class SubprocessA2AExecutor(AgentExecutor):
    """Base executor that ENFORCES history-injection + a stable session id.

    A subprocess/one-shot runtime adapter subclasses this and implements only
    ``run_agent``. It inherits ``execute()`` — do not override it.
    """

    # Human-readable label used in error/empty-output fallbacks. Override in the
    # subclass (e.g. "OpenClaw").
    runtime_label: str = "Agent"

    def __init__(self, *, workspace_id: str = "", heartbeat: Any = None) -> None:
        # WORKSPACE_ID-first stable identity. An explicit constructor value
        # (from AdapterConfig.workspace_id) wins; otherwise read the env.
        self._workspace_id = (workspace_id or "").strip() or _resolve_workspace_id()
        self._heartbeat = heartbeat

    # ------------------------------------------------------------------ #
    # The enforced contract. Subclasses MUST NOT override this method.
    # ------------------------------------------------------------------ #
    async def execute(self, context, event_queue) -> None:
        from a2a.helpers import new_text_message

        user_message = extract_message_text(context)
        attached = extract_attached_files(getattr(context, "message", None))
        if attached:
            user_message = await append_image_descriptions(user_message, attached)
        # Per-runtime message adornments (e.g. openclaw MEDIA: lines). Kept as a
        # hook so the shared execute() body stays identical across runtimes.
        user_message = self._decorate_message(user_message, attached)

        if not user_message:
            await event_queue.enqueue_event(new_text_message("No message provided"))
            return

        # CONTRACT #1 — inject conversation history so a 2nd turn keeps context.
        history = extract_history(context)
        if history:
            logger.info(
                "%s execute: injecting %d history message(s)",
                self.runtime_label,
                len(history),
            )
        task_text = build_task_text(user_message, history)

        # CONTRACT #2 — a STABLE session id (workspace-keyed, not per-request).
        session_id = self.derive_session_id(context)

        await set_current_task(self._heartbeat, brief_task(user_message))
        reply: str | None = None
        try:
            reply = await self.run_agent(task_text, session_id, context)
        except Exception as e:  # noqa: BLE001 - surface any runtime error as a reply
            logger.exception("%s run_agent failed", self.runtime_label)
            reply = f"{self.runtime_label} error: {e}"
        finally:
            await set_current_task(self._heartbeat, "")

        if not reply:
            reply = f"{self.runtime_label} returned no output"
        await event_queue.enqueue_event(new_text_message(reply))

    async def cancel(self, context, event_queue) -> None:  # pragma: no cover
        # Subprocess runtimes are one-shot; nothing durable to cancel by default.
        pass

    # ------------------------------------------------------------------ #
    # Template methods / hooks for subclasses.
    # ------------------------------------------------------------------ #
    def derive_session_id(self, context) -> str:
        """Return a STABLE conversation/session id for the native runtime.

        WORKSPACE_ID-first (the proven fix): the a2a-sdk mints a fresh
        ``context_id`` per request when the client does not thread one, so keying
        the native session on ``context_id`` opens a new session every turn. The
        workspace identity is stable across turns, so a runtime keyed on it
        resumes the same session. ``context_id`` then ``task_id`` remain fallbacks
        only when no WORKSPACE_ID is available (legacy/local); these are the two
        RequestContext id fields, tried in cross-turn-stability order.
        """
        if self._workspace_id:
            return f"workspace:{self._workspace_id}"
        for attr in ("context_id", "task_id"):
            v = getattr(context, attr, None)
            if v:
                return str(v)
        return "default"

    def _decorate_message(self, user_message: str, attached: list) -> str:
        """Hook: adorn the user message before history is prepended. Default no-op."""
        return user_message

    async def run_agent(self, task_text: str, session_id: str, context) -> str:
        """Invoke the underlying runtime. Subclasses MUST implement this.

        Args:
            task_text: the current user message with conversation history already
                prepended (build_task_text) — pass this straight to the runtime.
            session_id: the STABLE, workspace-keyed session id — pass to the
                runtime's native session flag (e.g. ``--session-id``).
            context: the raw A2A RequestContext, for any extra needs.

        Returns:
            The agent's reply text.
        """
        raise NotImplementedError(
            "SubprocessA2AExecutor subclasses must implement run_agent(task_text, session_id, context)"
        )
