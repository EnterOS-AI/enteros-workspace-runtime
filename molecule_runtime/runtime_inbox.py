"""Per-context_id non-blocking inbox for A2A inbound messages.

Task #378 — make the A2A POST handler non-blocking so user input is ALWAYS
received even while the agent is mid-turn.

Empirical motivation (diagnostic agent a0a39be, 2026-05-20):
PM workspace (workspace ``deedcb61``) had ``molecule-runtime`` listening on
:8000 with the A2A POST handler serialised on the live SDK turn. While the
agent was running ``bash -c 'sleep 600 && echo …'``, the next inbound POST
queued and the tenant workspace-server timed out at 180s. First turn lands,
second times out.

Design
======
Per-context inbox state object holds:

* ``pending_messages`` — ``asyncio.Queue`` of unprocessed user inputs that
  arrived while a turn was in flight for the same ``context_id``. The SDK
  turn loop drains this between LangGraph events.
* ``interrupt_event`` — set when a fresh message arrived. The executor
  checks this between ``astream_events`` iterations and aborts the
  in-flight turn so the new message can be processed.
* ``current_subprocess`` — handle (``asyncio.subprocess.Process`` /
  ``subprocess.Popen``) of any long-running shell tool. On interrupt the
  executor calls ``terminate()`` / ``kill()`` so a ``sleep 600`` doesn't
  block the new turn.
* ``last_accumulated`` — partial reply text built up before interruption,
  used so the next turn can prepend an AI message preserving context.

Upstream alignment
==================
A2A spec — "Task Execution and Interruption":

  > "Agents MUST infer contextId from the task if only taskId is
  > provided … Clients can send additional messages with taskId references
  > to continue or refine existing tasks."

This module is the per-context shared state that bridges a2a-sdk's
per-request execution into a single logical agent turn that can be
interrupted by a follow-on message on the same ``context_id``.

Feature flag
============
``MOLECULE_A2A_NONBLOCKING=true`` opts the executor into the interrupt
path. Default ``false`` for safety during the rollout bake — flip to
default-on in a follow-up PR after 24h on chloe-dong + agents-team.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def is_nonblocking_enabled() -> bool:
    """Return True when MOLECULE_A2A_NONBLOCKING is set to a truthy value."""
    return os.environ.get("MOLECULE_A2A_NONBLOCKING", "").lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class InboxEntry:
    """State for a single ``context_id`` — see module docstring."""

    context_id: str
    pending_messages: asyncio.Queue = field(default_factory=asyncio.Queue)
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_subprocess: Any = None
    last_accumulated: str = ""
    # Wall-clock seconds when this entry was created; used by tests and
    # for the response-time SLA assertion (POST must return <100 ms).
    created_at: float = field(default_factory=time.monotonic)
    # True while a turn is actively running for this context_id. The
    # second POST inspects this to decide whether to enqueue + interrupt
    # (turn in flight) or fall through to the normal blocking path
    # (fresh turn).
    turn_in_flight: bool = False

    def request_interrupt(self, new_message: str) -> None:
        """Queue a new message and signal the running turn to interrupt.

        Idempotent — calling twice in rapid succession just enqueues both
        messages; the running turn drains them in order on its next
        interrupt check.
        """
        # Queue first, THEN set the event — guarantees the interrupted
        # turn always sees at least one pending message when it wakes
        # (no lost-wakeup race between the event-set and the queue-put).
        self.pending_messages.put_nowait(new_message)
        self.interrupt_event.set()

    def consume_pending(self) -> list[str]:
        """Drain all queued messages without blocking. Clears the interrupt event."""
        out: list[str] = []
        while True:
            try:
                out.append(self.pending_messages.get_nowait())
            except asyncio.QueueEmpty:
                break
        self.interrupt_event.clear()
        return out

    def kill_subprocess(self) -> bool:
        """Terminate the registered subprocess if any. Returns True on kill attempt.

        Best-effort: a subprocess that's already exited or doesn't expose
        ``terminate()`` is silently skipped. Both ``subprocess.Popen`` and
        ``asyncio.subprocess.Process`` shapes are accepted.
        """
        proc = self.current_subprocess
        if proc is None:
            return False
        try:
            # asyncio.subprocess.Process exposes returncode as an attribute;
            # subprocess.Popen.poll() returns the same thing.
            if hasattr(proc, "returncode") and proc.returncode is not None:
                return False  # Already exited
            if hasattr(proc, "poll") and callable(proc.poll):
                if proc.poll() is not None:
                    return False
            proc.terminate()
            logger.info(
                "runtime_inbox: terminated subprocess for context_id=%s",
                self.context_id,
            )
            return True
        except (ProcessLookupError, OSError) as exc:
            logger.debug(
                "runtime_inbox: kill_subprocess no-op: %s", exc,
            )
            return False


class RuntimeInbox:
    """Process-wide registry of per-context inbox entries.

    Keyed by ``context_id``. Entries are created lazily on first access
    and persist for the workspace's lifetime so a long-lived chat retains
    its interrupt-state between turns. Memory is bounded by the platform's
    own context_id reuse pattern (one per chat tab) — no per-turn churn.
    """

    def __init__(self) -> None:
        self._entries: dict[str, InboxEntry] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, context_id: str) -> InboxEntry:
        """Atomically fetch (creating if needed) the inbox entry."""
        async with self._lock:
            entry = self._entries.get(context_id)
            if entry is None:
                entry = InboxEntry(context_id=context_id)
                self._entries[context_id] = entry
            return entry

    def peek(self, context_id: str) -> InboxEntry | None:
        """Return the entry without creating one. None if absent."""
        return self._entries.get(context_id)

    def reset_for_tests(self) -> None:
        """Test-only: clear all entries. Production code never calls this."""
        self._entries.clear()


# Module-level singleton — one inbox per process, shared across all
# concurrent A2A requests. Matches the "one workspace = one process"
# topology from reference_workspace_ec2_one_per_workspace_2026_05_19.
_INBOX = RuntimeInbox()


def get_inbox() -> RuntimeInbox:
    """Return the process-wide inbox singleton."""
    return _INBOX
