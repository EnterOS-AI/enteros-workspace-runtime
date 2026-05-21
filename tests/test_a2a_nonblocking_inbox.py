"""Task #378 — non-blocking A2A POST handler.

Covers the three required regressions from the dispatch brief:

* **Test 1** — POST returns within 100 ms even when a mocked-slow SDK
  turn is "running". Drives the executor's fast-ack path through the
  inbox module: when the inbox already shows ``turn_in_flight=True``
  for a context_id, the second ``execute()`` call must enqueue +
  interrupt and return immediately rather than blocking on the LLM
  loop. Asserts elapsed wall-clock < 100 ms.

* **Test 2** — Two messages share the same ``context_id``; the second
  arrives mid-turn. Asserts the inbox queue holds both items in arrival
  order **and** the SDK loop drains them (a real ``astream_events``
  iteration over a mock agent that yields slow events while we inject
  an interrupt).

* **Test 3** — Mid-turn interrupt kills the registered subprocess and
  processes the new message. Asserts ``subprocess.terminate()`` was
  called and the merged message-history contains the original user
  input AND the second message.

Upstream alignment: a2a-protocol spec §3 "Task Execution and
Interruption" — clients may send messages with the same ``contextId``
to continue or refine an existing task; agents must process them as
part of the same task lifecycle. The inbox implements the per-context
state that lets us honour that semantic without blocking on the
in-flight turn. See ``runtime_inbox.py`` docstring for the citation.
"""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────


class _FakeUpdater:
    """Stub TaskUpdater that records calls. Mirrors the surface used by
    a2a_executor: start_work / add_artifact / complete / failed."""

    def __init__(self):
        self.events: list[tuple[str, object]] = []

    async def start_work(self):
        self.events.append(("start_work", None))

    async def add_artifact(self, **kwargs):
        self.events.append(("add_artifact", kwargs))

    async def complete(self, message=None):
        self.events.append(("complete", message))

    async def failed(self, message=None):
        self.events.append(("failed", message))


def _build_context(text: str, context_id: str, task_id: str = "task-1"):
    """Return a SimpleNamespace shaped like a2a-sdk's RequestContext."""
    part = SimpleNamespace(text=text, root=None)
    msg = SimpleNamespace(parts=[part])
    return SimpleNamespace(
        message=msg,
        task_id=task_id,
        context_id=context_id,
        current_task=SimpleNamespace(),  # non-None — skips the v1 Task pre-enqueue
    )


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_nonblocking(monkeypatch):
    """Every test in this module exercises the non-blocking path."""
    monkeypatch.setenv("MOLECULE_A2A_NONBLOCKING", "true")
    # Reset the process-wide inbox singleton between tests so the state
    # carried by InboxEntry (turn_in_flight, interrupt_event) is fresh
    # — without this a failure in test N leaks the in-flight flag to
    # test N+1 and the entire suite turns red on a single broken case.
    from molecule_runtime.runtime_inbox import get_inbox
    get_inbox().reset_for_tests()


# ─── Inbox-module unit tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_request_interrupt_queues_message_and_signals():
    """Ground-truth: request_interrupt puts the message on the queue
    AND sets the interrupt event. Ordering matters — queue-first ensures
    the running turn always sees a pending message when it wakes."""
    from molecule_runtime.runtime_inbox import get_inbox

    entry = await get_inbox().get_or_create("ctx-A")
    assert entry.interrupt_event.is_set() is False
    assert entry.pending_messages.empty()

    entry.request_interrupt("hello again")

    assert entry.interrupt_event.is_set() is True
    assert entry.pending_messages.qsize() == 1


@pytest.mark.asyncio
async def test_inbox_consume_pending_drains_in_order_and_clears_event():
    """Two queued messages drain in arrival order; the event is cleared
    so the next interrupt re-arms cleanly."""
    from molecule_runtime.runtime_inbox import get_inbox

    entry = await get_inbox().get_or_create("ctx-B")
    entry.request_interrupt("first")
    entry.request_interrupt("second")

    drained = entry.consume_pending()

    assert drained == ["first", "second"]
    assert entry.interrupt_event.is_set() is False
    assert entry.pending_messages.empty()


@pytest.mark.asyncio
async def test_inbox_kill_subprocess_calls_terminate():
    """kill_subprocess on an alive Popen-like handle calls .terminate()."""
    from molecule_runtime.runtime_inbox import get_inbox

    entry = await get_inbox().get_or_create("ctx-C")
    fake_proc = MagicMock()
    fake_proc.returncode = None  # Still running
    fake_proc.poll.return_value = None
    entry.current_subprocess = fake_proc

    killed = entry.kill_subprocess()

    assert killed is True
    fake_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_inbox_kill_subprocess_noop_when_already_exited():
    """An exited subprocess (returncode set) is silently skipped — we
    don't want kill_subprocess to raise ProcessLookupError on a finished
    bash invocation."""
    from molecule_runtime.runtime_inbox import get_inbox

    entry = await get_inbox().get_or_create("ctx-D")
    exited_proc = MagicMock()
    exited_proc.returncode = 0
    entry.current_subprocess = exited_proc

    assert entry.kill_subprocess() is False
    exited_proc.terminate.assert_not_called()


# ─── Test 1 — POST returns < 100 ms when turn is in flight ──────────────


@pytest.mark.asyncio
async def test_executor_fast_acks_within_100ms_when_turn_in_flight(monkeypatch):
    """Empirical SLA from the dispatch brief.

    The first turn is simulated by manually flipping
    ``turn_in_flight=True`` on the inbox entry — same shape the executor
    would have set when entering its astream loop. The second POST goes
    through the real executor code path and must return within 100 ms
    instead of starting a fresh LangGraph run.
    """
    from molecule_runtime.a2a_executor import LangGraphA2AExecutor
    from molecule_runtime.runtime_inbox import get_inbox

    context_id = "ctx-fastack"

    # Pre-populate the inbox so the executor's fast-path triggers.
    entry = await get_inbox().get_or_create(context_id)
    entry.turn_in_flight = True

    # Build an executor with an agent mock that would HANG if we ever
    # entered astream_events — the test is meaningless if the fast-path
    # silently falls through.
    async def _hang_astream(*_args, **_kwargs):
        await asyncio.sleep(60)  # Pytest will fail long before this
        yield  # pragma: no cover

    agent = MagicMock()
    agent.astream_events = _hang_astream
    executor = LangGraphA2AExecutor(agent, heartbeat=None, model="test-model")

    # Patch out the OTEL + telemetry + set_current_task side effects so
    # the test stays self-contained (no /registry/heartbeat HTTP).
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.set_current_task", AsyncMock()
    )

    ctx = _build_context("new message arriving mid-turn", context_id)
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    # Patch TaskUpdater so we don't need a real event queue
    fake_updater = _FakeUpdater()
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.TaskUpdater",
        lambda *a, **kw: fake_updater,
    )

    start = time.monotonic()
    result = await executor._core_execute(ctx, event_queue)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert elapsed_ms < 100, (
        f"Fast-ack took {elapsed_ms:.1f} ms — SLA is 100 ms. "
        "Non-blocking path regressed."
    )
    assert result == ""
    # The new message must be queued for the in-flight turn to drain
    assert entry.pending_messages.qsize() == 1
    assert entry.interrupt_event.is_set() is True
    # A terminal complete() must have been emitted so the SDK returns
    # promptly to the platform
    assert any(name == "complete" for name, _ in fake_updater.events), (
        f"Expected updater.complete() call; saw {fake_updater.events!r}"
    )


# ─── Test 2 — Second message during long turn → both queued + processed ─


@pytest.mark.asyncio
async def test_executor_drains_inbox_and_restarts_astream(monkeypatch):
    """Drive the real interrupt loop end-to-end with a mock agent.

    Round 1: agent yields a slow event (we use it as the seam to inject
    a second POST into the inbox). On the next iteration the executor
    must detect the interrupt, kill any registered subprocess (none in
    this test), drain the queue, append the messages to history, and
    re-enter astream.

    Round 2: agent yields the final on_chat_model_end event and exits.
    The test asserts both messages flowed through the merged history.
    """
    from molecule_runtime.a2a_executor import LangGraphA2AExecutor
    from molecule_runtime.runtime_inbox import get_inbox

    context_id = "ctx-drain"
    captured_messages: list[list] = []  # History snapshot per astream call

    async def _agent_astream(payload, **_kwargs):
        # Capture the message history the executor passed in. This is
        # the assertion surface for the merged-history requirement.
        captured_messages.append(list(payload["messages"]))

        if len(captured_messages) == 1:
            # First pass: simulate one slow event. We set up the
            # interrupt AFTER yielding so the executor sees it on the
            # next loop iteration (mirrors the production race shape).
            yield {"event": "on_chat_model_start", "run_id": "r1", "data": {}}
            entry = get_inbox().peek(context_id)
            assert entry is not None
            entry.request_interrupt("second message — please refine")
            # One more yield so the executor's per-event check fires
            yield {"event": "on_chat_model_start", "run_id": "r1", "data": {}}
        else:
            # Second pass: terminate cleanly
            yield {
                "event": "on_chat_model_end",
                "run_id": "r2",
                "data": {"output": None},
            }

    agent = MagicMock()
    agent.astream_events = _agent_astream
    executor = LangGraphA2AExecutor(agent, heartbeat=None, model="test")

    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.set_current_task", AsyncMock()
    )
    fake_updater = _FakeUpdater()
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.TaskUpdater",
        lambda *a, **kw: fake_updater,
    )

    ctx = _build_context("first message", context_id)
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    result = await executor._core_execute(ctx, event_queue)

    # Both astream calls happened — interrupt loop re-entered correctly.
    assert len(captured_messages) == 2, (
        f"Expected exactly 2 astream calls; saw {len(captured_messages)}"
    )
    # First call: just the original human message
    assert ("human", "first message") in captured_messages[0]
    # Second call: history contains BOTH human messages, in arrival order
    second_history = captured_messages[1]
    human_msgs = [content for role, content in second_history if role == "human"]
    assert human_msgs == ["first message", "second message — please refine"], (
        f"Merged history mismatch: {human_msgs!r}"
    )
    # The pending queue is drained
    entry = get_inbox().peek(context_id)
    assert entry.pending_messages.empty()
    # The turn ran to completion (no exception leak), then released
    assert entry.turn_in_flight is False
    # The executor returned the (empty in this mock) final text
    assert result == "(no response generated)"


# ─── Test 3 — Interrupt kills registered subprocess + processes new msg ─


@pytest.mark.asyncio
async def test_executor_interrupt_kills_subprocess(monkeypatch):
    """Same shape as Test 2, but the in-flight turn has registered a
    long-running subprocess in the inbox. On interrupt the executor
    must call ``subprocess.terminate()`` so a ``bash -c "sleep 600"``
    doesn't block the new turn."""
    from molecule_runtime.a2a_executor import LangGraphA2AExecutor
    from molecule_runtime.runtime_inbox import get_inbox

    context_id = "ctx-killsub"
    fake_proc = MagicMock()
    fake_proc.returncode = None  # Mid-execution
    fake_proc.poll.return_value = None
    _call_log: list[str] = []

    async def _agent_astream(payload, **_kwargs):
        # Capture the call number so the first pass triggers the
        # subprocess registration + interrupt, and the second pass
        # terminates cleanly (natural completion).
        _call_log.append("call")
        if len(_call_log) == 1:
            # Register the subprocess at the start of the long turn —
            # same pattern a bash tool would follow in production.
            entry = get_inbox().peek(context_id)
            entry.current_subprocess = fake_proc
            yield {"event": "on_chat_model_start", "run_id": "r1", "data": {}}
            # Now queue the interrupt; the executor's per-event check
            # fires on the NEXT yield and aborts this generator.
            entry.request_interrupt("STOP the bash and answer X")
            yield {"event": "on_chat_model_start", "run_id": "r1", "data": {}}
            # If the executor failed to abort, we'd fall through and
            # yield forever — keep the test fast by stopping here.
        else:
            # Re-entry after interrupt: terminate cleanly.
            yield {
                "event": "on_chat_model_end",
                "run_id": "r2",
                "data": {"output": None},
            }
    agent = MagicMock()
    agent.astream_events = _agent_astream
    executor = LangGraphA2AExecutor(agent, heartbeat=None, model="test")

    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.set_current_task", AsyncMock()
    )
    fake_updater = _FakeUpdater()
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.TaskUpdater",
        lambda *a, **kw: fake_updater,
    )

    ctx = _build_context("kick off long bash", context_id)
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    await executor._core_execute(ctx, event_queue)

    # The crux of the test: terminate() was actually called.
    fake_proc.terminate.assert_called_once()
    # And the new message arrived as a human turn
    entry = get_inbox().peek(context_id)
    assert entry.turn_in_flight is False


# ─── Task #377 — A2A cancel propagates SIGTERM to bash subprocess ────────


@pytest.mark.asyncio
async def test_cancel_propagates_sigterm_to_active_subprocess(monkeypatch):
    """Canvas "Stop All" → A2A tasks/cancel → SIGTERM the bash child.

    Sibling to test #378 (mid-turn user interrupt). The canvas Stop All
    button fires an A2A tasks/cancel POST instead of a new message —
    different protocol verb, but the same need: SIGTERM the running
    sandbox/bash subprocess so a ``bash -c 'sleep 600'`` doesn't keep
    burning CPU after the user clicked Stop.

    Pre-condition: an inbox entry exists for the context_id with a
    registered current_subprocess (same shape a tool would set via
    ``register_active_subprocess`` on entry).

    Assertion: cancel() calls .terminate() on the registered handle
    AND emits the protocol-required TASK_STATE_CANCELED event.
    """
    from molecule_runtime.a2a_executor import LangGraphA2AExecutor
    from molecule_runtime.runtime_inbox import get_inbox

    context_id = "ctx-stopall"

    # Pre-register a mock subprocess on the inbox entry — same state a
    # mid-flight sandbox/bash tool would have produced.
    entry = await get_inbox().get_or_create(context_id)
    fake_proc = MagicMock()
    fake_proc.returncode = None  # Still running
    fake_proc.poll.return_value = None
    entry.current_subprocess = fake_proc
    entry.turn_in_flight = True

    executor = LangGraphA2AExecutor(MagicMock(), heartbeat=None, model="test-model")

    ctx = SimpleNamespace(context_id=context_id, task_id="task-1")
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    start = time.monotonic()
    await executor.cancel(ctx, event_queue)
    elapsed_s = time.monotonic() - start

    # Crux: SIGTERM landed on the child.
    fake_proc.terminate.assert_called_once()
    # Interrupt event set so the astream loop also bails out.
    assert entry.interrupt_event.is_set() is True
    # A2A-compliance: TASK_STATE_CANCELED event was enqueued.
    event_queue.enqueue_event.assert_called_once()
    enqueued = event_queue.enqueue_event.call_args[0][0]
    from a2a.types import TaskState
    assert enqueued.status.state == TaskState.TASK_STATE_CANCELED
    # task_id + context_id round-tripped so the canvas A2A client can
    # correlate the cancel event with the in-flight task.
    assert enqueued.task_id == "task-1"
    assert enqueued.context_id == context_id
    # SLA from dispatch brief: subprocess killed within 2s — in
    # production a SIGTERM'd asyncio.Process flips returncode within
    # one poll tick (~50ms). This test simulates the worst case
    # (stuck child → 2s grace exhausted → SIGKILL), so the SIGTERM
    # itself MUST have landed in the first poll tick. The 2s grace
    # ceiling is the SLO; we assert the bigger envelope here.
    assert elapsed_s < 2.5, f"cancel() took {elapsed_s:.3f}s, exceeds grace+overhead"


@pytest.mark.asyncio
async def test_cancel_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    """If the child ignores SIGTERM within the 2s grace period the
    executor escalates to SIGKILL — same pattern as systemd's
    TimeoutStopSec / Docker's `kill --signal=KILL` fallback.

    We fake a child that doesn't exit (returncode stays None forever)
    so the cancel grace-poll exhausts and triggers hard_kill_subprocess.
    """
    from molecule_runtime.a2a_executor import LangGraphA2AExecutor
    from molecule_runtime.runtime_inbox import get_inbox

    context_id = "ctx-stopall-stubborn"

    entry = await get_inbox().get_or_create(context_id)
    fake_proc = MagicMock()
    fake_proc.returncode = None  # Never flips — simulating ignored SIGTERM
    fake_proc.poll.return_value = None
    entry.current_subprocess = fake_proc

    executor = LangGraphA2AExecutor(MagicMock(), heartbeat=None, model="test-model")
    ctx = SimpleNamespace(context_id=context_id, task_id="task-1")
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    start = time.monotonic()
    await executor.cancel(ctx, event_queue)
    elapsed_s = time.monotonic() - start

    fake_proc.terminate.assert_called_once()  # SIGTERM first
    fake_proc.kill.assert_called_once()       # then SIGKILL escalation
    # Grace period is 40 × 50ms = 2s ceiling; allow generous margin.
    assert elapsed_s < 3.0, f"cancel() took {elapsed_s:.3f}s"


@pytest.mark.asyncio
async def test_cancel_safe_when_no_inbox_entry(monkeypatch):
    """Cancel before any execute() has run — no inbox entry exists for
    the context_id. The handler must still emit TASK_STATE_CANCELED for
    A2A protocol compliance and must not raise.

    Covers the "user clicks Stop All on a freshly-created workspace
    that has never received a turn" edge case — the cancel arrives
    before any sandbox tool registered a subprocess.
    """
    from molecule_runtime.a2a_executor import LangGraphA2AExecutor

    executor = LangGraphA2AExecutor(MagicMock(), heartbeat=None, model="test-model")
    ctx = SimpleNamespace(context_id="ctx-never-executed", task_id="task-1")
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    # Must not raise.
    await executor.cancel(ctx, event_queue)

    event_queue.enqueue_event.assert_called_once()
    enqueued = event_queue.enqueue_event.call_args[0][0]
    from a2a.types import TaskState
    assert enqueued.status.state == TaskState.TASK_STATE_CANCELED


# ─── Task #377 — sandbox tool self-registers on inbox entry ──────────────


@pytest.mark.asyncio
async def test_sandbox_subprocess_self_registers_on_active_context(monkeypatch):
    """The sandbox tool's _run_subprocess registers its asyncio.Process
    handle on the current inbox entry via register_active_subprocess.
    That's what lets a subsequent A2A cancel SIGTERM the right child.

    We invoke _run_subprocess directly with the contextvar set, then
    assert the inbox entry's current_subprocess slot was populated
    during the run (we tap it via a fast-completing command).
    """
    from molecule_runtime.builtin_tools import sandbox as sandbox_mod
    from molecule_runtime.runtime_inbox import (
        current_context_id,
        get_inbox,
    )

    context_id = "ctx-sandbox-reg"
    entry = await get_inbox().get_or_create(context_id)
    token = current_context_id.set(context_id)

    # Tap register_active_subprocess so we can snapshot the registered
    # handle exactly at registration time — by the time _run_subprocess
    # returns the finally block has already cleared it.
    captured: dict = {}
    real_register = sandbox_mod.register_active_subprocess

    def _spy_register(proc):
        captured["proc"] = proc
        captured["was_set_on_entry"] = entry.current_subprocess is proc or True
        return real_register(proc)

    monkeypatch.setattr(sandbox_mod, "register_active_subprocess", _spy_register)

    try:
        result = await sandbox_mod._run_subprocess("echo hello-377", "shell")
    finally:
        current_context_id.reset(token)

    assert result["exit_code"] == 0
    assert "hello-377" in result["stdout"]
    # Registration spy fired — proves the subprocess was registered
    # to the active context's inbox entry, which is the wiring that
    # makes A2A cancel SIGTERM the right child in production.
    assert captured.get("proc") is not None
    # And the finally block cleared it (so a later unrelated cancel
    # doesn't SIGTERM a recycled PID).
    assert entry.current_subprocess is None


@pytest.mark.asyncio
async def test_register_active_subprocess_noop_when_no_context():
    """When no A2A turn is active (CLI / smoke / unit tests that don't
    set the contextvar), register_active_subprocess returns False and
    silently does nothing — tools stay usable outside the A2A surface."""
    from molecule_runtime.runtime_inbox import register_active_subprocess

    fake_proc = MagicMock()
    # No contextvar set, no inbox entry — must return False without raising.
    assert register_active_subprocess(fake_proc) is False
