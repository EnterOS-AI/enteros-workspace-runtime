"""Shared base A2A executor for SUBPROCESS / one-shot workspace runtimes.

WHY THIS EXISTS (tenant-agent BUG 3 meta-cause)
===============================================
Every subprocess-backed runtime adapter (openclaw, codex, …) used to
REIMPLEMENT ``AgentExecutor.execute()`` in its own template repo, and each one
silently diverged from the session CONTRACT that the platform relies on:

  * it keyed the native session on the per-request ``context_id``, which the
    a2a-sdk mints FRESH every turn when the canvas does not thread one — so the
    runtime's own SessionManager opened a NEW session on every message and the
    agent re-greeted ("fresh session, clean slate") every turn.

Fixing this per-runtime is the wrong altitude: the next new runtime would drop
the session contract again. This base moves the contract to the SHARED SSOT SDK
so EVERY subprocess runtime INHERITS one enforced behavior:

  STABLE SESSION ID — ``derive_session_id`` keys on the WORKSPACE IDENTITY
  (``WORKSPACE_ID``), NOT the per-request ``context_id``, so the runtime's native
  SessionManager RESUMES the same session across turns. ``context_id`` / ``task_id``
  remain only as a fallback for the (rare) no-WORKSPACE_ID case.

CONTINUITY IS THE RUNTIME'S OWN NATIVE SESSION (resumed via that stable id) — NOT
a force-injected transcript. The base passes ONLY the current user message to
``run_agent``; it deliberately does NOT prepend ``metadata.history`` to the task
text. Direct history injection was the previous approach and has been removed: it
double-fed context (native session + injected transcript), grew the prompt
unboundedly, and fought the runtime's own memory. Older/other history is retrieved
only when the agent CHOOSES to (e.g. by calling a platform-workspace MCP tool that
reads the persisted activity store) — never shoved into every task text.

Subclasses implement ONLY ``run_agent(task_text, session_id, context)`` — the
actual shell-out — and MAY override ``_decorate_message`` for per-runtime message
adornments (e.g. openclaw's ``MEDIA:`` lines). They MUST NOT override
``execute()``; the session contract lives here and is guarded by
``tests/test_subprocess_executor_contract.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from a2a.server.agent_execution import AgentExecutor

from molecule_runtime import turn_lease
from molecule_runtime.executor_helpers import extract_attached_files
from molecule_runtime.platform_auth import get_workspace_id as _get_workspace_id
from molecule_runtime.shared_runtime import (
    brief_task,
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
    """Base executor that ENFORCES a stable, workspace-keyed session id.

    Continuity is the runtime's own native session (resumed via that stable id);
    the base passes ONLY the current user message to ``run_agent`` and does NOT
    force-inject conversation history. A subprocess/one-shot runtime adapter
    subclasses this and implements only ``run_agent``. It inherits ``execute()``
    — do not override it.
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

        # THE CONTRACT — a STABLE session id (workspace-keyed, not per-request) so
        # the runtime's native SessionManager RESUMES the prior session. Continuity
        # is the runtime's OWN memory keyed on this id; conversation history is NOT
        # force-injected into the task text (that double-fed context and fought the
        # native session). Only the current user message is passed through.
        session_id = self.derive_session_id(context)
        task_text = user_message

        await set_current_task(self._heartbeat, brief_task(user_message))
        reply: str | None = None
        # THE TURN-LEASE CONTRACT (runtime#408). A subprocess runtime spends its
        # whole turn blocked inside run_agent and never enters the native
        # executor's astream loop, so nothing here used to arm or feed the
        # process-global lease: `turn_liveness_snapshot()` reported CONTAINER
        # UPTIME as the age of every turn, on every subprocess flavour, forever.
        # Entering the scope exports MOLECULE_TOOL_ACTIVITY_FILE (without which a
        # subprocess runtime's own tool-activity ping is a no-op) BEFORE
        # run_agent spawns the child, arms the lease for this turn if this
        # runtime has a demonstrated feed, and runs the activity watcher for the
        # turn's duration. It lives HERE, in the shared base, for the same
        # reason derive_session_id does: a per-adapter reimplementation is what
        # let the contract rot in the first place.
        async with turn_lease.turn_liveness_scope():
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
        """Hook: adorn the current user message. Default no-op."""
        return user_message

    async def run_agent(self, task_text: str, session_id: str, context) -> str:
        """Invoke the underlying runtime. Subclasses MUST implement this.

        Args:
            task_text: the CURRENT user message (already decorated / image-enriched).
                NO conversation history is prepended — pass it straight to the
                runtime. Continuity comes from the native session resumed via
                ``session_id``, not from injecting a transcript here.
            session_id: the STABLE, workspace-keyed session id — pass to the
                runtime's native session flag (e.g. ``--session-id``) so it RESUMES
                the same conversation across turns.
            context: the raw A2A RequestContext, for any extra needs.

        Returns:
            The agent's reply text.
        """
        raise NotImplementedError(
            "SubprocessA2AExecutor subclasses must implement run_agent(task_text, session_id, context)"
        )
