"""Every adapter executor participates in the turn lease — not just the base.

Issue #408 was filed against ``SubprocessA2AExecutor``, but that class has
exactly ONE subclass in the fleet. Of the four "subprocess flavours", only
openclaw inherits it; claude-code, codex and hermes each implement
``AgentExecutor`` directly in their own template repo and therefore inherit
nothing. Fixing only the base would have fixed one flavour in four and left no
test able to notice.

``TurnLeaseExecutor`` is applied at ``main.py``'s single executor funnel, so
these tests use a bare executor that inherits NOTHING from the runtime — the
shape of a template-owned executor — and pin that it still participates.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from molecule_runtime import turn_lease
from molecule_runtime.channel_events import turn_liveness_snapshot
from molecule_runtime.turn_lease_executor import TurnLeaseExecutor, wrap_executor

BOOT_UPTIME_SECONDS = 5000.0
TTL_SECONDS = 10.0
ABSOLUTE_CAP_SECONDS = 100.0
_MTIME_BASE = time.time() + 3600.0


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ForeignExecutor:
    """A template-owned executor. Inherits NOTHING from molecule_runtime.

    This is the real shape of ClaudeSDKExecutor / CodexAppServerExecutor /
    HermesAgentProxyExecutor: they subclass a2a's AgentExecutor and know
    nothing about the turn lease.
    """

    def __init__(self, clock: _FakeClock, work) -> None:
        self._clock = clock
        self._work = work
        self.snapshot: dict | None = None
        self.activity_path_in_env: str | None = None
        self.cancelled = False
        self._last_tool_uses = ["Read", "Bash"]

    async def execute(self, context, event_queue):
        self.activity_path_in_env = os.environ.get("MOLECULE_TOOL_ACTIVITY_FILE")
        self._work(self._clock)
        self.snapshot = turn_liveness_snapshot()
        await event_queue.enqueue_event("reply")

    async def cancel(self, context, event_queue):
        self.cancelled = True


class _RecordingQueue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


def _record_activity(path, seq: int = 0) -> None:
    path.write_text("1")
    at = _MTIME_BASE + seq
    os.utime(path, (at, at))


@pytest.fixture
def fed_lease(monkeypatch, tmp_path):
    """A boot-installed lease on a flavour with a demonstrated activity feed."""
    activity = tmp_path / "tool_activity"
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(activity))
    for name in (
        "MOLECULE_MAX_TURN_SECONDS",
        "MOLECULE_TURN_LEASE_TTL_SECONDS",
        "A2A_COMPLETION_IDLE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    clock = _FakeClock(0.0)
    lease = turn_lease.TurnLease(
        ttl_seconds=TTL_SECONDS,
        absolute_cap_seconds=ABSOLUTE_CAP_SECONDS,
        clock=clock,
    )
    turn_lease.install(lease)
    lease.touch()  # this flavour feeds the lease
    clock.advance(BOOT_UPTIME_SECONDS)
    try:
        yield clock, lease, activity
    finally:
        turn_lease.install(None)


@pytest.mark.asyncio
async def test_foreign_executor_turn_is_dated_to_the_turn(fed_lease):
    clock, _lease, _activity = fed_lease
    inner = _ForeignExecutor(clock, lambda c: c.advance(7.0))
    ex = wrap_executor(inner)

    await ex.execute(SimpleNamespace(), _RecordingQueue())

    # Pre-fix: 5007.0 (container uptime), for a runtime that inherits nothing.
    assert inner.snapshot["turn_age_seconds"] == pytest.approx(7.0)
    assert inner.snapshot["absolute_cap_exceeded"] is False


@pytest.mark.asyncio
async def test_unwrapped_foreign_executor_reads_container_uptime(fed_lease):
    """Pins that the WRAPPER is what does the work — the pre-fix behaviour.

    Identical to the test above in every respect except one: the executor is
    not wrapped. This is production before runtime#408 for claude-code, codex
    and hermes, and it is what the scheduler's attributability gate has been
    rejecting as "no signal". If someone removes the wrap in main.py, the test
    above goes red and this one stays green — which names the cause exactly.
    """
    clock, _lease, _activity = fed_lease
    inner = _ForeignExecutor(clock, lambda c: c.advance(7.0))

    await inner.execute(SimpleNamespace(), _RecordingQueue())  # NOT wrapped

    assert inner.snapshot["turn_age_seconds"] == pytest.approx(BOOT_UPTIME_SECONDS + 7.0)
    # ...and the absolute cap reads exceeded for a 7-second-old turn.
    assert inner.snapshot["absolute_cap_exceeded"] is True
    assert inner.snapshot["alive"] is False


@pytest.mark.asyncio
async def test_foreign_executor_wedged_turn_still_reads_idle(fed_lease):
    """The negative control, at the wrapper layer.

    Differs from the working case below in EXACTLY ONE input: this turn records
    no tool activity.
    """
    clock, _lease, _activity = fed_lease
    inner = _ForeignExecutor(clock, lambda c: c.advance(TTL_SECONDS * 3))
    ex = wrap_executor(inner)

    await ex.execute(SimpleNamespace(), _RecordingQueue())

    assert inner.snapshot["idle_seconds"] == pytest.approx(TTL_SECONDS * 3)
    assert inner.snapshot["idle_expired"] is True
    assert inner.snapshot["alive"] is False
    assert inner.snapshot["turn_age_seconds"] == pytest.approx(TTL_SECONDS * 3)


@pytest.mark.asyncio
async def test_foreign_executor_working_turn_reads_alive(fed_lease):
    clock, _lease, activity = fed_lease

    def work(c):
        for i in range(6):
            _record_activity(activity, i)
            c.advance(TTL_SECONDS / 2)

    inner = _ForeignExecutor(clock, work)
    ex = wrap_executor(inner)

    await ex.execute(SimpleNamespace(), _RecordingQueue())

    assert inner.snapshot["idle_seconds"] <= TTL_SECONDS
    assert inner.snapshot["idle_expired"] is False
    assert inner.snapshot["turn_age_seconds"] == pytest.approx(TTL_SECONDS * 3)
    assert inner.snapshot["alive"] is True


@pytest.mark.asyncio
async def test_activity_path_exported_before_the_child_is_spawned(fed_lease, monkeypatch):
    """codex and hermes both no-op their tool-activity ping when this is unset.

    It was exported ONLY by a2a_executor, which never runs on those flavours —
    so the tier-C feed written specifically for them was dead on arrival.
    """
    clock, _lease, _activity = fed_lease
    monkeypatch.delenv("MOLECULE_TOOL_ACTIVITY_FILE", raising=False)
    inner = _ForeignExecutor(clock, lambda c: None)
    ex = wrap_executor(inner)

    await ex.execute(SimpleNamespace(), _RecordingQueue())

    assert inner.activity_path_in_env


@pytest.mark.asyncio
async def test_creating_the_activity_file_is_not_activity(monkeypatch, tmp_path):
    """Materializing the liveness file must not itself count as a tool call.

    The file is created by the runtime at every turn setup. If that creation
    advanced the mtime watermark from zero, the first read would register a
    touch — so a flavour that never writes the file would look like one that
    does, `observed_activity` would be true everywhere, and the arming check
    would be defeated on exactly the flavour it protects.
    """
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(tmp_path / "tool_activity"))
    clock = _FakeClock(0.0)
    lease = turn_lease.TurnLease(
        ttl_seconds=TTL_SECONDS, absolute_cap_seconds=ABSOLUTE_CAP_SECONDS, clock=clock
    )
    turn_lease.install(lease)
    try:
        assert lease.observed_activity is False
        inner = _ForeignExecutor(clock, lambda c: c.advance(1.0))
        await wrap_executor(inner).execute(SimpleNamespace(), _RecordingQueue())
        # The file now exists (the runtime created it) — but nothing wrote a
        # tool call to it, so no activity may have been recorded.
        assert lease.observed_activity is False
    finally:
        turn_lease.install(None)


@pytest.mark.asyncio
async def test_wrapper_is_transparent(fed_lease):
    """Delegation must be total: cancel, and duck-typed reads the tracer makes."""
    clock, _lease, _activity = fed_lease
    inner = _ForeignExecutor(clock, lambda c: None)
    ex = wrap_executor(inner)

    q = _RecordingQueue()
    await ex.execute(SimpleNamespace(), q)
    assert q.events == ["reply"]  # the reply still flows through untouched

    await ex.cancel(SimpleNamespace(), q)
    assert inner.cancelled is True

    # tracing.TracingExecutor reads these off the executor by duck-typing.
    assert ex._last_tool_uses == ["Read", "Bash"]


@pytest.mark.asyncio
async def test_kernel_off_is_a_pass_through(monkeypatch, tmp_path):
    """No lease installed => no file materialized, no env exported, no change."""
    monkeypatch.delenv("MOLECULE_TOOL_ACTIVITY_FILE", raising=False)
    turn_lease.install(None)
    clock = _FakeClock(0.0)
    inner = _ForeignExecutor(clock, lambda c: None)
    ex = wrap_executor(inner)

    await ex.execute(SimpleNamespace(), _RecordingQueue())

    assert inner.snapshot == {
        "lease": False,
        "reason": "no turn lease is installed (mailbox kernel off)",
    }
    # The kernel-off path must not acquire a side effect it never had.
    assert inner.activity_path_in_env is None


def test_wrap_executor_is_fail_open():
    assert wrap_executor(None) is None
    inner = object()
    assert isinstance(wrap_executor(inner), TurnLeaseExecutor)
