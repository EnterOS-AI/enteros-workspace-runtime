"""Turn-lease participation for EVERY adapter executor (runtime#408).

Why a wrapper and not just the base class
=========================================
``SubprocessA2AExecutor`` owns the shared subprocess contract, and issue #408
was filed against it — but it turns out to have exactly ONE subclass. Of the
four "subprocess flavours", only **openclaw** inherits it. The other three each
implement ``AgentExecutor`` directly in their own template repo:

    claude-code -> ClaudeSDKExecutor(AgentExecutor)
    codex       -> CodexAppServerExecutor(AgentExecutor)
    hermes      -> HermesAgentProxyExecutor(AgentExecutor)
    openclaw    -> OpenClawA2AExecutor(SubprocessA2AExecutor)   <- the only one

So fixing the base alone would have fixed one flavour out of four, and the
remaining three would have kept reporting container uptime with no test able to
see it. Liveness is not an adapter's private business; it is a platform
invariant. This wrapper is applied at ``main.py``'s single executor funnel — the
same place Langfuse tracing is attached — so every runtime, present and future,
participates in the lease whether or not its author knew the lease existed.

What this wrapper is NOT
------------------------
It is not a liveness SOURCE. It arms the lease and guarantees the transport
(the exported activity-file path + the watcher); the evidence itself still has
to come from a runtime that reports real work. Deliberately, this wrapper does
NOT touch the lease when the executor enqueues an A2A event: the reply event is
emitted at the END of a turn, so counting it as activity would mean every turn
"proved" it had a liveness feed while proving nothing about the long silent
middle, and would defeat the empirical check in
:func:`turn_lease.arm_turn_if_fed`. Process liveness is not activity, and
neither is finishing.
"""
from __future__ import annotations

import logging
from typing import Any

from molecule_runtime import turn_lease

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard mirrors tracing.py
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutor
except Exception:  # pragma: no cover
    _AgentExecutor = object  # type: ignore[assignment,misc]


class TurnLeaseExecutor(_AgentExecutor):  # type: ignore[misc]
    """Wraps an adapter's executor so its turns participate in the turn lease.

    Observes only — delegates ``execute`` / ``cancel`` (and every other
    attribute) to the inner executor, so an executor that already arms the lease
    itself (openclaw, via the shared base) is unaffected: arming twice at turn
    start is idempotent, and neither arm can move ``_turn_start`` mid-turn,
    which is what would bypass the absolute cap.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def execute(self, context: Any, event_queue: Any):
        async with turn_lease.turn_liveness_scope():
            return await self._inner.execute(context, event_queue)

    async def cancel(self, *args, **kwargs):
        return await self._inner.cancel(*args, **kwargs)

    def __getattr__(self, name: str):
        # Keeps the tracer's duck-typed reads (`_last_tool_uses`, `_last_steps`,
        # …) working through the wrapper.
        return getattr(self._inner, name)


def wrap_executor(executor: Any) -> Any:
    """Wrap ``executor`` so every turn participates in the turn lease.

    Applied UNCONDITIONALLY (unlike the tracing wrap, which is gated on Langfuse
    being configured): a liveness invariant must not depend on whether an
    observability backend happens to be set up. The wrapper is nonetheless free
    when the mailbox kernel is off — :func:`turn_lease.turn_liveness_scope`
    short-circuits to a pass-through with no lease installed.

    Fail-open: any error returns the original executor unwrapped. A liveness
    concern must never be able to stop a workspace from serving turns.
    """
    try:
        if executor is None:
            return executor
        return TurnLeaseExecutor(executor)
    except Exception as e:  # pragma: no cover
        logger.warning("turn-lease wrap_executor failed (returning unwrapped): %s", e)
        return executor
