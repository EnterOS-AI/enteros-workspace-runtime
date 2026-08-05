"""Issue #408 — the turn lease must date THE TURN on the subprocess flavours.

Why this exists
---------------
``RuntimeA2AExecutor`` arms the process-global lease at turn start
(``a2a_executor.py``: ``_turn_lease.reset_current()``).  ``SubprocessA2AExecutor``
did not, and neither did any other adapter-supplied executor, so on every
subprocess flavour the lease installed by ``kernel.install()`` at container boot
kept ``_turn_start == _last_touch == boot time`` for the life of the container.

``turn_liveness_snapshot()`` therefore reported, for EVERY turn::

    idle_seconds == turn_age_seconds == container uptime

which is a measurement of nothing: it crosses the TTL once the container has
been up for the TTL and never recovers, and claims ``absolute_cap_exceeded``
about a turn that just started.

The discriminating pair
-----------------------
Arming alone is not the property under test — a lease that is armed but never
touched would report every turn as idle, which is the SAME lie pointed the other
way (and, once the scheduler's ``lease_is_attributable`` gate starts trusting an
attributable lease, a lie that KILLS live turns instead of merely failing to
reap dead ones).  So the suite pins BOTH directions with a pair of cases that
differ in EXACTLY ONE input — whether the in-flight turn emits any activity:

  * :func:`test_wedged_subprocess_turn_reads_idle` — a turn that emits nothing
    must read ``idle_expired`` / ``alive: False``.  This is the capability being
    restored: a genuinely hung turn gets reaped.
  * :func:`test_working_subprocess_turn_reads_alive` — the same turn, same
    clock, same TTL, differing ONLY in that it records tool activity, must read
    ``alive: True``.

A test that only asserted the second would pass on the broken code for the wrong
reason, and one that only asserted the first would pass on the broken code
trivially (there, EVERYTHING reads idle).  Only the pair discriminates.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from molecule_runtime import turn_lease
from molecule_runtime.channel_events import turn_liveness_snapshot
from molecule_runtime.subprocess_executor import SubprocessA2AExecutor

# Container has been up this long before the turn under test starts. Larger than
# both the TTL and the absolute cap below, so a lease that was armed at BOOT
# (the bug) is unmistakably distinguishable from one armed at TURN START.
BOOT_UPTIME_SECONDS = 5000.0
TTL_SECONDS = 10.0
ABSOLUTE_CAP_SECONDS = 100.0


class _FakeClock:
    """Monotonic clock under test control — no sleeps, no wall-clock races.

    ``TurnLease`` takes its clock by injection precisely so liveness can be
    tested deterministically; a test that slept would be timing-dependent and
    would prove nothing about a 900s TTL.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.parts = [{"text": text}]
        self.metadata: dict = {}


class _FakeContext:
    def __init__(self, text: str = "do the thing") -> None:
        self.message = _FakeMessage(text)
        self.request = SimpleNamespace(metadata={"history": []})
        self.metadata = {"history": []}
        self.context_id = "ctx-1"
        self.task_id = "task-1"


class _RecordingQueue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class _ProbingExecutor(SubprocessA2AExecutor):
    """A subprocess runtime whose turn is observed FROM INSIDE.

    ``run_agent`` is the whole turn on a subprocess flavour — the child process
    is running for its entire duration. A trigger daemon probing
    ``GET /turn-liveness`` mid-delivery reads the snapshot at exactly this
    point, so that is where the snapshot under test is taken.
    """

    runtime_label = "Probe"

    def __init__(self, clock: _FakeClock, *, work, **kw) -> None:
        super().__init__(**kw)
        self._clock = clock
        self._work = work
        self.snapshot: dict | None = None
        self.activity_path_in_env: str | None = None

    async def run_agent(self, task_text, session_id, context):
        # What the runtime exported to its subprocess children by the time the
        # child would be spawned. codex/hermes gate their tool-activity ping on
        # this env var being set.
        self.activity_path_in_env = os.environ.get("MOLECULE_TOOL_ACTIVITY_FILE")
        self._work(self._clock)
        self.snapshot = turn_liveness_snapshot()
        return "done"


@pytest.fixture
def clock_and_lease(monkeypatch, tmp_path):
    """A lease armed at BOOT, with the container already ``BOOT_UPTIME_SECONDS`` old.

    Mirrors production: ``kernel.install()`` constructs the lease once at boot,
    so a never-re-armed lease reads container uptime. The activity file is
    pointed at a path that does NOT exist, so an idle case can never be
    vacuously rescued by a stray file another test left behind.
    """
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
    # This flavour HAS a working feed — an earlier turn in this container fed
    # the lease at least once. Without that, `arm_turn_if_fed` deliberately
    # declines to arm (see test_flavour_with_no_feed_is_never_armed), so a
    # fixture that skipped it would be testing the unarmed path by accident.
    lease.touch()
    # The container runs for a while before the turn under test arrives.
    clock.advance(BOOT_UPTIME_SECONDS)
    try:
        yield clock, lease, activity
    finally:
        turn_lease.install(None)


#: Base for synthetic activity-file mtimes. Must be comfortably AHEAD of the
#: real mtime the runtime's own ensure_tool_activity_file() stamps when it
#: creates the file, or the writes below would sit below the baseline watermark
#: and never register — every "working turn" assertion would then pass or fail
#: for reasons having nothing to do with the code under test.
_MTIME_BASE = time.time() + 3600.0


def _record_activity(path, seq: int = 0) -> None:
    """Stand in for a subprocess runtime's tool-call liveness ping.

    Writes the file and stamps an EXPLICIT, strictly increasing mtime rather
    than relying on the filesystem clock: two writes inside one mtime
    granularity would not advance the high-watermark and the feed would
    silently no-op — a vacuous pass.
    """
    path.write_text("1")
    at = _MTIME_BASE + seq
    os.utime(path, (at, at))


# --------------------------------------------------------------------------- #
# 1. The lease must date THE TURN, not the container.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_subprocess_turn_age_dates_the_turn_not_the_container(clock_and_lease):
    clock, _lease, _activity = clock_and_lease

    def work(c):
        c.advance(3.0)  # the turn has been running 3s when the daemon probes

    ex = _ProbingExecutor(clock, work=work, workspace_id="ws-408")
    await ex.execute(_FakeContext(), _RecordingQueue())

    snap = ex.snapshot
    assert snap is not None and snap["lease"] is True
    # THE ASSERTION. Pre-fix this is BOOT_UPTIME_SECONDS + 3 == 5003.0.
    assert snap["turn_age_seconds"] == pytest.approx(3.0), (
        "turn_age_seconds must date the in-flight turn, not container uptime"
    )
    # A 3s-old turn is nowhere near the 100s absolute cap.
    assert snap["absolute_cap_exceeded"] is False


# --------------------------------------------------------------------------- #
# 2. NEGATIVE CONTROL — a wedged turn must STILL read idle.
#    (identical to #3 except the turn emits nothing)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wedged_subprocess_turn_reads_idle(clock_and_lease):
    clock, _lease, _activity = clock_and_lease

    def work(c):
        # The child is "running" but produces NOTHING for 3x the TTL.
        c.advance(TTL_SECONDS * 3)

    ex = _ProbingExecutor(clock, work=work, workspace_id="ws-408")
    await ex.execute(_FakeContext(), _RecordingQueue())

    snap = ex.snapshot
    assert snap is not None and snap["lease"] is True
    # The restored capability: silence is detected as idle.
    assert snap["idle_seconds"] == pytest.approx(TTL_SECONDS * 3)
    assert snap["idle_expired"] is True
    assert snap["alive"] is False
    # ...and it is attributed to THIS turn, not to the container. Pre-fix this
    # reads 5030.0, which is ALSO "expired" — but for the wrong reason, and it
    # would read expired for a turn that had just started.
    assert snap["turn_age_seconds"] == pytest.approx(TTL_SECONDS * 3), (
        "a wedged turn must be dated from ITS OWN start"
    )


# --------------------------------------------------------------------------- #
# 3. NEGATIVE CONTROL — the SAME turn that DOES work must read alive.
#    Exactly one input differs from #2: the turn records tool activity.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_working_subprocess_turn_reads_alive(clock_and_lease):
    clock, _lease, activity = clock_and_lease

    def work(c):
        # Same total duration as the wedged case (6 x TTL/2 == 3 x TTL), but the
        # child bumps the tool-activity file as it churns — a real tool call
        # every TTL/2, i.e. never a full TTL of silence.
        for i in range(6):
            _record_activity(activity, i)
            c.advance(TTL_SECONDS / 2)

    ex = _ProbingExecutor(clock, work=work, workspace_id="ws-408")
    await ex.execute(_FakeContext(), _RecordingQueue())

    snap = ex.snapshot
    assert snap is not None and snap["lease"] is True
    # Fresh activity => not idle. The last ping was TTL/2 ago, inside the TTL.
    assert snap["idle_seconds"] <= TTL_SECONDS
    assert snap["idle_expired"] is False
    # Dated to THIS turn, so the 100s absolute cap is nowhere near. Pre-fix
    # turn_age is 5030s, the cap reads exceeded, and `alive` is False for a turn
    # that is demonstrably working — the false verdict issue #408 describes.
    assert snap["turn_age_seconds"] == pytest.approx(TTL_SECONDS * 3)
    assert snap["absolute_cap_exceeded"] is False
    assert snap["alive"] is True


# --------------------------------------------------------------------------- #
# 4. The tool-activity path must be EXPORTED before the child is spawned.
#    codex/hermes gate their ping on this env var; it was only ever exported by
#    a2a_executor, which never runs on a subprocess flavour — so their tier-C
#    feed was a permanent no-op.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_activity_path_is_exported_to_the_child(clock_and_lease, monkeypatch):
    clock, _lease, _activity = clock_and_lease
    # Unset it: the runtime must MATERIALIZE and export a path itself, exactly
    # as ensure_tool_activity_file() does on the native path.
    monkeypatch.delenv("MOLECULE_TOOL_ACTIVITY_FILE", raising=False)

    ex = _ProbingExecutor(clock, work=lambda c: None, workspace_id="ws-408")
    await ex.execute(_FakeContext(), _RecordingQueue())

    assert ex.activity_path_in_env, (
        "MOLECULE_TOOL_ACTIVITY_FILE must be exported before run_agent spawns "
        "the child, or the subprocess tier-C liveness ping is a no-op"
    )


# --------------------------------------------------------------------------- #
# 5. THE SAFETY PROPERTY. A flavour with NO activity feed must NOT be armed.
#
#    Arming is what makes a consumer BELIEVE the lease (the scheduler's
#    `lease_is_attributable` gate passes as soon as turn_age < elapsed). A
#    flavour that never touches the lease — claude-code today — would then be
#    reported idle from the instant its turn started, and a twenty-minute
#    claude-code turn would be cancelled at the TTL. That is a false kill the
#    fix would have INVENTED, so the unarmed/"no signal" state is retained
#    exactly where it is still the honest answer.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_flavour_with_no_feed_is_never_armed(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(tmp_path / "absent"))
    clock = _FakeClock(0.0)
    lease = turn_lease.TurnLease(
        ttl_seconds=TTL_SECONDS,
        absolute_cap_seconds=ABSOLUTE_CAP_SECONDS,
        clock=clock,
    )
    turn_lease.install(lease)
    # NOTE: no lease.touch() here — this flavour has never fed the lease. That
    # is the ONLY difference from the fixture used by every case above.
    clock.advance(BOOT_UPTIME_SECONDS)
    try:
        ex = _ProbingExecutor(clock, work=lambda c: c.advance(3.0), workspace_id="ws-408")
        await ex.execute(_FakeContext(), _RecordingQueue())

        snap = ex.snapshot
        assert snap is not None and snap["lease"] is True
        # Still reads container uptime — and that is CORRECT here: it is what
        # makes the daemon's attributability gate reject the snapshot and fall
        # back to its own ceiling, instead of trusting a lease nothing feeds.
        assert snap["turn_age_seconds"] == pytest.approx(BOOT_UPTIME_SECONDS + 3.0)
    finally:
        turn_lease.install(None)


@pytest.mark.asyncio
async def test_a_feed_earns_arming_for_the_next_turn(monkeypatch, tmp_path):
    """The gate is empirical and self-healing, not a hardcoded flavour list.

    A runtime that starts feeding the lease — because its template gained a
    tool-activity ping, or because the exported path finally made its existing
    ping work — begins being armed on its very next turn, with no change here.
    """
    activity = tmp_path / "tool_activity"
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(activity))
    clock = _FakeClock(0.0)
    lease = turn_lease.TurnLease(
        ttl_seconds=TTL_SECONDS,
        absolute_cap_seconds=ABSOLUTE_CAP_SECONDS,
        clock=clock,
    )
    turn_lease.install(lease)
    clock.advance(BOOT_UPTIME_SECONDS)
    try:
        # Turn 1: the runtime records a tool call. Not armed (nothing had been
        # observed when it started) — but the feed is now proven.
        turn1 = _ProbingExecutor(
            clock,
            work=lambda c: (_record_activity(activity, 0), c.advance(2.0)),
            workspace_id="ws-408",
        )
        await turn1.execute(_FakeContext(), _RecordingQueue())
        assert turn1.snapshot["turn_age_seconds"] > BOOT_UPTIME_SECONDS

        # Turn 2: armed, because turn 1 demonstrated a working feed.
        turn2 = _ProbingExecutor(clock, work=lambda c: c.advance(4.0), workspace_id="ws-408")
        await turn2.execute(_FakeContext(), _RecordingQueue())
        assert turn2.snapshot["turn_age_seconds"] == pytest.approx(4.0)
    finally:
        turn_lease.install(None)


# --------------------------------------------------------------------------- #
# 6. Kernel OFF must stay byte-identical: no lease, no snapshot, no crash.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_kernel_off_subprocess_turn_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(tmp_path / "absent"))
    turn_lease.install(None)
    clock = _FakeClock(0.0)
    ex = _ProbingExecutor(clock, work=lambda c: None, workspace_id="ws-408")
    q = _RecordingQueue()
    await ex.execute(_FakeContext(), q)

    assert ex.snapshot == {
        "lease": False,
        "reason": "no turn lease is installed (mailbox kernel off)",
    }
    assert len(q.events) == 1  # the reply still flows
