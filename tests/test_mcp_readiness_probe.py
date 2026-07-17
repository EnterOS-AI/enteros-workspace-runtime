"""core#3082 / runtime#181: the active management-MCP readiness probe.

These tests prove the probe produces a RELIABLE, turn-INDEPENDENT readiness
signal — the thing the per-turn init capture could not:

  * MCP loaded + exposes provision_workspace  -> reports
    mcp__molecule-platform__provision_workspace (gate marks/holds online).
  * MCP up but provision_workspace genuinely absent -> reports the real (absent)
    list so the gate degrades correctly (no false-green).
  * not yet loaded / not declared / probe fails -> None, holder left untouched
    (no false-empty that would clobber a known-good signal; the core 180s grace
    window absorbs a transient miss).

The JSON-RPC exchange is exercised both hermetically (transport-injected fakes)
AND end-to-end against a real stdio subprocess (a tiny Python MCP server), so the
spawn + handshake path is covered without needing node / @molecule-ai/mcp-server.
"""

import json
import os
import sys
import threading

import pytest

from molecule_runtime import mcp_readiness_probe as probe
from molecule_runtime import platform_agent_identity as pai


PROVISION_ID = "mcp__molecule-platform__provision_workspace"


# ── transport-injected fake server ──────────────────────────────────────────
class _FakeServer:
    """A scripted JSON-RPC peer for exchange_tools_list. Records what the probe
    sends and serves a queue of response lines the probe reads back."""

    def __init__(self, tools=None, *, init_error=False, tools_error=False,
                 extra_noise=False, hang=False):
        self._tools = tools
        self._init_error = init_error
        self._tools_error = tools_error
        self._extra_noise = extra_noise
        self._hang = hang
        self.sent: list[dict] = []
        self._outbox: list[str] = []

    def send_line(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append(msg)
        method = msg.get("method")
        if method == "initialize":
            if self._extra_noise:
                # A notification + a stray non-JSON log line BEFORE the response,
                # to prove the reader skips noise and matches by id.
                self._outbox.append(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}))
                self._outbox.append("starting management MCP server...")
            if self._init_error:
                self._outbox.append(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "boom"}}))
            else:
                self._outbox.append(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "molecule-mcp", "version": "1"}}}))
        elif method == "notifications/initialized":
            pass  # no response to a notification
        elif method == "tools/list":
            if self._hang:
                return  # never respond -> probe must time out
            if self._tools_error:
                self._outbox.append(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}))
            else:
                tools = [{"name": n} for n in (self._tools or [])]
                self._outbox.append(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}}))

    def read_line(self, timeout: float):
        if self._outbox:
            return self._outbox.pop(0)
        return None  # nothing queued -> models timeout / EOF


def _deadline(seconds=5.0):
    import time
    return time.monotonic() + seconds


# ── exchange_tools_list ─────────────────────────────────────────────────────
def test_exchange_reports_provision_workspace():
    srv = _FakeServer(tools=["provision_workspace", "list_workspaces", "deprovision_workspace"])
    names = probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline())
    assert names == ["provision_workspace", "list_workspaces", "deprovision_workspace"]
    ids = probe.to_tool_ids(names)
    assert PROVISION_ID in ids
    assert probe.management_provision_ready(ids) is True
    # The handshake order: initialize, then initialized notification, then list.
    assert [m.get("method") for m in srv.sent] == [
        "initialize", "notifications/initialized", "tools/list",
    ]


def test_exchange_loaded_but_provision_absent_is_truthful():
    # The management MCP answered, but the required verb isn't on the surface —
    # the gate must be able to SEE that and degrade (no false-green).
    srv = _FakeServer(tools=["list_workspaces", "get_workspace"])
    names = probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline())
    ids = probe.to_tool_ids(names)
    assert PROVISION_ID not in ids
    assert probe.management_provision_ready(ids) is False


def test_exchange_empty_tool_list():
    srv = _FakeServer(tools=[])
    names = probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline())
    assert names == []
    assert probe.management_provision_ready(probe.to_tool_ids(names)) is False


def test_exchange_skips_notifications_and_stray_log_noise():
    srv = _FakeServer(tools=["provision_workspace"], extra_noise=True)
    names = probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline())
    assert names == ["provision_workspace"]


def test_exchange_initialize_error_returns_none():
    srv = _FakeServer(tools=["provision_workspace"], init_error=True)
    assert probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline()) is None


def test_exchange_tools_list_error_returns_none():
    srv = _FakeServer(tools=["provision_workspace"], tools_error=True)
    assert probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline()) is None


def test_exchange_timeout_returns_none():
    srv = _FakeServer(tools=["provision_workspace"], hang=True)
    # Past deadline immediately -> tools/list never answered -> None.
    assert probe.exchange_tools_list(srv.send_line, srv.read_line, _deadline(0.05)) is None


# ── resolve_management_launch ───────────────────────────────────────────────
def _write_settings(tmp_path, entry):
    settings = tmp_path / "settings.json"
    body = {"mcpServers": {"molecule-platform": entry}} if entry is not None else {"mcpServers": {}}
    settings.write_text(json.dumps(body))
    return settings


def test_resolve_from_settings_entry_merges_env(tmp_path, monkeypatch):
    settings = _write_settings(tmp_path, {
        "command": "molecule-mcp",
        "args": ["--stdio"],
        "env": {"MOLECULE_MCP_MODE": "management"},
    })
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
    monkeypatch.setattr(probe.shutil, "which", lambda c: "/usr/local/bin/molecule-mcp" if c == "molecule-mcp" else None)
    monkeypatch.setenv("MOLECULE_API_KEY", "from-container")

    launch = probe.resolve_management_launch()
    assert launch is not None
    argv, env = launch
    assert argv == ["/usr/local/bin/molecule-mcp", "--stdio"]
    assert env["MOLECULE_MCP_MODE"] == "management"   # from the settings entry
    assert env["MOLECULE_API_KEY"] == "from-container"  # from os.environ base


def test_resolve_none_when_not_declared_and_no_binary(tmp_path, monkeypatch):
    settings = _write_settings(tmp_path, None)
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
    monkeypatch.setattr(probe.shutil, "which", lambda c: None)
    assert probe.resolve_management_launch() is None


def test_resolve_baked_binary_fallback(tmp_path, monkeypatch):
    settings = _write_settings(tmp_path, None)
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
    monkeypatch.setattr(
        probe.shutil, "which",
        lambda c: "/usr/local/bin/molecule-platform-mcp" if c == probe.MANAGEMENT_MCP_COMMAND else None,
    )
    launch = probe.resolve_management_launch()
    assert launch is not None
    argv, env = launch
    assert argv == ["/usr/local/bin/molecule-platform-mcp"]
    assert env["MOLECULE_MCP_MODE"] == "management"


def test_resolve_missing_settings_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setattr(probe.shutil, "which", lambda c: None)
    assert probe.resolve_management_launch() is None


# ── real-subprocess end-to-end probe ────────────────────────────────────────
_FAKE_MCP_SERVER = r'''
import json, sys
def main(tools):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        if method == "initialize":
            sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"fake","version":"1"}}}) + "\n")
            sys.stdout.flush()
        elif method == "tools/list":
            sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"tools":[{"name":n} for n in tools]}}) + "\n")
            sys.stdout.flush()
        # notifications/initialized and anything else: ignore
if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
'''


def _install_fake_server(tmp_path, monkeypatch, tools):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(_FAKE_MCP_SERVER)
    settings = _write_settings(tmp_path, {
        "command": sys.executable,
        "args": [str(server), json.dumps(tools)],
        "env": {"MOLECULE_MCP_MODE": "management"},
    })
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
    # sys.executable is an absolute path; which() of an absolute path resolves
    # it, but pin it explicitly so the test never depends on PATH lookup.
    monkeypatch.setattr(probe.shutil, "which", lambda c: c)


def test_probe_real_subprocess_reports_provision_workspace(tmp_path, monkeypatch):
    _install_fake_server(tmp_path, monkeypatch, ["provision_workspace", "list_workspaces"])
    result = probe.probe_management_mcp_tools(timeout=20.0)
    assert result is not None
    assert PROVISION_ID in result
    assert probe.management_provision_ready(result) is True


def test_probe_real_subprocess_absent_provision_is_truthful(tmp_path, monkeypatch):
    _install_fake_server(tmp_path, monkeypatch, ["list_workspaces"])
    result = probe.probe_management_mcp_tools(timeout=20.0)
    assert result is not None
    assert PROVISION_ID not in result
    assert probe.management_provision_ready(result) is False


def test_probe_returns_none_when_not_declared(tmp_path, monkeypatch):
    settings = _write_settings(tmp_path, None)
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
    monkeypatch.setattr(probe.shutil, "which", lambda c: None)
    assert probe.probe_management_mcp_tools(timeout=2.0) is None


def test_probe_returns_none_on_unspawnable_command(tmp_path, monkeypatch):
    settings = _write_settings(tmp_path, {"command": "definitely-not-a-real-binary-xyz"})
    monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
    # which() resolves to the bare (nonexistent) name -> Popen raises -> None.
    monkeypatch.setattr(probe.shutil, "which", lambda c: c)
    assert probe.probe_management_mcp_tools(timeout=2.0) is None


# ── MCPReadinessProber: holder-publishing semantics ─────────────────────────
class TestProberPublishing:
    def setup_method(self):
        pai.set_loaded_mcp_tools(None)
        pai.set_mcp_tools_ready(None)  # EV2: reset the readiness holder too

    def teardown_method(self):
        pai.set_loaded_mcp_tools(None)
        pai.set_mcp_tools_ready(None)

    def test_run_once_publishes_loaded_tools_on_success(self):
        pr = probe.MCPReadinessProber(probe=lambda _t: [PROVISION_ID, "mcp__molecule-platform__list_workspaces"])
        result = pr.run_once()
        assert result == [PROVISION_ID, "mcp__molecule-platform__list_workspaces"]
        assert pai.loaded_mcp_tools() == [PROVISION_ID, "mcp__molecule-platform__list_workspaces"]

    def test_run_once_leaves_holder_untouched_on_none(self):
        # A prior good value must survive a transient probe miss (no false-empty).
        pai.set_loaded_mcp_tools([PROVISION_ID])
        pr = probe.MCPReadinessProber(probe=lambda _t: None)
        assert pr.run_once() is None
        assert pai.loaded_mcp_tools() == [PROVISION_ID]

    # ── EV2 mcp_tools_ready ─────────────────────────────────────────────────

    def test_mcp_tools_ready_absent_before_any_probe(self):
        # Tri-state initial: unknown (None), distinct from False. Nothing probed
        # yet, so the heartbeat OMITS the field.
        assert pai.mcp_tools_ready() is None
        assert pai.first_ready_at() is None

    def test_run_once_sets_mcp_tools_ready_true_on_first_success(self):
        # EV2 POSITIVE half: the FIRST successful tools/list flips the readiness
        # signal True (turn-independent) and stamps first_ready_at.
        assert pai.mcp_tools_ready() is None  # absent before
        pr = probe.MCPReadinessProber(probe=lambda _t: [PROVISION_ID])
        pr.run_once()
        assert pai.mcp_tools_ready() is True  # True after first list
        first = pai.first_ready_at()
        assert isinstance(first, str) and first.endswith("Z")

    def test_mcp_tools_ready_true_even_without_provision_workspace(self):
        # Turn-independent readiness is about the tools/list SUCCEEDING, not about
        # which tools it returned — so a management MCP that lists tools but not
        # provision_workspace still reports mcp_tools_ready=True (the online-flip
        # trigger), while loaded_mcp_tools truthfully lacks provision_workspace.
        pr = probe.MCPReadinessProber(probe=lambda _t: ["mcp__molecule-platform__list_workspaces"])
        pr.run_once()
        assert pai.mcp_tools_ready() is True
        assert probe.management_provision_ready(pai.loaded_mcp_tools()) is False

    def test_first_ready_at_stable_across_reprobes(self):
        # first_ready_at is stamped ONCE and never moves, so the reported
        # provisioning->ready latency stays stable across subsequent beats.
        pr = probe.MCPReadinessProber(probe=lambda _t: [PROVISION_ID])
        pr.run_once()
        stamped = pai.first_ready_at()
        assert stamped is not None
        pr.run_once()  # a later successful probe
        assert pai.first_ready_at() == stamped
        assert pai.mcp_tools_ready() is True

    def test_mcp_tools_ready_absent_on_probe_miss(self):
        # A probe that never succeeds leaves the readiness signal unknown (None),
        # so the heartbeat keeps OMITTING it — the row holds provisioning.
        pr = probe.MCPReadinessProber(probe=lambda _t: None)
        pr.run_once()
        assert pai.mcp_tools_ready() is None
        assert pai.first_ready_at() is None

    def test_run_once_publishes_truthful_absent_list(self):
        # Genuinely-missing provision_workspace is reported (not hidden), so the
        # gate can degrade — this is NOT a false-empty.
        pr = probe.MCPReadinessProber(probe=lambda _t: ["mcp__molecule-platform__list_workspaces"])
        pr.run_once()
        assert pai.loaded_mcp_tools() == ["mcp__molecule-platform__list_workspaces"]
        assert probe.management_provision_ready(pai.loaded_mcp_tools()) is False

    def test_run_once_swallows_probe_exception(self):
        def _boom(_t):
            raise RuntimeError("probe blew up")
        pr = probe.MCPReadinessProber(probe=_boom)
        assert pr.run_once() is None  # never raises into the loop
        assert pai.loaded_mcp_tools() is None

    def test_thread_lifecycle_publishes_then_stops(self):
        seen = threading.Event()

        def _probe(_t):
            seen.set()
            return [PROVISION_ID]

        pr = probe.MCPReadinessProber(retry_interval=0.01, interval=0.01, probe=_probe)
        pr.start()
        assert seen.wait(5.0), "prober thread did not run a probe"
        # Give the publish a beat, then assert the holder carries the tool.
        for _ in range(200):
            if pai.loaded_mcp_tools() == [PROVISION_ID]:
                break
            threading.Event().wait(0.01)
        assert pai.loaded_mcp_tools() == [PROVISION_ID]
        pr.stop()

    def test_disabled_via_env_does_not_start(self, monkeypatch):
        monkeypatch.setenv(probe.PROBE_ENABLED_ENV, "0")
        pr = probe.MCPReadinessProber(probe=lambda _t: [PROVISION_ID])
        assert pr.start() is None  # disabled -> no thread
        assert pai.loaded_mcp_tools() is None

    def test_run_retries_until_first_success_then_stops_reprobing(self):
        # runtime EV3: the prober must KEEP retrying (tight cadence) until the
        # first success — a management MCP delivered a few seconds post-boot is
        # still picked up — and then STOP. The old loop re-spawned npx +
        # handshake + tools/list FOREVER every 120s to re-publish a sticky signal
        # that only changes on the added/removed edges (removed is caught by
        # mcp_server_present() independently), so a running concierge cold-spawned
        # a subprocess for nothing for its whole life.
        calls = {"n": 0}
        done = threading.Event()

        def _probe(_t):
            calls["n"] += 1
            # First TWO attempts miss (MCP not connectable yet) -> None; the third
            # succeeds. Proves the retry-until-first-success loop still runs.
            if calls["n"] < 3:
                return None
            done.set()
            return [PROVISION_ID]

        pr = probe.MCPReadinessProber(retry_interval=0.01, interval=0.01, probe=_probe)
        pr.start()
        assert done.wait(5.0), "prober never reached its first success"
        # After the first success the loop must EXIT: no further run_once calls.
        # Give it well more than a retry interval's worth of chances to (wrongly)
        # re-probe; the count must stay pinned at the success attempt.
        threading.Event().wait(0.2)
        assert calls["n"] == 3, (
            "prober kept re-spawning after first success (steady-state re-probe "
            "was not removed)"
        )
        # The sticky holder still carries the published tools after the loop ends.
        assert pai.loaded_mcp_tools() == [PROVISION_ID]
        # And the thread has actually terminated (returned), not just idling.
        for _ in range(200):
            if pr._thread is None or not pr._thread.is_alive():
                break
            threading.Event().wait(0.01)
        assert pr._thread is None or not pr._thread.is_alive(), (
            "prober thread did not terminate after first success"
        )
        pr.stop()


def test_probe_enabled_default_on_and_toggle(monkeypatch):
    monkeypatch.delenv(probe.PROBE_ENABLED_ENV, raising=False)
    assert probe.probe_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv(probe.PROBE_ENABLED_ENV, off)
        assert probe.probe_enabled() is False
    monkeypatch.setenv(probe.PROBE_ENABLED_ENV, "1")
    assert probe.probe_enabled() is True


# ── HeartbeatLoop wiring: start() spawns the prober, stop() tears it down ────
@pytest.mark.asyncio
async def test_heartbeat_start_wires_prober_and_stop_tears_down(monkeypatch):
    import asyncio

    from molecule_runtime import heartbeat as hb_mod
    from molecule_runtime.heartbeat import HeartbeatLoop

    calls = {"start": 0, "stop": 0}

    class _SpyProber:
        def start(self):
            calls["start"] += 1
            return None

        def stop(self):
            calls["stop"] += 1

    # start() does `from molecule_runtime.mcp_readiness_probe import MCPReadinessProber`
    # at call time, so patching the attribute on the module is picked up.
    monkeypatch.setattr(probe, "MCPReadinessProber", _SpyProber)

    # Neutralize the network loops so start() doesn't POST anywhere.
    monkeypatch.setattr(hb_mod.HeartbeatLoop, "_heartbeat_thread_loop", lambda self: None)

    async def _noop_loop(self):
        return

    monkeypatch.setattr(hb_mod.HeartbeatLoop, "_loop", _noop_loop)

    loop = HeartbeatLoop("http://platform.test", "ws-1")
    loop.start()
    assert calls["start"] == 1
    assert isinstance(loop._mcp_prober, _SpyProber)

    await loop.stop()
    assert calls["stop"] == 1
    assert loop._mcp_prober is None


# ── runtime#49: adapter-aware launch env (mcp_launch_env overlay) ────────────
# The heartbeat readiness prober spawns the management MCP from the runtime
# process. Unlike the boot enumeration path (loaded_mcp_tools_probe), it did not
# hold the adapter, so on a runtime bundling its interpreter OFF the system PATH
# (hermes: Node under $HERMES_HOME/node/bin) its `npx @molecule-ai/mcp-server`
# spawn re-failed post-boot and could degrade the concierge via the heartbeat.
# These tests mirror test_loaded_mcp_tools_probe.py's launch-env tests: the same
# overlay the boot path applies is registered (main.py wires it from the resolved
# adapter) and folded UNDER the per-server env here (descriptor wins), child-only.
class TestReadinessProberLaunchEnv:
    def teardown_method(self):
        # Always clear the registered provider so it can't leak into other tests.
        pai.register_mcp_launch_env_provider(None)

    def _write_bare_named_server(self, tmp_path, tools, cmd_name="fake-npx"):
        """A fake stdio MCP server + a launcher exposed ONLY as a BARE command name
        in a bin dir OFF the system PATH — exactly the hermes node-off-PATH shape
        (mirrors test_loaded_mcp_tools_probe._write_bare_named_server). Returns
        ``(bin_dir, cmd_name)``.

        The tool list is BAKED into the server script (not passed as a shell arg)
        so the launcher only forwards ``"$@"`` — the same shape the reference test
        uses, and it sidesteps shell-quoting the JSON tool list."""
        server = tmp_path / "bare_mcp_server.py"
        # Reuse the shared fake-server body, but drive main() from a baked-in list
        # instead of sys.argv[1] so the launcher passes no JSON through the shell.
        server.write_text(
            _FAKE_MCP_SERVER.replace(
                'if __name__ == "__main__":\n    main(json.loads(sys.argv[1]))',
                f'if __name__ == "__main__":\n    main({tools!r})',
            )
        )
        bin_dir = tmp_path / "bundled-bin"
        bin_dir.mkdir()
        launcher = bin_dir / cmd_name
        launcher.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{server}" "$@"\n'
        )
        launcher.chmod(0o755)
        return bin_dir, cmd_name

    def test_resolve_folds_overlay_under_settings_env_and_leaves_parent_untouched(
        self, tmp_path, monkeypatch
    ):
        """The registered adapter overlay is merged over os.environ but UNDER the
        settings entry's own env (descriptor wins), and os.environ is never mutated
        (child-only) — the same precedence the boot path uses."""
        settings = _write_settings(tmp_path, {
            "command": "molecule-mcp",
            "args": ["--stdio"],
            # spec.env pins MOLECULE_MCP_MODE — it must win over any overlay key.
            "env": {"MOLECULE_MCP_MODE": "management"},
        })
        monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
        monkeypatch.setattr(probe.shutil, "which", lambda c: "/usr/local/bin/molecule-mcp" if c == "molecule-mcp" else None)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        # Adapter overlay: prepend a bundled bin dir to PATH + a key the spec pins.
        overlay = {"PATH": "/bundled/node/bin:/usr/bin:/bin", "MOLECULE_MCP_MODE": "OVERLAY_SHOULD_LOSE"}
        pai.register_mcp_launch_env_provider(lambda: overlay)

        launch = probe.resolve_management_launch()
        assert launch is not None
        argv, env = launch
        assert argv == ["/usr/local/bin/molecule-mcp", "--stdio"]
        # Overlay PATH landed (adapter bin dir prepended for the child)…
        assert env["PATH"] == "/bundled/node/bin:/usr/bin:/bin"
        # …but the per-server spec.env WON the collision (descriptor wins).
        assert env["MOLECULE_MCP_MODE"] == "management"
        # Parent process env is never mutated (child-only overlay).
        assert os.environ["PATH"] == "/usr/bin:/bin"

    def test_resolve_no_provider_is_a_noop(self, tmp_path, monkeypatch):
        """With no provider registered (base claude-code: node already on PATH) the
        resolved env is just os.environ + spec.env — unchanged behaviour."""
        settings = _write_settings(tmp_path, {"command": "molecule-mcp", "env": {"MOLECULE_MCP_MODE": "management"}})
        monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
        monkeypatch.setattr(probe.shutil, "which", lambda c: c)
        monkeypatch.setenv("MOLECULE_API_KEY", "from-container")
        pai.register_mcp_launch_env_provider(None)  # explicit: none registered

        _argv, env = probe.resolve_management_launch()
        assert env["MOLECULE_API_KEY"] == "from-container"
        assert env["MOLECULE_MCP_MODE"] == "management"

    def test_resolve_swallows_a_buggy_provider(self, tmp_path, monkeypatch):
        """A provider that raises must never crash the prober — resolve falls back
        to no overlay (resolve_mcp_launch_env swallows + returns {})."""
        settings = _write_settings(tmp_path, {"command": "molecule-mcp"})
        monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
        monkeypatch.setattr(probe.shutil, "which", lambda c: c)

        def _boom():
            raise RuntimeError("provider blew up")
        pai.register_mcp_launch_env_provider(_boom)

        launch = probe.resolve_management_launch()  # must not raise
        assert launch is not None

    def test_probe_end_to_end_overlay_resolves_bare_command_off_path(
        self, tmp_path, monkeypatch
    ):
        """End-to-end through probe_management_mcp_tools (real subprocess): with the
        bundled interpreter OFF the process PATH, NO overlay -> the bare command is
        unresolvable -> None; WITH the registered adapter overlay -> the SAME server
        resolves and its tools enumerate. This is the runtime#49 fix that the boot
        path already had, now closed on the heartbeat path."""
        bin_dir, cmd = self._write_bare_named_server(tmp_path, ["provision_workspace", "list_workspaces"])
        # Declare the management MCP by the BARE command name (like `npx`); it is
        # ONLY resolvable via bin_dir, which is OFF the system PATH.
        settings = _write_settings(tmp_path, {"command": cmd})
        monkeypatch.setattr(probe, "SETTINGS_PATH", str(settings))
        # Real PATH resolution (no monkeypatched which): mirror the live spawn.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        # 1) No overlay -> bare `cmd` off PATH -> Popen can't spawn it -> None.
        pai.register_mcp_launch_env_provider(None)
        assert probe.probe_management_mcp_tools(timeout=20.0) is None

        # 2) Adapter overlay prepends the bundled bin dir -> resolves -> enumerate.
        pai.register_mcp_launch_env_provider(
            lambda: {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        )
        result = probe.probe_management_mcp_tools(timeout=20.0)
        assert result is not None
        assert PROVISION_ID in result
        assert probe.management_provision_ready(result) is True
        # Child-only: the parent process PATH was never mutated by the overlay.
        assert str(bin_dir) not in os.environ.get("PATH", "")
