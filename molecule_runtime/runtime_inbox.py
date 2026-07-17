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
  turn loop drains this between runtime events.
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
``MOLECULE_A2A_NONBLOCKING`` gates the executor's non-blocking fast-path.
**Default-ON as of 2026-06-10** — the rollout bake on chloe-dong +
agents-team completed and PR #112's queue-don't-break path fast-acks the
inbound POST (<100 ms) instead of blocking ~300s behind the live turn.
Set ``MOLECULE_A2A_NONBLOCKING=false`` (or ``0``/``no``/``off``) on a
workspace and restart to roll back to the legacy synchronous handler.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Task #377 — Stop All propagation. The A2A executor stamps the active
# context_id into this contextvar at the start of every turn so any
# subprocess-spawning tool (sandbox, future bash, etc.) can self-register
# its handle on the matching inbox entry. Default empty string means "no
# active context" — tools fall through to a no-op self-registration.
current_context_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "molecule_runtime_current_context_id", default=""
)


def is_nonblocking_enabled() -> bool:
    """Return True unless MOLECULE_A2A_NONBLOCKING is explicitly disabled.

    Default-ON as of 2026-06-10 (the rollout bake on chloe-dong +
    agents-team completed; PR #112 reworked the path from interrupt to
    queue-don't-break and its fast-ack <100 ms SLA is regression-tested in
    tests/test_a2a_nonblocking_inbox.py). Unset / empty / any non-falsey
    value enables the non-blocking fast-path. Only an explicit falsey value
    disables it:

        MOLECULE_A2A_NONBLOCKING=false   # disable (also accepts 0 / no / off)

    Rollback: set MOLECULE_A2A_NONBLOCKING=false on the affected
    workspace(s) and restart. This restores the legacy synchronous POST
    handler with no code change.
    """
    val = os.environ.get("MOLECULE_A2A_NONBLOCKING", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def _inbox_maxsize() -> int:
    """Return the maxsize for per-context pending-messages queues.

    Reads ``MOLECULE_A2A_INBOX_MAXSIZE``.  Default 32.  Invalid or
    non-positive values clamp to the default (never unbounded).
    """
    raw = os.environ.get("MOLECULE_A2A_INBOX_MAXSIZE", "")
    if not raw:
        return 32
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "MOLECULE_A2A_INBOX_MAXSIZE=%r is not an integer; using default 32",
            raw,
        )
        return 32
    if n < 1:
        logger.warning(
            "MOLECULE_A2A_INBOX_MAXSIZE=%d is < 1; clamping to 1", n,
        )
        return 1
    return n


class InboxFull(Exception):
    """Raised when a message cannot be queued because the inbox is at capacity.

    The executor catches this and emits a structured busy response so the
    caller can retry.  Never silently dropped.
    """
    pass


def register_active_subprocess(proc: Any) -> bool:
    """Register ``proc`` as the current_subprocess for the active context_id.

    Returns True if registration succeeded (active context_id present + entry
    exists), False otherwise. Best-effort — a tool that calls this when no
    A2A turn is active (CLI/tests/smoke) simply gets a False and continues.

    Tools using this MUST clear via ``clear_active_subprocess(proc)`` in their
    finally block so a finished tool call doesn't leave a stale handle
    pointing at a freed Process — a subsequent unrelated cancel could
    otherwise SIGTERM whatever recycled that PID.
    """
    cid = current_context_id.get()
    if not cid:
        return False
    entry = _INBOX.peek(cid)
    if entry is None:
        return False
    entry.current_subprocess = proc
    return True


def clear_active_subprocess(proc: Any) -> None:
    """Clear the registered subprocess if it matches ``proc``.

    Identity match prevents a slow-tool-A's finally block from clearing a
    new tool-B's registration in the rare interleaved case. No-op when
    nothing is registered or a different proc is registered.
    """
    cid = current_context_id.get()
    if not cid:
        return
    entry = _INBOX.peek(cid)
    if entry is None:
        return
    if entry.current_subprocess is proc:
        entry.current_subprocess = None


@dataclass
class InboxEntry:
    """State for a single ``context_id`` — see module docstring."""

    context_id: str
    pending_messages: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_inbox_maxsize())
    )
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

    def request_interrupt(self, new_message: Any) -> bool:
        """Queue a new message and signal the running turn to interrupt.

        Idempotent — calling twice in rapid succession just enqueues both
        messages; the running turn drains them in order on its next
        interrupt check.

        Returns ``True`` when the message was accepted, ``False`` when the
        inbox is at capacity (caller should emit a busy backpressure
        response).  NEVER silently drops.
        """
        # Queue first, THEN set the event — guarantees the interrupted
        # turn always sees at least one pending message when it wakes
        # (no lost-wakeup race between the event-set and the queue-put).
        try:
            self.pending_messages.put_nowait(new_message)
        except asyncio.QueueFull:
            logger.warning(
                "runtime_inbox: inbox full for context_id=%s "
                "(maxsize=%d) — rejecting message",
                self.context_id,
                self.pending_messages.maxsize,
            )
            return False
        self.interrupt_event.set()
        return True

    def defer_message(self, new_message: Any) -> bool:
        """Queue a new message WITHOUT signalling an interrupt.

        The running turn drains it via ``consume_pending()`` at NATURAL
        completion and answers it as a follow-up turn — it is never
        interrupted. This is the "queue, don't break" model: only an
        explicit Stop (``tasks/cancel`` -> ``interrupt_event``) breaks a
        turn in flight.

        Returns ``True`` when the message was accepted, ``False`` when the
        inbox is at capacity (caller should emit a busy backpressure
        response).  NEVER silently drops.
        """
        try:
            self.pending_messages.put_nowait(new_message)
        except asyncio.QueueFull:
            logger.warning(
                "runtime_inbox: inbox full for context_id=%s "
                "(maxsize=%d) — rejecting deferred message",
                self.context_id,
                self.pending_messages.maxsize,
            )
            return False
        return True

    def consume_pending(self) -> list[Any]:
        """Drain all queued messages without blocking. Clears the interrupt event."""
        out: list[Any] = []
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

        Sends SIGTERM via ``terminate()`` first (graceful — lets the child
        flush stdout/stderr buffers). The caller is expected to poll for
        liveness within the 2s SLA from feedback_canvas_stop_all; if the
        child ignores SIGTERM the executor follows up with ``hard_kill()``.
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

    def hard_kill_subprocess(self) -> bool:
        """Escalation path — SIGKILL the registered subprocess.

        Called by the A2A cancel handler after a short SIGTERM grace period
        when the child hasn't exited. Returns True on kill attempt. Safe to
        call even when no subprocess is registered or the proc has exited.
        """
        proc = self.current_subprocess
        if proc is None:
            return False
        try:
            if hasattr(proc, "returncode") and proc.returncode is not None:
                return False
            if hasattr(proc, "poll") and callable(proc.poll):
                if proc.poll() is not None:
                    return False
            proc.kill()
            logger.warning(
                "runtime_inbox: SIGKILL'd subprocess for context_id=%s "
                "(did not exit on SIGTERM grace)",
                self.context_id,
            )
            return True
        except (ProcessLookupError, OSError) as exc:
            logger.debug(
                "runtime_inbox: hard_kill_subprocess no-op: %s", exc,
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

    def any_turn_in_flight(self) -> bool:
        """True iff SOME context currently has a turn executing.

        The single source of truth for "is this workspace busy?" — reads the
        same ``turn_in_flight`` flag the A2A executor sets True at the start of
        a turn (inside the get_or_create lock) and clears False in its finally.
        Iterates a snapshot of the entries dict so a concurrent get_or_create
        can't raise "dictionary changed size during iteration"; the read is
        lock-free by design (a stale-by-one-beat answer is acceptable for a
        liveness signal and avoids awaiting an asyncio lock from the sync
        heartbeat thread).
        """
        return any(e.turn_in_flight for e in list(self._entries.values()))

    def reset_for_tests(self) -> None:
        """Test-only: clear all entries. Production code never calls this."""
        self._entries.clear()


# Module-level singleton — one inbox per process, shared across all concurrent
# A2A requests. Matches the current isolation boundary: one workspace = one
# runtime process.
_INBOX = RuntimeInbox()


def get_inbox() -> RuntimeInbox:
    """Return the process-wide inbox singleton."""
    return _INBOX


def any_turn_in_flight() -> bool:
    """Process-wide busy signal: True while any context has a turn executing.

    Thin accessor over the inbox singleton so callers (e.g. the heartbeat
    sender's ``is_busy`` field) don't reach into module internals. Sourced
    from the live ``turn_in_flight`` state — no duplicate turn-tracking.
    """
    return _INBOX.any_turn_in_flight()
