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

    def test_run_once_sets_mcp_tools_ready_true_on_first_provision_ready_success(self):
        # EV2 POSITIVE half (CORRECTED contract): the readiness signal flips True
        # only when a successful tools/list carries the REQUIRED provision_workspace
        # verb — turn-independent — and stamps first_ready_at. mcp_tools_ready MEANS
        # "provision-ready", NOT merely "some tools/list succeeded" (the fix for the
        # #4449/#320 regression where core flipped a provision-less concierge online).
        assert pai.mcp_tools_ready() is None  # absent before
        pr = probe.MCPReadinessProber(probe=lambda _t: [PROVISION_ID])
        pr.run_once()
        assert pai.mcp_tools_ready() is True  # True after first provision-ready list
        first = pai.first_ready_at()
        assert isinstance(first, str) and first.endswith("Z")

    def test_mcp_tools_ready_FALSE_without_provision_workspace(self):
        # CORRECTED contract (was test_mcp_tools_ready_true_even_without_provision_
        # workspace, which encoded the BUG): a management MCP whose tools/list
        # SUCCEEDS but does NOT expose provision_workspace is NOT online-ready. The
        # probe records mcp_tools_ready=False (tri-state "probed-not-ready"), never
        # True — so core neither flips such a concierge online (defect #1) nor treats
        # it as the reliable degrade signal. loaded_mcp_tools stays truthful (absent).
        pr = probe.MCPReadinessProber(probe=lambda _t: ["mcp__molecule-platform__list_workspaces"])
        pr.run_once()
        assert pai.mcp_tools_ready() is False
        assert pai.first_ready_at() is None  # never stamped without provision-ready
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
        # gate can degrade — this is NOT a false-empty. And mcp_tools_ready is
        # recorded False (probed-not-ready), never True.
        pr = probe.MCPReadinessProber(probe=lambda _t: ["mcp__molecule-platform__list_workspaces"])
        pr.run_once()
        assert pai.loaded_mcp_tools() == ["mcp__molecule-platform__list_workspaces"]
        assert probe.management_provision_ready(pai.loaded_mcp_tools()) is False
        assert pai.mcp_tools_ready() is False

    def test_run_retries_while_provision_absent_until_provision_ready(self):
        # "success" means provision-READY, not merely a tools/list that returned
        # SOME tool. A management MCP that lists tools but not provision_workspace is
        # NOT ready — the prober must KEEP retrying (so a post-boot plugin reconcile
        # that later delivers provision_workspace is still captured) and only publish
        # True once provision-ready.
        calls = {"n": 0}
        ready = threading.Event()

        def _probe(_t):
            calls["n"] += 1
            # First two probes: MCP up but provision_workspace ABSENT -> not ready.
            if calls["n"] < 3:
                return ["mcp__molecule-platform__list_workspaces"]
            # Third: plugin reconcile has delivered provision_workspace.
            ready.set()
            return [PROVISION_ID]

        pr = probe.MCPReadinessProber(retry_interval=0.01, interval=0.01, probe=_probe)
        pr.start()
        assert ready.wait(5.0), "prober never reached provision-ready"
        threading.Event().wait(0.05)
        pr.stop()
        # It kept retrying through the provision-absent probes (>=3 calls) and then
        # published True on the provision-ready list.
        assert calls["n"] >= 3
        assert pai.mcp_tools_ready() is True
        assert probe.management_provision_ready(pai.loaded_mcp_tools()) is True

    def test_run_keeps_probing_after_success_and_reflects_provision_loss(self):
        # LIVE-signal contract (fix for the #4457 EV2 availability hole): after the
        # first provision-ready success the prober does NOT stop — it keeps
        # re-probing at the steady interval so mcp_tools_ready reflects CURRENT
        # reality. If provision_workspace is later UNREGISTERED while the management
        # MCP SERVER stays up (a plugin reconcile drops the verb; mcp_server_present
        # stays true), the holder must flip back to False so core degrades the box —
        # NOT stay frozen True forever (the sustained-absence availability hole).
        calls = {"n": 0}
        lost = threading.Event()

        def _probe(_t):
            calls["n"] += 1
            n = calls["n"]
            if n < 3:
                # MCP up but provision_workspace ABSENT -> not ready, keep retrying.
                return ["mcp__molecule-platform__list_workspaces"]
            if n == 3:
                # plugin reconcile delivered provision_workspace -> ready.
                return [PROVISION_ID]
            # n >= 4: provision_workspace UNREGISTERED again while the server stays up.
            lost.set()
            return ["mcp__molecule-platform__list_workspaces"]

        pr = probe.MCPReadinessProber(retry_interval=0.01, interval=0.01, probe=_probe)
        pr.start()
        assert lost.wait(5.0), (
            "prober stopped after the first success — it did not keep probing to "
            "observe the post-online provision_workspace loss"
        )
        # Let the loss-probe's publish land, then stop.
        threading.Event().wait(0.2)
        pr.stop()
        assert calls["n"] >= 4, f"prober did not keep re-probing after success (calls={calls['n']})"
        # The live SIGNAL tracks the loss — this is the thing core degrades on.
        assert pai.mcp_tools_ready() is False, (
            "mcp_tools_ready stayed frozen True after provision_workspace was lost — "
            "the signal is not live"
        )
        # loaded_mcp_tools is FROZEN at the ready list once we published ready ([5]):
        # the loss probe's management-only subset must NOT have clobbered it (in
        # production the per-turn producer owns it from first-ready on). If [5]
        # regressed, loaded would track the prober again and this would be False.
        assert probe.management_provision_ready(pai.loaded_mcp_tools()) is True, (
            "loaded_mcp_tools was clobbered by the prober after first-ready — the "
            "per-turn producer's list is not protected ([5])"
        )
        # RECOVERY CADENCE ([6]): a provision-loss flips _succeeded_once back to False
        # so _run drops to the TIGHT retry until provision returns.
        assert pr._succeeded_once is False, (
            "_succeeded_once stayed True after a provision-loss — recovery would poll "
            "on the slow steady interval instead of the tight retry ([6])"
        )
        # first_ready_at stays sticky (stamped once at the first ready) even though
        # the live readiness flag went back to False.
        assert pai.first_ready_at() is not None

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

    def test_run_keeps_reprobing_after_first_success(self):
        # LIVE-signal contract (fix for the #4457 EV2 availability hole): the prober
        # must KEEP re-probing at the steady interval after the first success so
        # mcp_tools_ready reflects CURRENT reality (a post-online provision loss can
        # then flip it back to False — see
        # test_run_keeps_probing_after_success_and_reflects_provision_loss). The old
        # loop stopped after the first success, freezing the signal sticky-True and
        # blinding core to a later loss. A transient probe FAILURE (None) must leave
        # the holder UNCHANGED — only a real tools/list result writes it — so steady
        # re-probing never false-degrades a healthy box on a blip.
        calls = {"n": 0}
        ready = threading.Event()

        def _probe(_t):
            calls["n"] += 1
            if calls["n"] < 3:
                return None  # MCP not connectable yet -> retry
            if calls["n"] == 3:
                ready.set()
                return [PROVISION_ID]
            return None  # post-success transient probe failures

        pr = probe.MCPReadinessProber(retry_interval=0.01, interval=0.01, probe=_probe)
        pr.start()
        assert ready.wait(5.0), "prober never reached its first success"
        threading.Event().wait(0.2)
        # It kept re-probing after success (thread alive, calls growing past 3).
        assert calls["n"] > 3, (
            f"prober stopped re-probing after first success (calls={calls['n']}); "
            "the signal is no longer live"
        )
        assert pr._thread is not None and pr._thread.is_alive(), (
            "prober thread terminated after first success — it must keep the signal live"
        )
        # Transient None probes did NOT clobber the holder — still provision-ready.
        assert pai.loaded_mcp_tools() == [PROVISION_ID]
        assert pai.mcp_tools_ready() is True
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
