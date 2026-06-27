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

    def teardown_method(self):
        pai.set_loaded_mcp_tools(None)

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
