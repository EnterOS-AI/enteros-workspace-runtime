"""The trigger lane's turn-liveness read — the ONE honest in-flight signal.

Why this exists
---------------
A trigger daemon fires a self-turn over the local A2A lane and then blocks until
the turn completes.  While it waits it must be able to tell a WORKING agent from
a wedged one.  Elapsed wall-clock cannot tell them apart, so the scheduler used
to abandon every delivery at a fixed 600s and record ``status: unknown`` — on a
live client agent that was ~40% of all scheduled work.

The runtime already owns the honest answer: :class:`turn_lease.TurnLease`, which
is *touched* on every tool call, expires only after an idle TTL with no touch,
and carries an un-bypassable absolute cap measured from turn start.  These tests
pin that this lane REPORTS that object rather than growing a second, drifting
liveness notion — and that a working turn past the old 600s wall reads alive.
"""
from __future__ import annotations

import os
import socket as _socket

import httpx
import pytest

from molecule_runtime import turn_lease
from molecule_runtime.channel_events import (
    TRIGGER_A2A_SOCKET_ENV,
    TRIGGER_A2A_TOKEN_ENV,
    TRIGGER_LIVENESS_PATH,
    ChannelEventSocketManager,
    RuntimeStampedChannelProvenance,
    _stamp_trigger_source,
    turn_liveness_snapshot,
)
from molecule_runtime.channel_sdk import (
    CHANNEL_CAPABILITY_HEADER,
    probe_trigger_liveness,
)
from molecule_runtime.plugin_daemons import DaemonSpec

TOKEN = "trigger-capability-token"


@pytest.fixture(autouse=True)
def _isolated_lease(monkeypatch, tmp_path):
    """Every case installs its OWN lease and its OWN (absent) activity file.

    Pointing ``MOLECULE_TOOL_ACTIVITY_FILE`` at a path that does not exist keeps
    the source-C feed a no-op, so an idle case cannot be silently rescued by a
    stray activity file another test left behind — which would make the idle
    assertions below vacuously pass.
    """
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(tmp_path / "absent"))
    for name in (
        "MOLECULE_MAX_TURN_SECONDS",
        "MOLECULE_TURN_LEASE_TTL_SECONDS",
        "A2A_COMPLETION_IDLE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    turn_lease.install(None)
    yield
    turn_lease.install(None)


# ---------------------------------------------------------------------------
# the snapshot itself — it must be the lease, not a re-implementation
# ---------------------------------------------------------------------------


def test_no_lease_installed_reports_no_signal_not_death():
    """Kernel off -> "no signal". Reporting "dead" would recreate the kill."""
    snapshot = turn_liveness_snapshot()
    assert snapshot["lease"] is False
    assert "reason" in snapshot
    # Crucially it does NOT claim the turn is finished/idle.
    assert "alive" not in snapshot


def test_a_freshly_armed_turn_reads_alive():
    turn_lease.install(turn_lease.TurnLease(ttl_seconds=900.0))
    snapshot = turn_liveness_snapshot()
    assert snapshot["lease"] is True
    assert snapshot["alive"] is True
    assert snapshot["idle_expired"] is False
    assert snapshot["absolute_cap_exceeded"] is False
    assert snapshot["ttl_seconds"] == 900.0
    assert snapshot["absolute_cap_seconds"] == 3600.0  # 4 x the 900s idle cap


def test_a_turn_still_emitting_tool_activity_past_600s_reads_alive():
    """THE DEFECT, at the source: 50 minutes of real work is still alive.

    A fake clock drives the lease so the assertion is about the LEASE's rule
    (idle since last touch), not about the test sleeping for 50 minutes.
    """
    now = {"t": 0.0}
    lease = turn_lease.TurnLease(ttl_seconds=900.0, clock=lambda: now["t"])
    turn_lease.install(lease)
    # 3000s of steady tool activity: a touch every 60s, exactly what an agent
    # running tools produces.
    for _ in range(50):
        now["t"] += 60.0
        lease.touch()
    snapshot = turn_liveness_snapshot()
    assert snapshot["turn_age_seconds"] == 3000.0   # far past the old 600s wall
    assert snapshot["idle_seconds"] == 0.0
    assert snapshot["alive"] is True                # ... and still working


def test_a_turn_that_went_quiet_reads_idle_expired():
    """Negative control for the case above: same turn age, no touches."""
    now = {"t": 0.0}
    lease = turn_lease.TurnLease(ttl_seconds=900.0, clock=lambda: now["t"])
    turn_lease.install(lease)
    now["t"] += 3000.0            # 3000s elapsed, NOT ONE tool call
    snapshot = turn_liveness_snapshot()
    assert snapshot["turn_age_seconds"] == 3000.0   # identical to the test above
    assert snapshot["idle_expired"] is True         # the ONLY difference
    assert snapshot["alive"] is False


def test_activity_cannot_buy_time_past_the_absolute_cap():
    """The un-bypassable backstop: touching forever does not extend the cap."""
    now = {"t": 0.0}
    lease = turn_lease.TurnLease(ttl_seconds=900.0, clock=lambda: now["t"])
    turn_lease.install(lease)
    for _ in range(70):           # 4200s of continuous activity
        now["t"] += 60.0
        lease.touch()
    snapshot = turn_liveness_snapshot()
    assert snapshot["idle_expired"] is False        # never idle
    assert snapshot["absolute_cap_exceeded"] is True
    assert snapshot["alive"] is False


# ---------------------------------------------------------------------------
# the ASGI binding — capability-gated, trigger-lane only
# ---------------------------------------------------------------------------


async def _get_liveness(app, *, token: str | None):
    headers = []
    if token is not None:
        headers.append((CHANNEL_CAPABILITY_HEADER.encode("ascii"), token.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": TRIGGER_LIVENESS_PATH,
        "headers": headers,
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _binding(*, trigger: bool, downstream=None):
    async def _unreachable(scope, receive, send):
        if downstream is not None:
            downstream.append(scope["path"])
            return
        raise AssertionError("request must not reach the wrapped app")

    return RuntimeStampedChannelProvenance(
        _unreachable,
        "molecule-scheduler",
        TOKEN,
        stamp=_stamp_trigger_source,
        serves_turn_liveness=trigger,
    )


@pytest.mark.asyncio
async def test_trigger_binding_serves_the_snapshot_to_a_valid_capability():
    turn_lease.install(turn_lease.TurnLease(ttl_seconds=900.0))
    sent = await _get_liveness(_binding(trigger=True), token=TOKEN)
    assert sent[0]["status"] == 200
    import json

    payload = json.loads(sent[1]["body"])
    assert payload["lease"] is True
    assert payload["alive"] is True


@pytest.mark.asyncio
async def test_liveness_read_is_capability_gated():
    """A daemon that cannot fire a turn cannot inspect one either."""
    turn_lease.install(turn_lease.TurnLease(ttl_seconds=900.0))
    for bad in (None, "", "not-the-token"):
        sent = await _get_liveness(_binding(trigger=True), token=bad)
        assert sent[0]["status"] == 401, bad


@pytest.mark.asyncio
async def test_channel_binding_does_not_expose_turn_liveness():
    """A channel bridges an EXTERNAL party; it gets no window into the turn."""
    turn_lease.install(turn_lease.TurnLease(ttl_seconds=900.0))
    seen: list[str] = []
    await _get_liveness(_binding(trigger=False, downstream=seen), token=TOKEN)
    # Not served here — delegated to the wrapped app exactly as before.
    assert seen == [TRIGGER_LIVENESS_PATH]


# ---------------------------------------------------------------------------
# end to end over the REAL private Unix socket, via the SDK client
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"), reason="private lane requires AF_UNIX"
)
@pytest.mark.asyncio
async def test_probe_client_reads_the_lease_over_the_private_socket():
    """No fakes: real manager, real UDS, real vendored client, real lease."""
    from starlette.applications import Starlette

    app = Starlette(routes=[])
    spec = DaemonSpec(
        name="scheduler",
        plugin="molecule-scheduler",
        kind="trigger",
        command=["does-not-spawn-in-this-test"],
        env={},
    )
    manager = ChannelEventSocketManager(app, [spec], startup_timeout_seconds=5)
    now = {"t": 0.0}
    lease = turn_lease.TurnLease(ttl_seconds=900.0, clock=lambda: now["t"])
    turn_lease.install(lease)
    try:
        assert await manager.start() is True
        socket_path = spec.env[TRIGGER_A2A_SOCKET_ENV]
        assert os.path.exists(socket_path)

        # A working turn, 50 minutes in: past the old 600s wall, still alive.
        for _ in range(50):
            now["t"] += 60.0
            lease.touch()
        snapshot = await probe_trigger_liveness(environ=spec.env)
        assert snapshot is not None
        assert snapshot["alive"] is True
        assert snapshot["turn_age_seconds"] == 3000.0

        # The same turn goes quiet -> the SAME probe now reports idle.
        now["t"] += 1000.0
        snapshot = await probe_trigger_liveness(environ=spec.env)
        assert snapshot["alive"] is False
        assert snapshot["idle_expired"] is True

        # A forged capability is refused at the socket, and the client reports
        # "no signal" rather than inventing a verdict.
        bad_env = dict(spec.env)
        bad_env[TRIGGER_A2A_TOKEN_ENV] = "forged"
        assert await probe_trigger_liveness(environ=bad_env) is None
    finally:
        await manager.stop()


@pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"), reason="private lane requires AF_UNIX"
)
@pytest.mark.asyncio
async def test_probe_reports_no_signal_against_a_host_without_the_contract():
    """An older runtime 404s the path; that is "no signal", never an error."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _root(request):  # pragma: no cover - never called here
        return JSONResponse({})

    app = Starlette(routes=[Route("/", _root, methods=["POST"])])
    spec = DaemonSpec(
        name="scheduler",
        plugin="molecule-scheduler",
        kind="trigger",
        command=["does-not-spawn-in-this-test"],
        env={},
    )
    manager = ChannelEventSocketManager(app, [spec], startup_timeout_seconds=5)
    try:
        assert await manager.start() is True
        # Simulate the pre-contract host by disabling the handler on the live
        # binding: the GET then falls through to an app that has no such route.
        for wrapped in manager._servers.values():
            wrapped.config.app._serves_turn_liveness = False
        assert await probe_trigger_liveness(environ=spec.env) is None
    finally:
        await manager.stop()


def test_probe_is_time_bounded_even_though_delivery_is_not():
    """The probe must never become the thing that wedges."""
    from molecule_runtime import channel_sdk

    assert channel_sdk.TRIGGER_LIVENESS_PROBE_TIMEOUT_SECONDS == 5.0
    bounded = channel_sdk._lane_timeout(
        channel_sdk.TRIGGER_LIVENESS_PROBE_TIMEOUT_SECONDS
    )
    assert bounded.read == 5.0
    # ... while a trigger DELIVERY has no read deadline at all.
    unbounded = channel_sdk._lane_timeout(None)
    assert unbounded.read is None
    assert unbounded.connect == 5.0
    assert isinstance(unbounded, httpx.Timeout)
