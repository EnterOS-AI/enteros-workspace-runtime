"""SDK-owned tool-call telemetry primitive (ADR-004 — shared tool-call emit).

The workspace canvas renders a tool-call ONLY from an ``agent_log`` activity
row POSTed to

    POST {PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity

with the payload shape below. molecule-core turns each such row into BOTH the
live MyChat progress line AND the persistent ``ToolTraceChip`` it reconstructs
server-side (core#2636). Wire shape (exactly these six keys — byte-parity with
the claude-code template's ``_report_tool_use`` so core's chip reconstruction
is identical across every runtime)::

    {
      "activity_type": "agent_log",
      "source_id":     WORKSPACE_ID,   # self-action: source == target
      "target_id":     WORKSPACE_ID,
      "summary":       "<emoji> Tool(…)",
      "status":        "ok",
      "method":        "<tool_name>"
    }

Before ADR-004 ONLY the claude-code template emitted these rows (its
``claude_sdk_executor._report_tool_use``); hermes/codex/openclaw emitted
nothing renderable, so their canvas showed a bare spinner with no visible
tool activity. This module generalizes that battle-tested emitter into a
single engine-owned primitive every adapter calls at its tool site, so the
behavior is shared, not per-template. The SDK adapter-socket conformance
suite (adapter-socket.contract.md §8) asserts every executor emits here.

Design contract (mirrors ``boot_step_emit`` — deliberately paranoid):

* **Fire-and-forget + best-effort.** Short (5s) timeout, and EVERY exception
  is swallowed — connection error, timeout, non-2xx, and the RuntimeError
  raised when ``WORKSPACE_ID`` is unset. Emitting a tool-call MUST NEVER
  abort the tool, the turn, or raise into the executor's stream loop. Losing
  the telemetry only loses the canvas chip; the agent runs regardless.

* **Lazy imports inside the try.** ``httpx``, the ``PLATFORM_URL`` /
  ``WORKSPACE_ID`` constants (``a2a_client``), and ``auth_headers``
  (``platform_auth``) are imported INSIDE the guarded body so this module
  imports cleanly even when the a2a plumbing is absent (unit tests, the
  wheel-build static-analysis import). Nothing at module scope touches the
  environment.

* **Direct to /activity only.** This must NOT route through
  ``a2a_tools.report_activity`` — that helper fires a SECOND POST to
  ``/registry/heartbeat`` with ``current_task``, which double-emits as a
  ``TASK_UPDATED`` line in the chat feed. Per-tool telemetry is a chat-only
  signal; the workspace card's ``current_task`` is set once per turn by the
  executor elsewhere.

* **Not concierge-gated.** Unlike ``boot_step_emit`` (concierge-only), tool
  calls emit on EVERY workspace — every agent on every box should render its
  tool activity.

* **Engine-owned free function, not an adapter method.** Adapters IMPORT and
  call ``emit_tool_call`` (the engine never calls back into an adapter),
  exactly like ``boot_step_emit.emit_boot_step`` is a free function rather
  than a ``BaseAdapter`` method.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The generic summary is a one-liner shown in the canvas chip + activity row.
# Cap it defensively the way ``a2a_tools.report_activity`` caps ``summary``
# (``_MAX_SUMMARY_CHARS = 256``) so a giant tool-arg string an adapter passes
# in ``summary=`` can't write a multi-KB activity row.
_MAX_SUMMARY_CHARS = 256

# Short, fixed timeout — claude-code's ``_report_tool_use`` value. Long enough
# to absorb a single platform GC pause, short enough that a wedged platform
# doesn't slow the tool-iteration cadence noticeably.
_TIMEOUT_SECONDS = 5.0


def summarize_tool(name: str, args: dict | None = None) -> str:
    """Generic one-line summary for a tool invocation.

    The ENGINE ships ONLY the generic ``🛠 name(…)`` fallback (U+1F6E0
    hammer-and-wrench + U+2026 ellipsis — the same generic branch
    claude-code's ``_summarize_tool_use`` falls through to). The richer
    per-tool summarizer table (Read→📄, Bash→⚡, Edit→✏️, …) is
    claude-code-specific and STAYS in that template; other adapters either
    pass an explicit ``summary=`` when they have a nicer line, or accept this
    generic fallback.

    ``args`` is accepted so a future richer default CAN inspect the tool's
    arguments, but the v1 default ignores them — keeping the primitive lean
    per the capture-first principle (an adapter that wants argument-aware
    summaries computes its own and passes ``summary=``). Truncated so a tool
    with a giant input dict can't produce a multi-KB summary.
    """
    return f"🛠 {name}(…)"[:_MAX_SUMMARY_CHARS]


async def emit_tool_call(
    name: str,
    summary: str | None = None,
    status: str = "ok",
) -> None:
    """Fire-and-forget one ``agent_log`` tool-call activity row.

    The single SDK-owned primitive every adapter awaits at its tool site so
    the canvas can render a ``ToolTraceChip`` for the invocation (core#2636).
    POSTs the six-key ``agent_log`` payload DIRECTLY to
    ``{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity``.

    Best-effort: the ENTIRE body is wrapped in ``try/except Exception`` and
    NEVER raises — a network blip, a wedged platform, a non-2xx, or the
    ``RuntimeError`` from an unset ``WORKSPACE_ID`` is swallowed silently. The
    tool and the turn proceed regardless; only the telemetry is lost.

    Args:
        name: the tool name — becomes the ``method`` field and (via the
            default summary) the chip legend. A falsy ``name`` no-ops.
        summary: optional pretty one-liner. When ``None`` (the common case)
            the generic ``summarize_tool(name)`` fallback is used. Capped at
            256 chars regardless of source.
        status: activity status, defaults to ``"ok"``. Adapters MAY pass a
            different value (e.g. an error status) at a failed tool site.
    """
    try:
        # Lazy imports INSIDE the guard so the module imports cleanly even
        # when the a2a plumbing / environment isn't set up (unit tests, the
        # publish-runtime wheel-build static import). Importing WORKSPACE_ID
        # triggers a2a_client's PEP-562 resolver, which validates + caches
        # the env var (and raises RuntimeError when unset — swallowed here).
        import httpx

        from molecule_runtime.a2a_client import PLATFORM_URL, WORKSPACE_ID
        from molecule_runtime.platform_auth import auth_headers

        if not name:
            return

        summary = summary or summarize_tool(name, None)
        # Defensive cap — an adapter-supplied summary could be arbitrarily
        # long; the generic fallback is already short.
        summary = summary[:_MAX_SUMMARY_CHARS]

        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            await client.post(
                f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity",
                json={
                    "activity_type": "agent_log",
                    # source == target for self-actions, matching the
                    # convention other self-logged activity rows use so DB
                    # consumers joining on target_id see a defined value.
                    "source_id": WORKSPACE_ID,
                    "target_id": WORKSPACE_ID,
                    "summary": summary,
                    "status": status,
                    "method": name,
                },
                headers=auth_headers(),
            )
        logger.debug("emit_tool_call: sent %s (%s)", name, status)
    except Exception as exc:  # noqa: BLE001 — telemetry MUST never break a turn
        # Connection refused / timeout / non-2xx / unset WORKSPACE_ID / any
        # other failure: swallow. Container stdout may be invisible on prod
        # boxes, so debug-only, never print.
        logger.debug(
            "emit_tool_call: swallowed %s: %s", type(exc).__name__, exc
        )
        return
