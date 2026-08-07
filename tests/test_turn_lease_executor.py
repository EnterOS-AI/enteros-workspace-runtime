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

import asyncio
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
    rejecting as "no signal".

    Scope of the claim: this pins the CONTRAST (wrapped vs not), nothing more.
    It says nothing about whether production actually applies the wrap — every
    test in this file wraps the executor itself, so all of them stay green if
    ``main.py`` stops doing it. That one production line is covered by
    :func:`test_main_py_wraps_the_executor_in_the_turn_lease` below, which was
    added after this docstring was found to be claiming otherwise.
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


# --------------------------------------------------------------------------- #
# runtime#409 — A DELIVERY IS NOT A TURN.
#
# `execute()` is entered once per DELIVERY. On the native executor it delegates
# to `_core_execute()`, which is RE-ENTERED for every delivery that arrives
# while a turn is in flight (MOLECULE_A2A_NONBLOCKING is default-ON fleet-wide,
# so this is the live path). A routine self-ping is fast-acked and dropped
# (`return ""`, a2a_executor.py:680); a user message is deferred at the same
# point. NEITHER becomes a turn — both return BEFORE the `turn_in_flight = True`
# gate at :681 and therefore never reach the `reset_current()` at :812, which is
# why the pre-#408 arm site was safe.
#
# A wrapper that armed on every `execute()` arms before that gate. `reset()`
# moves the absolute-cap origin as well as the idle clock, so one dropped
# self-ping resets BOTH; on a workspace taking periodic self-pings (cron tick,
# delegation harvester) that repeats indefinitely and a wedged turn is NEVER
# reaped — while the armed lease makes the scheduler's `lease_is_attributable`
# gate pass, so the daemon TRUSTS the "alive" reading instead of falling back
# to its own ceiling. Strictly worse than the unarmed state #408 set out to fix.
# --------------------------------------------------------------------------- #
class _NativeLikeExecutor:
    """The shape of the native executor under MOLECULE_A2A_NONBLOCKING.

    The first delivery becomes a turn: it passes the in-flight gate and arms
    the lease at the :812 site, then wedges (produces no activity at all). Any
    delivery arriving while that turn is in flight is fast-acked and returned
    at :680 — never a turn, and it must never touch the lease.
    """

    def __init__(self) -> None:
        self.turn_in_flight = False
        self.release = asyncio.Event()
        self.deliveries_dropped = 0

    async def execute(self, context, event_queue):
        if self.turn_in_flight:                      # a2a_executor.py ~:660
            await event_queue.enqueue_event("[Acknowledged — queued]")
            self.deliveries_dropped += 1
            return ""                                # :680 — NOT a turn
        self.turn_in_flight = True                   # :681
        turn_lease.reset_current()                   # :812 — the real arm
        await self.release.wait()
        self.turn_in_flight = False
        return "done"

    async def cancel(self, context, event_queue):  # pragma: no cover
        pass


@pytest.mark.parametrize(
    "delivery",
    [
        pytest.param("self-ping", id="dropped_self_ping"),
        pytest.param("user message", id="deferred_user_message"),
    ],
)
@pytest.mark.asyncio
async def test_delivery_that_never_becomes_a_turn_does_not_re_arm(fed_lease, delivery):
    """A delivery dropped/deferred mid-turn must not reset the lease clocks.

    Both dispositions leave ``_core_execute`` at the SAME point (:680), before
    the in-flight gate — the parametrisation names the two live sources rather
    than two code paths.

    Pre-fix this reads ``turn_age=0.0 idle=0.0 alive=True`` after the delivery:
    the wedged turn's idle clock AND its absolute-cap origin are both reset, so
    a periodically-pinged workspace can never reap it.
    """
    clock, _lease, _activity = fed_lease
    inner = _NativeLikeExecutor()
    ex = wrap_executor(inner)

    # The real turn starts, arms, and wedges.
    turn = asyncio.create_task(ex.execute(SimpleNamespace(), _RecordingQueue()))
    while not inner.turn_in_flight:
        await asyncio.sleep(0)
    clock.advance(TTL_SECONDS * 9)  # 90s of silence: idle-expired, cap not yet hit
    before = turn_liveness_snapshot()
    assert before["idle_expired"] is True, "fixture did not produce a wedged turn"
    assert before["absolute_cap_exceeded"] is False, (
        "the cap must NOT already be exceeded, or a cap-origin reset would be "
        "invisible and this test could pass vacuously"
    )
    assert before["alive"] is False

    try:
        # ...and the delivery arrives mid-turn.
        await ex.execute(SimpleNamespace(), _RecordingQueue())
        assert inner.deliveries_dropped == 1, (
            f"the {delivery} did not take the fast-ack/drop path — this test "
            "would then be measuring a second turn, not a dropped delivery"
        )

        after = turn_liveness_snapshot()
        assert after["turn_age_seconds"] == pytest.approx(
            before["turn_age_seconds"]
        ), (
            "a dropped delivery re-dated the ABSOLUTE-CAP origin: the wedged "
            "turn's 3600s cap now restarts on every self-ping and the turn can "
            "never be reaped (runtime#409)"
        )
        assert after["idle_seconds"] == pytest.approx(before["idle_seconds"]), (
            "a dropped delivery reset the IDLE clock: the 900s TTL now "
            "restarts on every self-ping (runtime#409)"
        )
        assert after["idle_expired"] is True
        assert after["alive"] is False, (
            "the lease reports a wedged turn as alive AND is armed, so "
            "lease_is_attributable passes and the daemon trusts it — worse "
            "than the unarmed state #408 set out to fix"
        )
    finally:
        inner.release.set()
        await turn

    # The registry drains: the turn's scope and the delivery's are both gone.
    assert turn_lease.active_liveness_scopes() == 0


@pytest.mark.asyncio
async def test_a_lost_scope_exit_cannot_disable_arming_forever(fed_lease):
    """Why the guard is keyed on the asyncio task and not a depth counter.

    A bare counter is sound for mutual exclusion (asyncio does not preempt
    between statements) but not for recovery: an ``@asynccontextmanager``
    abandoned without ``__aexit__`` — cancellation at a bad moment, GC
    finalisation with no running loop — loses its decrement, and a counter
    stuck above zero silently stops arming EVERY later turn for the life of the
    container. That is the module's own defect reintroduced by its guard.

    Keying on ``asyncio.current_task()`` makes it self-healing: an owner whose
    task is ``done()`` is provably not in a scope and is pruned on next entry.
    """
    clock, _lease, _activity = fed_lease

    async def leaks_a_scope():
        turn_lease._enter_liveness_scope()  # ...and never exits

    await asyncio.create_task(leaks_a_scope())
    assert turn_lease.active_liveness_scopes() == 1, "the leak was not created"

    inner = _ForeignExecutor(clock, lambda c: c.advance(7.0))
    await wrap_executor(inner).execute(SimpleNamespace(), _RecordingQueue())

    # With a counter this is 5007.0 forever: the next turn is never armed.
    assert inner.snapshot["turn_age_seconds"] == pytest.approx(7.0)
    assert turn_lease.active_liveness_scopes() == 0, "the dead owner was not pruned"


def test_main_py_wraps_the_executor_in_the_turn_lease():
    """The ONE production line that delivers #408 to claude-code / codex / hermes.

    Every other test in this file constructs ``TurnLeaseExecutor`` itself, so
    all of them stay green if ``main.py`` stops applying the wrap: reverting
    only that hunk leaves the whole suite passing while the entire fleet runs
    unwrapped. The suite would go on proving the wrapper works and prove
    nothing about it being reached.

    Same technique as ``tests/test_load_config_opt_fallback.py``: inspect the
    source of ``main.main()`` and pin the structural contract. ``main()`` is
    ``# pragma: no cover`` and cannot be executed in a unit test (uvicorn,
    heartbeat, registration), so a source contract is the available coverage.
    """
    import inspect

    from molecule_runtime import main as main_mod

    source = inspect.getsource(main_mod.main)

    create_idx = source.find("executor = await adapter.create_executor(")
    trace_idx = source.find("_tracing.wrap_executor(executor")
    lease_idx = source.find("_turn_lease_executor.wrap_executor(executor)")
    routes_idx = source.find("build_routes(agent_card, executor")

    assert create_idx != -1, (
        "main() no longer builds the executor via adapter.create_executor(); "
        "update this test if the entrypoint was refactored."
    )
    assert lease_idx != -1, (
        "main() is missing `executor = _turn_lease_executor.wrap_executor("
        "executor)`. Without it claude-code, codex and hermes never arm the "
        "turn lease — none of them inherits SubprocessA2AExecutor — and "
        "GET /turn-liveness reports container uptime as the age of every "
        "turn, which is what runtime#408 fixed."
    )
    assert create_idx < lease_idx, (
        "the turn-lease wrap must come AFTER the executor is created."
    )
    assert trace_idx != -1, (
        "main() no longer applies the Langfuse tracing wrap; update this test "
        "if the executor funnel was refactored."
    )
    assert trace_idx < lease_idx, (
        "the turn-lease wrap must be applied OUTSIDE the tracing wrap. It is "
        "unconditional; the tracing wrap is contingent on Langfuse being "
        "configured, and a liveness invariant must not sit inside something "
        "that may not be there."
    )
    assert routes_idx != -1, (
        "main() no longer passes the executor to build_routes(); update this "
        "test if the entrypoint was refactored."
    )
    assert lease_idx < routes_idx, (
        "main() hands the executor to build_routes() BEFORE wrapping it, so "
        "the A2A server would serve the UNWRAPPED executor and the wrap would "
        "be dead code."
    )
