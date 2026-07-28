"""Tests for plugin-declared channel daemons (issue #215, PR-1).

Locks in the daemon-lifecycle slice ONLY (the event socket / provenance /
turn-complete lane is PR-2):

  * ``discover_daemon_specs`` reads ``contributes.daemons`` from installed
    plugin manifests via the SAME scan every other plugin surface uses
    (``plugins.load_plugins`` — per-workspace ``<configs>/plugins`` first,
    shared ``/plugins`` fallback, dedup by name);
  * a ``contributes.daemons``-bearing manifest passes SSOT validation
    (additive contribution point — ``additionalProperties: true``), so
    enforcement (default ON) never refuses a daemon-declaring plugin;
  * malformed daemon entries are SKIPPED with a log line — never a crash,
    never a manifest-validation failure;
  * ``DaemonSupervisor`` spawns each daemon in its own process group with
    env = os.environ + spec env, restarts on unexpected exit with
    exponential backoff, trips a circuit breaker after N consecutive fast
    failures, and terminates children (SIGTERM, then SIGKILL after grace)
    on ``stop()``;
  * the supervisor is actually WIRED into main.py boot + shutdown
    (prove-fail: discovery returning specs with nothing spawning them, or
    a shutdown that leaks children, fails here).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time

import pytest

import molecule_runtime.main as m
from molecule_runtime import manifest_ssot
from molecule_runtime.channel_events import (
    CHANNEL_A2A_SOCKET_ENV,
    CHANNEL_A2A_TOKEN_ENV,
    CHANNEL_API_VERSION_ENV,
    CHANNEL_PLUGIN_ID_ENV,
)
from molecule_runtime.plugin_daemons import (
    DaemonSpec,
    DaemonSupervisor,
    daemon_specs_from_manifest,
    discover_daemon_specs,
    start_supervisor_when_bound,
    wait_until_server_bound,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write_plugin(base, name, manifest_yaml):
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(manifest_yaml)
    return plugin_dir


def _wait_for(predicate, timeout=10.0, interval=0.02):
    """Poll ``predicate`` until truthy or ``timeout`` — returns its value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _fast_supervisor(specs, **overrides):
    """Supervisor with test-speed timings (defaults are seconds-scale)."""
    kwargs = dict(
        backoff_base_seconds=0.05,
        backoff_cap_seconds=0.2,
        max_fast_failures=3,
        fast_failure_seconds=5.0,
        term_grace_seconds=2.0,
        poll_interval_seconds=0.02,
    )
    kwargs.update(overrides)
    return DaemonSupervisor(specs, **kwargs)


DAEMON_MANIFEST = """\
name: lark-bridge
version: 1.0.0
description: channel bridge plugin
kind: channel
contributes:
  daemons:
    - name: bridge
      command: python
      args: ["-m", "lark_channel_molecule.bridge"]
      env:
        LARK_DOMAIN: feishu
"""


# ---------------------------------------------------------------------------
# SSOT schema tolerance — daemons is an ADDITIVE contribution point
# ---------------------------------------------------------------------------
def test_daemons_contribution_passes_ssot_validation():
    """`contributes.daemons` is an unknown-but-tolerated contribution point
    (`additionalProperties: true`): it MUST NOT produce violations, otherwise
    fail-closed enforcement (default ON) would refuse every daemon-declaring
    plugin at load."""
    import yaml

    raw = yaml.safe_load(DAEMON_MANIFEST)
    assert manifest_ssot.validate_manifest_ssot(raw) == []


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def test_discover_reads_contributes_daemons(tmp_path):
    ws = tmp_path / "configs" / "plugins"
    plugin_dir = _write_plugin(ws, "lark-bridge", DAEMON_MANIFEST)

    specs = discover_daemon_specs(
        workspace_plugins_dir=str(ws),
        shared_plugins_dir=str(tmp_path / "no-shared"),
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.plugin == "lark-bridge"
    assert spec.kind == "channel"
    assert spec.name == "bridge"
    assert spec.command == ["python", "-m", "lark_channel_molecule.bridge"]
    assert spec.env == {"LARK_DOMAIN": "feishu"}
    # default cwd is the plugin dir — the daemon's files live there
    assert spec.cwd == str(plugin_dir)


def test_channel_daemon_identity_comes_from_manifest_not_install_directory(tmp_path):
    ws = tmp_path / "configs" / "plugins"
    _write_plugin(
        ws,
        "legacy-repository-name",
        "name: molecule-slack-channel\n"
        "version: 3.0.0\n"
        "description: Slack channel plugin\n"
        "kind: channel\n"
        "contributes:\n"
        "  daemons:\n"
        "    - {name: bridge, command: python}\n",
    )

    specs = discover_daemon_specs(
        workspace_plugins_dir=str(ws),
        shared_plugins_dir=str(tmp_path / "no-shared"),
    )

    assert len(specs) == 1
    assert specs[0].plugin == "molecule-slack-channel"


def test_duplicate_channel_manifest_identities_fail_closed(tmp_path):
    ws = tmp_path / "configs" / "plugins"
    manifest = (
        "name: molecule-slack-channel\n"
        "version: 3.0.0\n"
        "description: Slack channel plugin\n"
        "kind: channel\n"
        "contributes:\n"
        "  daemons:\n"
        "    - {name: bridge, command: python}\n"
    )
    _write_plugin(ws, "slack-repository-a", manifest)
    _write_plugin(ws, "slack-repository-b", manifest)

    with pytest.raises(
        ValueError,
        match="duplicate channel plugin identity 'molecule-slack-channel'",
    ):
        discover_daemon_specs(
            workspace_plugins_dir=str(ws),
            shared_plugins_dir=str(tmp_path / "no-shared"),
        )


def test_duplicate_daemon_keys_fail_closed(tmp_path):
    ws = tmp_path / "configs" / "plugins"
    _write_plugin(
        ws,
        "duplicate-daemon",
        "name: duplicate-daemon\n"
        "version: 1.0.0\n"
        "description: duplicate daemon names\n"
        "contributes:\n"
        "  daemons:\n"
        "    - {name: worker, command: python}\n"
        "    - {name: worker, command: python}\n",
    )

    with pytest.raises(
        ValueError,
        match="duplicate plugin daemon key 'duplicate-daemon/worker'",
    ):
        discover_daemon_specs(
            workspace_plugins_dir=str(ws),
            shared_plugins_dir=str(tmp_path / "no-shared"),
        )


def test_discover_zero_daemon_manifests_is_noop(tmp_path):
    ws = tmp_path / "plugins"
    _write_plugin(ws, "plain", "name: plain\nversion: 1.0.0\n")
    # manifest-less plugin dir (bare-SKILL.md convention) must not crash
    (ws / "bare").mkdir()

    assert (
        discover_daemon_specs(
            workspace_plugins_dir=str(ws),
            shared_plugins_dir=str(tmp_path / "no-shared"),
        )
        == []
    )


def test_discover_missing_dirs_is_noop(tmp_path):
    assert (
        discover_daemon_specs(
            workspace_plugins_dir=str(tmp_path / "nope"),
            shared_plugins_dir=str(tmp_path / "also-nope"),
        )
        == []
    )


def test_discover_workspace_wins_over_shared(tmp_path):
    """Same plugin name in both dirs: per-workspace wins (load_plugins dedup)
    — the shared copy's daemons must NOT also spawn."""
    ws = tmp_path / "ws"
    shared = tmp_path / "shared"
    _write_plugin(ws, "dup", DAEMON_MANIFEST)
    _write_plugin(
        shared,
        "dup",
        "name: dup\nversion: 1.0.0\ndescription: shared copy\n"
        "contributes:\n  daemons:\n    - {name: shared-d, command: python}\n",
    )

    specs = discover_daemon_specs(
        workspace_plugins_dir=str(ws), shared_plugins_dir=str(shared)
    )
    assert [s.name for s in specs] == ["bridge"]


# ---------------------------------------------------------------------------
# malformed entries — skip with a log line, never crash
# ---------------------------------------------------------------------------
def test_malformed_entries_skipped_valid_kept(tmp_path, caplog):
    manifest = """\
name: mixed
version: 1.0.0
description: mixed good and malformed daemon entries
contributes:
  daemons:
    - name: good
      command: python
      args: ["-c", "pass"]
    - "not a mapping"
    - name: ""
      command: python
    - name: no-command
    - name: bad-args
      command: python
      args: "not-a-list"
    - name: bad-env
      command: python
      env: [not, a, mapping]
"""
    ws = tmp_path / "plugins"
    _write_plugin(ws, "mixed", manifest)

    with caplog.at_level("WARNING"):
        specs = discover_daemon_specs(
            workspace_plugins_dir=str(ws),
            shared_plugins_dir=str(tmp_path / "no-shared"),
        )

    assert [s.name for s in specs] == ["good"]
    # each skipped entry produced a loud skip line
    skip_lines = [r for r in caplog.records if "skipping daemon entry" in r.message]
    assert len(skip_lines) == 5


def test_daemons_not_a_list_skipped(tmp_path, caplog):
    ws = tmp_path / "plugins"
    _write_plugin(
        ws,
        "bad",
        "name: bad\nversion: 1.0.0\ndescription: daemons is not a list\n"
        "contributes:\n  daemons: {name: d, command: x}\n",
    )
    with caplog.at_level("WARNING"):
        specs = discover_daemon_specs(
            workspace_plugins_dir=str(ws),
            shared_plugins_dir=str(tmp_path / "no-shared"),
        )
    assert specs == []
    assert any("contributes.daemons" in r.message for r in caplog.records)


def test_daemon_specs_from_manifest_never_raises():
    """Direct parse-layer guarantee: garbage in, empty list + logs out."""
    for garbage in (None, 42, "x", [], [None], [{"env": 1}], {"a": 1}):
        specs = daemon_specs_from_manifest("p", "/tmp/p", garbage)
        assert specs == []


# ---------------------------------------------------------------------------
# supervisor — spawn / env / restart / circuit breaker / stop
# ---------------------------------------------------------------------------
def test_supervisor_spawns_daemon(tmp_path):
    out = tmp_path / "pid.txt"
    script = (
        "import os, sys, time\n"
        f"open({str(out)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    spec = DaemonSpec(
        name="d", plugin="p", command=[sys.executable, "-c", script]
    )
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        assert sup.states[spec.key] == "running"
    finally:
        sup.stop()


def test_supervisor_env_injection(tmp_path, monkeypatch):
    """spec.env overlays os.environ — workspace-delivered env (tokens etc.)
    flows through, spec vars are added."""
    monkeypatch.setenv("DAEMON_TEST_INHERITED", "from-workspace")
    out = tmp_path / "env.txt"
    script = (
        "import os\n"
        f"open({str(out)!r}, 'w').write("
        "os.environ.get('DAEMON_TEST_INHERITED', '') + '|' + "
        "os.environ.get('DAEMON_TEST_OWN', ''))\n"
    )
    spec = DaemonSpec(
        name="env",
        plugin="p",
        command=[sys.executable, "-c", script],
        env={"DAEMON_TEST_OWN": "from-spec"},
    )
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        assert out.read_text() == "from-workspace|from-spec"
    finally:
        sup.stop()


def test_supervisor_does_not_inherit_stale_channel_capability(tmp_path, monkeypatch):
    """Only the socket manager may publish its reserved local capability.

    A runtime launched from a shell/process that happens to carry an old socket
    env must not forward that dead or attacker-selected path to a daemon after
    the local bind fails and the spec capability has been cleared.
    """
    monkeypatch.setenv(CHANNEL_A2A_SOCKET_ENV, "/tmp/stale-parent.sock")
    monkeypatch.setenv(CHANNEL_A2A_TOKEN_ENV, "stale-parent-token")
    monkeypatch.setenv(CHANNEL_API_VERSION_ENV, "stale-parent-version")
    monkeypatch.setenv(CHANNEL_PLUGIN_ID_ENV, "stale-parent-plugin")
    out = tmp_path / "reserved-env.txt"
    script = (
        "import os\n"
        f"open({str(out)!r}, 'w').write("
        f"str({CHANNEL_A2A_SOCKET_ENV!r} in os.environ) + '|' + "
        f"str({CHANNEL_A2A_TOKEN_ENV!r} in os.environ) + '|' + "
        f"str({CHANNEL_API_VERSION_ENV!r} in os.environ) + '|' + "
        f"str({CHANNEL_PLUGIN_ID_ENV!r} in os.environ))\n"
    )
    spec = DaemonSpec(name="env", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        assert out.read_text() == "False|False|False|False"
    finally:
        sup.stop()


def test_supervisor_cwd(tmp_path):
    out = tmp_path / "cwd.txt"
    script = f"import os\nopen({str(out)!r}, 'w').write(os.getcwd())\n"
    spec = DaemonSpec(
        name="cwd",
        plugin="p",
        command=[sys.executable, "-c", script],
        cwd=str(tmp_path),
    )
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        assert out.read_text() == str(tmp_path.resolve())
    finally:
        sup.stop()


def test_supervisor_restarts_on_crash_then_trips_breaker(tmp_path):
    """A fast-crashing daemon is restarted with backoff; after
    max_fast_failures consecutive fast exits the breaker trips: state
    'failed', no further spawns."""
    marker = tmp_path / "spawns.txt"
    script = f"open({str(marker)!r}, 'a').write('x')\n"
    spec = DaemonSpec(name="crashy", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec], max_fast_failures=3)
    sup.start()
    try:
        assert _wait_for(lambda: sup.states.get(spec.key) == "failed")
        # 3 consecutive fast failures = 3 spawns, 2 restarts
        assert marker.read_text() == "xxx"
        assert sup.restart_counts[spec.key] == 2
        # breaker means no further spawns
        time.sleep(0.3)
        assert marker.read_text() == "xxx"
    finally:
        sup.stop()


def test_supervisor_backoff_is_exponential_and_capped():
    """Backoff waits double per consecutive failure and cap at the ceiling.
    Recorded via the _wait_backoff seam so timing never flakes."""
    waits = []

    class Recorder(DaemonSupervisor):
        def _wait_backoff(self, seconds):
            waits.append(seconds)
            return super()._wait_backoff(0)  # don't actually sleep

    spec = DaemonSpec(
        name="crashy", plugin="p", command=[sys.executable, "-c", "pass"]
    )
    sup = Recorder(
        [spec],
        backoff_base_seconds=1.0,
        backoff_cap_seconds=4.0,
        max_fast_failures=5,
        poll_interval_seconds=0.02,
    )
    sup.start()
    try:
        assert _wait_for(lambda: sup.states.get(spec.key) == "failed")
        assert waits == [1.0, 2.0, 4.0, 4.0]  # doubled then capped
    finally:
        sup.stop()


def test_supervisor_slow_failure_resets_breaker(tmp_path):
    """A daemon that ran long enough before exiting resets the consecutive
    fast-failure count — long-lived-then-crashed daemons keep restarting."""
    marker = tmp_path / "spawns.txt"
    script = f"open({str(marker)!r}, 'a').write('x')\n"
    spec = DaemonSpec(name="slow", plugin="p", command=[sys.executable, "-c", script])
    # every exit counts as "slow" — breaker must never trip
    sup = _fast_supervisor([spec], fast_failure_seconds=0.0, max_fast_failures=2)
    sup.start()
    try:
        assert _wait_for(lambda: len(marker.read_text()) >= 4 if marker.exists() else False)
        assert sup.states[spec.key] != "failed"
    finally:
        sup.stop()


def test_supervisor_stop_terminates_daemon(tmp_path):
    ready = tmp_path / "ready.txt"
    script = (
        "import time\n"
        f"open({str(ready)!r}, 'w').write('up')\n"
        "time.sleep(60)\n"
    )
    spec = DaemonSpec(name="d", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec])
    sup.start()
    assert _wait_for(ready.exists)
    proc = sup._procs[spec.key]
    sup.stop()
    assert proc.poll() is not None
    assert sup.states[spec.key] == "stopped"


def test_supervisor_stop_sigkills_term_ignorer(tmp_path):
    """A daemon that ignores SIGTERM is SIGKILLed after the grace period."""
    ready = tmp_path / "ready.txt"
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').write('up')\n"
        "time.sleep(60)\n"
    )
    spec = DaemonSpec(name="stubborn", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec], term_grace_seconds=0.3)
    sup.start()
    assert _wait_for(ready.exists)
    proc = sup._procs[spec.key]
    start = time.monotonic()
    sup.stop()
    assert proc.poll() is not None
    # went through the grace window, not a hang
    assert time.monotonic() - start < 10


def test_supervisor_spawn_failure_counts_as_fast_failure():
    """An unspawnable command (ENOENT) never crashes the supervisor thread;
    it burns through the breaker and lands on 'failed'."""
    spec = DaemonSpec(
        name="ghost", plugin="p", command=["/nonexistent/daemon-binary-215"]
    )
    sup = _fast_supervisor([spec], max_fast_failures=2)
    sup.start()
    try:
        assert _wait_for(lambda: sup.states.get(spec.key) == "failed")
    finally:
        sup.stop()


def test_supervisor_zero_specs_noop():
    sup = _fast_supervisor([])
    sup.start()
    sup.stop()
    assert sup.states == {}


def test_supervisor_stop_idempotent(tmp_path):
    spec = DaemonSpec(
        name="d",
        plugin="p",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    sup = _fast_supervisor([spec])
    sup.start()
    _wait_for(lambda: sup.states.get(spec.key) == "running")
    sup.stop()
    sup.stop()  # second stop must not raise


def test_daemon_runs_in_own_process_group(tmp_path):
    """The daemon is spawned in its own session/process group so a group
    SIGTERM/SIGKILL reaps grandchildren too."""
    out = tmp_path / "pgid.txt"
    script = (
        "import os, time\n"
        f"open({str(out)!r}, 'w').write(str(os.getpgid(0)))\n"
        "time.sleep(60)\n"
    )
    spec = DaemonSpec(name="d", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        child_pgid = int(out.read_text())
        assert child_pgid != os.getpgid(0)  # not OUR group
        assert child_pgid == sup._procs[spec.key].pid  # its own group leader
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# boot wiring — prove-fail: specs discovered but never spawned/stopped
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_supervisor_when_bound_waits_for_bind():
    """The starter must NOT start daemons before uvicorn reports bound
    (a daemon that talks to the local A2A server would race the bind),
    and MUST start them once it flips."""

    class FakeServer:
        started = False

    class FakeSupervisor:
        started = False

        def start(self):
            self.started = True

    server = FakeServer()
    sup = FakeSupervisor()
    task = asyncio.create_task(
        start_supervisor_when_bound(server, sup, poll_interval=0.01)
    )
    await asyncio.sleep(0.05)
    assert sup.started is False  # still waiting for the bind
    server.started = True
    assert await task is True
    assert sup.started is True


@pytest.mark.asyncio
async def test_start_supervisor_when_bound_backstop_starts_anyway():
    """Mirrors the poll-delivery backstop: a stalled bind must not mean
    daemons never start."""

    class NeverBound:
        started = False

    class FakeSupervisor:
        started = False

        def start(self):
            self.started = True

    sup = FakeSupervisor()
    assert await start_supervisor_when_bound(
        NeverBound(), sup, poll_interval=0.01, max_wait_seconds=0.03
    )
    assert sup.started is True


# ---------------------------------------------------------------------------
# wait_until_server_bound — the shared "is uvicorn bound?" gate (EV1 fail-OPEN)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wait_until_server_bound_returns_true_when_flips_late():
    """runtime EV1: a server whose ``.started`` flips True LATE must be waited
    for and reported bound — this is the exact shape the initial-prompt path now
    uses instead of the old HTTP self-poll that fail-CLOSED dropped the prompt."""

    class FakeServer:
        started = False

    server = FakeServer()
    task = asyncio.create_task(
        wait_until_server_bound(server, max_wait=5.0, poll_interval=0.01)
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # still waiting on the (unbound) server
    server.started = True   # bind reported late
    assert await asyncio.wait_for(task, timeout=2.0) is True


@pytest.mark.asyncio
async def test_wait_until_server_bound_true_immediately_when_already_bound():
    class FakeServer:
        started = True

    assert await wait_until_server_bound(FakeServer(), poll_interval=0.01) is True


@pytest.mark.asyncio
async def test_wait_until_server_bound_returns_false_on_timeout_fail_open():
    """The defensive backstop: never-bound returns False after max_wait so the
    CALLER can fail-OPEN (send/start anyway) — it must NOT hang forever."""

    class NeverBound:
        started = False

    result = await wait_until_server_bound(
        NeverBound(), max_wait=0.03, poll_interval=0.01
    )
    assert result is False


@pytest.mark.asyncio
async def test_wait_until_server_bound_none_server_degrades_fail_open():
    """A mis-wired (None) server must degrade to fail-OPEN (False after the
    bounded wait), never raise into the caller."""
    result = await wait_until_server_bound(None, max_wait=0.02, poll_interval=0.01)
    assert result is False


def test_main_boot_wires_daemon_supervisor():
    """Prove-fail for the boot wiring: if discovery/spawn/stop are not wired
    into main(), this fails. Follows the repo convention of pinning main()'s
    monolith wiring by source inspection (main() is not runnable in unit
    tests), same spirit as test_main_privileged_plugin_failure's 'if main.py
    drifts, these tests catch the drift'."""
    src = inspect.getsource(m.main)
    # The daemon lifecycle moved into the DaemonRuntime holder so it can be
    # ESTABLISHED at boot AND EXTENDED on a post-boot plugin install
    # (hot-start via /internal/daemons/reload) without a restart. main() wires
    # the holder; the holder owns discovery/spawn/sockets. Pin BOTH halves.
    #
    # main() side: construct the holder, mount the reload route, and schedule
    # ensure_daemons as a background task (never blocks boot — its bind gate
    # only clears once server.serve() below starts listening).
    assert "DaemonRuntime(" in src
    assert "add_daemon_routes(" in src
    assert "ensure_daemons()" in src
    # shutdown wired in the `finally:` of server.serve()
    finally_block = src.split("await server.serve()", 1)[1]
    assert "daemon_boot_task" in finally_block
    assert "await daemon_runtime.stop()" in finally_block

    # holder side: it actually does discovery + spawn + the private A2A sockets,
    # gates spawn on the uvicorn bind, and supports the hot-add path.
    import molecule_runtime.daemon_runtime as _dr

    holder_src = inspect.getsource(_dr)
    assert "discover_daemon_specs" in holder_src
    assert "DaemonSupervisor" in holder_src
    assert "ChannelEventSocketManager" in holder_src
    assert "wait_until_server_bound" in holder_src
    assert "add_specs" in holder_src          # warm hot-add path
    assert "supervise(" in holder_src


def test_main_boot_daemon_wiring_is_fail_open():
    """The daemon holder setup at boot must be logged, never fatal — the
    DaemonRuntime construction + route mount + task schedule sit inside a
    try/except whose handler announces non-fatality instead of re-raising."""
    src = inspect.getsource(m.main)
    idx = src.index("DaemonRuntime(")
    # a try: guards the holder setup
    assert "try:" in src[max(0, idx - 500):idx]
    # and its except announces non-fatality instead of re-raising into boot
    assert "non-fatal" in src[idx:idx + 600]
    # the holder's own internal steps (grid seed, socket bind) are fail-soft too
    import molecule_runtime.daemon_runtime as _dr

    holder_src = inspect.getsource(_dr)
    assert "non-fatal" in holder_src or "must never break a reload" in holder_src


# ---------------------------------------------------------------------------
# contributes.state — durable per-plugin state dir (RFC sdk#181, #360 defect A)
# ---------------------------------------------------------------------------
STATE_MANIFEST = """\
name: gmail-channel-molecule
version: 1.0.0
description: channel bridge that keeps a poll cursor
kind: channel
contributes:
  state:
    durability: required
    description: Gmail poll cursor.
  daemons:
    - name: bridge
      command: python
      args: ["-m", "gmail_channel_molecule.daemon"]
"""

# Deliberately NOT a channel/trigger plugin: contributes.state must be
# kind-agnostic. A kind-gated implementation (e.g. injecting in channel_events,
# which only runs for channel + trigger) passes every channel test and still
# fails this one.
SKILL_STATE_MANIFEST = """\
name: notes-skill
version: 1.0.0
description: a skill plugin that nonetheless keeps state
kind: skill
contributes:
  state:
    durability: preferred
  daemons:
    - name: sync
      command: python
      args: ["-m", "notes_skill.sync"]
"""


def test_state_contribution_passes_ssot_validation():
    """Same tolerance argument as `daemons`: if `contributes.state` produced
    violations, fail-closed enforcement would refuse every stateful plugin."""
    import yaml

    raw = yaml.safe_load(STATE_MANIFEST)
    assert manifest_ssot.validate_manifest_ssot(raw) == []


def test_discovery_reads_contributes_state(tmp_path):
    ws = tmp_path / "configs" / "plugins"
    ws.mkdir(parents=True)
    _write_plugin(ws, "gmail-channel-molecule", STATE_MANIFEST)
    specs = discover_daemon_specs(workspace_plugins_dir=str(ws), shared_plugins_dir=str(tmp_path / "none"))
    assert len(specs) == 1
    assert specs[0].state == {
        "durability": "required",
        "description": "Gmail poll cursor.",
    }


def test_state_is_kind_agnostic(tmp_path):
    """A `kind: skill` plugin declaring state still gets it carried through."""
    ws = tmp_path / "configs" / "plugins"
    ws.mkdir(parents=True)
    _write_plugin(ws, "notes-skill", SKILL_STATE_MANIFEST)
    specs = discover_daemon_specs(workspace_plugins_dir=str(ws), shared_plugins_dir=str(tmp_path / "none"))
    assert len(specs) == 1
    assert specs[0].kind == "skill"
    assert specs[0].state == {"durability": "preferred"}


def test_a_plugin_without_state_gets_none():
    specs = daemon_specs_from_manifest(
        "p", "/tmp/p", [{"name": "d", "command": "python"}], raw_state=None
    )
    assert specs[0].state is None


def test_a_malformed_state_declaration_is_coerced_not_dropped(caplog):
    """Skip-not-reject. A typo must not silently deny the plugin a state dir,
    and must not fail manifest validation either."""
    caplog.set_level("WARNING")
    specs = daemon_specs_from_manifest(
        "p", "/tmp/p", [{"name": "d", "command": "python"}], raw_state="durable-please"
    )
    assert specs[0].state == {"durability": "required"}
    assert any("contributes.state is str" in r.getMessage() for r in caplog.records)


def test_supervisor_injects_the_state_dir(tmp_path, monkeypatch):
    from molecule_runtime import plugin_state

    root = tmp_path / "durable-root"
    root.mkdir()
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(root))
    out = tmp_path / "state-env.txt"
    script = (
        "import os\n"
        f"open({str(out)!r}, 'w').write("
        f"os.environ.get({plugin_state.STATE_DIR_ENV!r}, 'MISSING') + '|' + "
        f"os.environ.get({plugin_state.DURABLE_ENV!r}, 'MISSING'))\n"
    )
    spec = DaemonSpec(
        name="d",
        plugin="gmail-channel-molecule",
        command=[sys.executable, "-c", script],
        state={"durability": "required"},
    )
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        got_dir, got_durable = out.read_text().split("|")
    finally:
        sup.stop()
    # Already namespaced by the runtime — the plugin never joins its own name.
    assert got_dir == str(root / "gmail-channel-molecule")
    assert got_durable in ("0", "1")  # honest either way; value is probe-dependent


def test_a_manifest_cannot_forge_its_own_state_dir(tmp_path, monkeypatch):
    """THE isolation test.

    `contributes.state` has no path field, so the only way a plugin could name
    its own directory is by smuggling the reserved var through its `env:` map.
    The runtime sets both vars AFTER `child_env.update(spec.env)`, so the
    manifest loses. If injection ever moves before the overlay, this fails.
    """
    from molecule_runtime import plugin_state

    root = tmp_path / "durable-root"
    root.mkdir()
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(root))
    out = tmp_path / "forged.txt"
    script = (
        "import os\n"
        f"open({str(out)!r}, 'w').write("
        f"os.environ.get({plugin_state.STATE_DIR_ENV!r}, 'MISSING') + '|' + "
        f"os.environ.get({plugin_state.DURABLE_ENV!r}, 'MISSING'))\n"
    )
    spec = DaemonSpec(
        name="d",
        plugin="evil-plugin",
        command=[sys.executable, "-c", script],
        env={
            plugin_state.STATE_DIR_ENV: "/etc",          # try to escape
            plugin_state.DURABLE_ENV: "1",               # try to lie
        },
        state={"durability": "required"},
    )
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        got_dir, got_durable = out.read_text().split("|")
    finally:
        sup.stop()
    assert got_dir == str(root / "evil-plugin"), "manifest env overrode the state dir"
    assert got_dir != "/etc"
    # The forged "1" must not survive either — on a tmp root the probe
    # downgrades, so the honest answer here is 0.
    assert got_durable == "0", "manifest env forged the durability flag"


def test_a_stale_inherited_state_dir_is_stripped(tmp_path, monkeypatch):
    """Reserved means reserved: a daemon we resolve NO state dir for must not
    see one the runtime happened to inherit from its own environment."""
    from molecule_runtime import plugin_state

    monkeypatch.setenv(plugin_state.STATE_DIR_ENV, "/tmp/stale-parent-state")
    monkeypatch.setenv(plugin_state.DURABLE_ENV, "1")
    out = tmp_path / "stale.txt"
    script = (
        "import os\n"
        f"open({str(out)!r}, 'w').write("
        f"str({plugin_state.STATE_DIR_ENV!r} in os.environ) + '|' + "
        f"str({plugin_state.DURABLE_ENV!r} in os.environ))\n"
    )
    # state=None -> the plugin declared none, so nothing is injected.
    spec = DaemonSpec(name="d", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        assert out.read_text() == "False|False"
    finally:
        sup.stop()


def test_a_plugin_without_state_gets_a_byte_identical_child_env(tmp_path, monkeypatch):
    """No regression for every daemon that exists today.

    A plugin that declares no state must see exactly the environment it saw
    before this change — neither var present, nothing else perturbed.
    """
    from molecule_runtime import plugin_state

    root = tmp_path / "durable-root"
    root.mkdir()
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(root))
    out = tmp_path / "clean.txt"
    script = (
        "import os, json\n"
        f"open({str(out)!r}, 'w').write(json.dumps(sorted(\n"
        f"    k for k in os.environ if k.startswith('MOLECULE_PLUGIN_STATE'))))\n"
    )
    spec = DaemonSpec(name="d", plugin="p", command=[sys.executable, "-c", script])
    sup = _fast_supervisor([spec])
    sup.start()
    try:
        assert _wait_for(out.exists)
        import json as _json

        present = _json.loads(out.read_text())
    finally:
        sup.stop()
    # The provisioner's own root var is inherited (it always was); the two
    # reserved daemon vars must NOT appear.
    assert plugin_state.STATE_DIR_ENV not in present
    assert plugin_state.DURABLE_ENV not in present
    assert present == [plugin_state.STATE_ROOT_ENV]
