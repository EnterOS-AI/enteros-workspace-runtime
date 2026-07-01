"""Turn lease (MUST-FIX 1) — liveness signal for an in-flight autonomous turn.

Pins:
  1. touch()/expired() semantics with a fake clock;
  2. TTL is PINNED to the executor idle-cap by default, override-able;
  3. the activity-file feeder (source C) touches only when mtime advances;
  4. the process-global install/current/touch_current helpers are no-ops when
     no lease is installed — so the touches added to a2a_executor change
     NOTHING in the default (kernel-off) flow.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import turn_lease as tl  # noqa: E402


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _clear_global():
    tl.install(None)
    yield
    tl.install(None)


def test_fresh_lease_is_not_expired():
    lease = tl.TurnLease(ttl_seconds=10.0)
    assert not lease.expired()
    assert lease.age() >= 0.0


def test_expires_after_ttl_and_touch_resets():
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, clock=clk)
    assert not lease.expired()
    clk.advance(4.9)
    assert not lease.expired(), "still within TTL"
    clk.advance(0.2)
    assert lease.expired(), "past TTL with no touch"
    lease.touch()
    assert not lease.expired(), "touch resets the expiry clock"
    clk.advance(5.1)
    assert lease.expired()


def test_reset_is_touch_alias():
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=2.0, clock=clk)
    clk.advance(3.0)
    assert lease.expired()
    lease.reset()
    assert not lease.expired()


def test_default_ttl_pinned_to_idle_cap(monkeypatch):
    monkeypatch.delenv(tl.LEASE_TTL_ENV, raising=False)
    monkeypatch.setenv(tl.IDLE_CAP_ENV, "900")
    assert tl.default_ttl_seconds() == 900.0
    # Explicit override wins over the idle-cap.
    monkeypatch.setenv(tl.LEASE_TTL_ENV, "123")
    assert tl.default_ttl_seconds() == 123.0
    # A zero-ish / bad idle-cap is floored at 1s so a mis-set can't make every
    # turn instantly expired.
    monkeypatch.delenv(tl.LEASE_TTL_ENV, raising=False)
    monkeypatch.setenv(tl.IDLE_CAP_ENV, "0")
    assert tl.default_ttl_seconds() == 1.0
    monkeypatch.setenv(tl.IDLE_CAP_ENV, "not-a-number")
    assert tl.default_ttl_seconds() == 900.0


def test_default_constructor_uses_idle_cap(monkeypatch):
    monkeypatch.delenv(tl.LEASE_TTL_ENV, raising=False)
    monkeypatch.setenv(tl.IDLE_CAP_ENV, "42")
    lease = tl.TurnLease()
    assert lease.ttl_seconds == 42.0


def test_feed_from_activity_file_touches_on_mtime_advance(tmp_path):
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, clock=clk)
    f = tmp_path / "activity"
    # Missing file: no-op.
    assert lease.feed_from_activity_file(str(f)) is False
    f.write_text("1")
    import os

    os.utime(f, (2000.0, 2000.0))
    assert lease.feed_from_activity_file(str(f)) is True, "first observed mtime touches"
    # Same mtime again: no touch.
    assert lease.feed_from_activity_file(str(f)) is False
    # Advance mtime: touches again.
    os.utime(f, (2001.0, 2001.0))
    assert lease.feed_from_activity_file(str(f)) is True


def test_global_helpers_are_noops_without_install():
    # No lease installed => every helper is a cheap no-op (the default flow).
    assert tl.current() is None
    tl.touch_current()  # must not raise
    tl.reset_current()  # must not raise
    assert tl.feed_from_activity_file("/nonexistent/path") is False


def test_install_and_touch_current():
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, clock=clk)
    tl.install(lease)
    assert tl.current() is lease
    clk.advance(6.0)
    assert lease.expired()
    tl.touch_current()
    assert not lease.expired(), "touch_current touched the installed lease"


# ── MUST-FIX 1 stall gate: expired() is CONSUMED to end a stalled turn ──────


def test_turn_is_alive_despite_idle_no_lease_kernel_off():
    """Kernel OFF (no lease installed): the idle-cap timeout is ALWAYS a stall,
    so the executor raises exactly as before — byte-identical."""
    assert tl.current() is None
    assert tl.turn_is_alive_despite_idle("/no/such/activity") is False


def test_turn_is_alive_despite_idle_live_subprocess(tmp_path):
    """A subprocess that bumped MOLECULE_TOOL_ACTIVITY_FILE within the TTL keeps
    the turn ALIVE past the idle-cap: expired lease is refreshed by the feed."""
    import os

    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, clock=clk)
    tl.install(lease)
    f = tmp_path / "activity"
    f.write_text("1")
    os.utime(f, (3000.0, 3000.0))
    clk.advance(6.0)  # age the lease PAST the TTL
    assert lease.expired(), "precondition: lease is stale before the feed"
    # The activity file's mtime is newer than the lease has seen -> the source-C
    # feed inside turn_is_alive_despite_idle touches the lease -> turn is alive.
    assert tl.turn_is_alive_despite_idle(str(f)) is True
    assert not lease.expired()


def test_turn_is_alive_despite_idle_stalled_ends_turn(tmp_path):
    """A genuinely stalled turn (no tool activity since the last touch) stays
    expired: turn_is_alive_despite_idle -> False -> executor ENDS the turn."""
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, clock=clk)
    tl.install(lease)
    f = tmp_path / "activity"  # never created -> feed is a no-op
    clk.advance(6.0)
    assert lease.expired()
    assert tl.turn_is_alive_despite_idle(str(f)) is False, "no activity -> stall"


@pytest.mark.asyncio
async def test_watch_activity_file_refreshes_lease_on_touch(tmp_path):
    """MUST-FIX 1/3: the background watcher refreshes the installed lease when
    MOLECULE_TOOL_ACTIVITY_FILE's mtime advances, and only then. Stops on the
    stop event."""
    import asyncio
    import os

    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=100.0, clock=clk)
    tl.install(lease)
    f = tmp_path / "activity"
    f.write_text("1")
    os.utime(f, (5000.0, 5000.0))
    stop = asyncio.Event()
    task = asyncio.create_task(
        tl.watch_activity_file(str(f), interval=0.005, stop_event=stop)
    )
    try:
        # Let the watcher poll and observe the initial mtime (touches once).
        await asyncio.sleep(0.05)
        # Age the lease PAST the TTL with NO file advance: subsequent polls see
        # no mtime change -> no touch -> the lease stays expired.
        clk.advance(200.0)
        await asyncio.sleep(0.05)
        assert lease.expired(), "watcher must NOT refresh when the file is static"
        # Now advance the file's mtime: the next poll refreshes the lease.
        os.utime(f, (5001.0, 5001.0))
        await asyncio.sleep(0.05)
        assert not lease.expired(), "watcher refreshes the lease on an mtime bump"
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_watch_activity_file_noop_when_kernel_off(tmp_path):
    """No lease installed (kernel OFF): the watcher returns immediately and does
    not spin — the default flow never runs a watcher (byte-identical)."""
    import asyncio

    assert tl.current() is None
    # Must return promptly even with a large interval, because there is no lease.
    await asyncio.wait_for(
        tl.watch_activity_file(str(tmp_path / "activity"), interval=999.0),
        timeout=1.0,
    )


# ── Absolute wall-clock cap (activity-file liveness bypass fix) ──────────────


def test_absolute_cap_default_is_multiple_of_ttl(monkeypatch):
    """Default absolute cap = _ABSOLUTE_CAP_IDLE_MULTIPLE × the lease TTL;
    MOLECULE_MAX_TURN_SECONDS overrides it (honored as-given for tests)."""
    monkeypatch.delenv(tl.MAX_TURN_ENV, raising=False)
    lease = tl.TurnLease(ttl_seconds=10.0)
    assert lease.absolute_cap_seconds == tl._ABSOLUTE_CAP_IDLE_MULTIPLE * 10.0
    monkeypatch.setenv(tl.MAX_TURN_ENV, "37")
    assert tl.TurnLease(ttl_seconds=10.0).absolute_cap_seconds == 37.0
    # default_absolute_cap_seconds() tracks the env-derived TTL.
    monkeypatch.delenv(tl.MAX_TURN_ENV, raising=False)
    monkeypatch.delenv(tl.LEASE_TTL_ENV, raising=False)
    monkeypatch.setenv(tl.IDLE_CAP_ENV, "100")
    assert tl.default_absolute_cap_seconds() == tl._ABSOLUTE_CAP_IDLE_MULTIPLE * 100.0


def test_touch_does_not_reset_absolute_cap_origin():
    """touch() advances the idle clock but NEVER the turn-start origin, so the
    absolute cap is measured from turn start regardless of how often the
    activity file is touched. This is the property that closes the bypass."""
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, absolute_cap_seconds=20.0, clock=clk)
    lease.reset()  # arm turn-start at t=1000
    clk.advance(10.0)
    lease.touch()  # idle clock reset...
    assert lease.turn_age() == 10.0
    assert not lease.expired(), "just touched -> idle clock is fresh"
    assert not lease.absolute_cap_exceeded(), "10 < 20"
    clk.advance(11.0)
    lease.touch()  # ...STILL touching
    assert not lease.expired(), "idle clock fresh despite the wall-clock passing"
    assert lease.absolute_cap_exceeded(), "21 > 20 -> capped despite the touch"
    # reset() (a NEW turn) is the only thing that moves the origin.
    lease.reset()
    assert not lease.absolute_cap_exceeded()


def test_absolute_cap_ends_turn_despite_continuous_activity(tmp_path, monkeypatch):
    """Bypass closed: a turn whose activity file is CONTINUOUSLY touched (lease
    never idle-expires) is STILL ended once the absolute wall-clock cap since
    turn-start is exceeded — via the executor's turn_is_alive_despite_idle gate."""
    import os

    monkeypatch.setenv(tl.MAX_TURN_ENV, "30")  # absolute cap = 30s
    clk = _FakeClock()
    lease = tl.TurnLease(ttl_seconds=5.0, clock=clk)  # idle TTL 5s
    tl.install(lease)
    lease.reset()  # arm turn-start at t=1000
    f = tmp_path / "activity"
    f.write_text("1")
    mt = 3000.0
    ended_by_cap = False
    for _ in range(40):
        clk.advance(2.0)         # 2s of wall-clock elapse per loop
        mt += 1.0
        os.utime(f, (mt, mt))    # CONTINUOUS activity — bump the file every step
        alive = tl.turn_is_alive_despite_idle(str(f))
        # The idle clock is always fresh (we just touched the file), so WITHOUT
        # the absolute cap this loop would report alive forever.
        assert not lease.expired(), "continuous touches keep the idle clock fresh"
        if lease.turn_age() > 30.0:
            assert alive is False, "absolute cap must end the turn despite activity"
            ended_by_cap = True
            break
        assert alive is True, "within the cap + fresh activity -> still alive"
    assert ended_by_cap, "the turn must eventually be ended by the absolute cap"
