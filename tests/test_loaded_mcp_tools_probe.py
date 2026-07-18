"""Tests for the init-time loaded_mcp_tools enumeration (core#3082, task #85).

Proves REQUIREMENT 5:
  (a) set_loaded_mcp_tools -> identity_gate_payload includes loaded_mcp_tools
      (covered in test_platform_agent_identity.py; reasserted end-to-end here
      via capture_loaded_mcp_tools_at_init publishing into the gate payload).
  (b) None -> key omitted (capture leaves the producer None on no-observation).
  (c) the init enumeration produces correctly-namespaced ids incl.
      mcp__molecule-platform__create_workspace given a fake MCP server.
  (d) a broken server leaves the producer None + doesn't crash.
"""

import asyncio
import json
import os
import sys
import textwrap
import time

import pytest
import yaml

from molecule_runtime import loaded_mcp_tools_probe as probe
from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter
from molecule_runtime.platform_agent_identity import (
    identity_gate_payload,
    loaded_mcp_tools,
    set_loaded_mcp_tools,
)
from molecule_runtime.privileged_mcp_env import TENANT_FORBIDDEN_ENV_KEYS


class _ProbeAdapter(BaseAdapter):
    """Minimal concrete adapter for the capture tests. name()=="claude-code" and
    the inherited BaseAdapter default for ``enumerate_loaded_mcp_tools`` reads the
    generic JSON ``mcpServers`` native config (``<config_path>/.claude/settings.json``,
    the base fallback default) and feeds the resolved specs to the surviving generic
    engine ``enumerate_from_specs_async`` — so passing ``_ProbeAdapter()`` exercises
    the exact read→probe engine the deleted by-name ``("claude-code", config_path)``
    signature did, now through the ADR-004 runtime-owns-discovery contract (the
    engine no longer resolves servers by runtime name)."""

    def __init__(self, name="claude-code"):
        self._name = name

    def name(self):
        return self._name

    @classmethod
    def display_name(cls):
        return "Probe"

    @classmethod
    def description(cls):
        return "probe-test"

    def setup(self, config):  # pragma: no cover - stub
        return None

    def create_executor(self, config):  # pragma: no cover - stub
        return None


def _cfg(config_root):
    """AdapterConfig carrying just the configs dir the probe reads."""
    return AdapterConfig(model="test", config_path=str(config_root))


@pytest.fixture(autouse=True)
def _reset_producer():
    set_loaded_mcp_tools(None)
    # The launch-failure REFUSE-ONLINE signal is a module-global. Real-subprocess
    # tests in this file spawn servers that exit non-zero and legitimately RECORD a
    # reason; without this reset it leaks into later tests and (correctly) trips the
    # retry loop's new hard-fail short-circuit. Mirror test_mcp_launch_failure_alarm's
    # _reset_launch_signal so every test starts from a clean signal.
    probe.record_launch_failure(None)
    yield
    set_loaded_mcp_tools(None)
    probe.record_launch_failure(None)


# ---------------------------------------------------------------------------
# A real-ish stdio MCP server written as a tiny Python script. It speaks the
# minimal initialize -> tools/list handshake the probe drives, so the test
# exercises the actual subprocess + JSON-RPC path rather than mocking it out.
# ---------------------------------------------------------------------------

def _write_fake_server(
    tmp_path, *, tools, name="server.py", broken=False, hang=False,
    hang_after_init=False,
):
    """Write a fake stdio MCP server script and return its path.

    tools: list of bare tool names the server advertises on tools/list.
    broken: exit immediately (spawns but never handshakes) -> probe sees None.
    hang: read forever without answering ANYTHING -> probe times out -> None.
    hang_after_init: answer `initialize` correctly, then HANG on `tools/list`
        (write nothing, keep reading stdin + hold stdout open). This is the exact
        core#3082 boot-stall reproduction the must-fix targets — a server that
        looks alive at handshake but never returns its tool list (e.g. the
        management MCP when the control plane is slow/unreachable). The probe's
        per-read asyncio timeout must trip here instead of blocking boot.
    """
    if broken:
        body = "import sys; sys.exit(3)\n"
    elif hang:
        body = "import sys\nfor _ in sys.stdin:\n    pass\n"
    elif hang_after_init:
        body = textwrap.dedent(
            """
            import sys, json
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
                    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                        "result": {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "serverInfo": {"name": "fake", "version": "1"}}}),
                        flush=True)
                elif method == "notifications/initialized":
                    pass
                elif method == "tools/list":
                    # The stall: do NOT answer. Keep draining stdin (so the
                    # probe's stdin.drain() never blocks) but write nothing and
                    # hold stdout open. With a blocking readline the probe would
                    # hang here forever; with the per-read asyncio timeout it
                    # trips and treats us as not-loaded.
                    for _ in sys.stdin:
                        pass
                    break
            """
        )
    else:
        tool_objs = [{"name": t} for t in tools]
        body = textwrap.dedent(
            f"""
            import sys, json
            TOOLS = {json.dumps(tool_objs)}
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({{"jsonrpc": "2.0", "id": mid,
                        "result": {{"protocolVersion": "2024-11-05",
                                    "capabilities": {{}},
                                    "serverInfo": {{"name": "fake", "version": "1"}}}}}}),
                        flush=True)
                elif method == "notifications/initialized":
                    pass  # no response
                elif method == "tools/list":
                    print(json.dumps({{"jsonrpc": "2.0", "id": mid,
                        "result": {{"tools": TOOLS}}}}), flush=True)
                    # server can keep running; probe terminates it
            """
        )
    path = tmp_path / name
    path.write_text(body)
    return path


def _claude_settings_with(tmp_path, servers: dict):
    """Write a JSON settings.json declaring the given {name: spec} servers and
    return the config_path root (tmp_path) the BaseAdapter default reader resolves
    from (<config_path>/.claude/settings.json — the generic JSON native config)."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"mcpServers": servers}))
    return str(tmp_path)


# ADR-004 migration: the deleted by-name engine entrypoints
# ``probe.enumerate_loaded_mcp_tools[_async](runtime, config_path)`` are replaced by
# routing through the ADAPTER (adapter reads its own native config, feeds the
# generic ``enumerate_from_specs_async`` engine). ``_ProbeAdapter`` uses the
# BaseAdapter default, which reads the generic JSON ``mcpServers`` config seeded by
# ``_claude_settings_with`` — so these helpers exercise the identical read→probe
# engine + boot-safety machinery the old by-name signature did.

async def _enumerate_root_async(config_root):
    """Adapter-routed async enumeration for a seeded ``.claude/settings.json`` root
    (the ADR-004 replacement for ``probe.enumerate_loaded_mcp_tools_async``)."""
    return await _ProbeAdapter().enumerate_loaded_mcp_tools(_cfg(config_root))


def _enumerate_root_sync(config_root):
    """Sync adapter-routed enumeration (the ADR-004 replacement for the sync
    ``probe.enumerate_loaded_mcp_tools``); drives the async path under
    ``asyncio.run`` exactly as the deleted sync wrapper did."""
    return asyncio.run(_enumerate_root_async(config_root))


# ---------------------------------------------------------------------------
# (c) init enumeration produces correctly-namespaced ids incl. create_workspace
# ---------------------------------------------------------------------------

class TestInitEnumerationHappyPath:
    def test_enumerates_and_namespaces_create_workspace(self, tmp_path):
        server = _write_fake_server(
            tmp_path,
            tools=["create_workspace", "send_message"],
        )
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )

        ids = _enumerate_root_sync(config_root)

        assert ids == [
            "mcp__molecule-platform__create_workspace",
            "mcp__molecule-platform__send_message",
        ]
        # The exact id the core gate keys on must be present.
        assert "mcp__molecule-platform__create_workspace" in ids

    @pytest.mark.asyncio
    async def test_capture_publishes_into_gate_payload(self, tmp_path):
        """(a) end-to-end: capture -> set_loaded_mcp_tools -> gate payload."""
        server = _write_fake_server(tmp_path, tools=["create_workspace"])
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )

        # force=True bypasses the kind=platform gate (gate is exercised separately
        # in TestKindPlatformGate); this asserts the publish path end-to-end.
        observed = await probe.capture_loaded_mcp_tools_at_init(
            _ProbeAdapter(), _cfg(config_root), force=True
        )

        assert observed == ["mcp__molecule-platform__create_workspace"]
        assert loaded_mcp_tools() == ["mcp__molecule-platform__create_workspace"]
        assert identity_gate_payload()["loaded_mcp_tools"] == [
            "mcp__molecule-platform__create_workspace"
        ]

    def test_already_namespaced_ids_passed_through(self, tmp_path):
        # A server that self-namespaces its tools must not be double-prefixed.
        server = _write_fake_server(
            tmp_path, tools=["mcp__molecule-platform__create_workspace"]
        )
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )
        ids = _enumerate_root_sync(config_root)
        assert ids == ["mcp__molecule-platform__create_workspace"]

    def test_union_across_multiple_servers_sorted_deduped(self, tmp_path):
        a = _write_fake_server(tmp_path, tools=["create_workspace"], name="a.py")
        b = _write_fake_server(tmp_path, tools=["send_message"], name="b.py")
        config_root = _claude_settings_with(
            tmp_path,
            {
                "molecule-platform": {"command": sys.executable, "args": [str(a)]},
                "a2a": {"command": sys.executable, "args": [str(b)]},
            },
        )
        ids = _enumerate_root_sync(config_root)
        assert ids == [
            "mcp__a2a__send_message",
            "mcp__molecule-platform__create_workspace",
        ]


# ---------------------------------------------------------------------------
# Empty-list semantics: a server connects but advertises zero tools => []
# ---------------------------------------------------------------------------

class TestConnectedButToolless:
    def test_empty_tools_is_observed_empty_list_not_none(self, tmp_path):
        server = _write_fake_server(tmp_path, tools=[])
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )
        ids = _enumerate_root_sync(config_root)
        # Genuine "connected, no tools" -> [], a meaningful non-None signal.
        assert ids == []

    @pytest.mark.asyncio
    async def test_capture_publishes_empty_list_when_truly_observed(self, tmp_path):
        server = _write_fake_server(tmp_path, tools=[])
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )
        observed = await probe.capture_loaded_mcp_tools_at_init(
            _ProbeAdapter(), _cfg(config_root), force=True
        )
        assert observed == []
        assert loaded_mcp_tools() == []
        assert identity_gate_payload()["loaded_mcp_tools"] == []


@pytest.mark.asyncio
async def test_spawn_env_strips_tenant_forbidden_capability_from_ambient_and_spec(
    monkeypatch,
):
    """The real subprocess seam must not trust either parent env or descriptor.

    Rendering already strips the descriptor in normal setup, but boot probes also
    accept specs directly and begin from ``os.environ``. Assert the exact env passed
    to ``create_subprocess_exec`` so a rendered-spec-only test cannot false-green.
    """
    forbidden = TENANT_FORBIDDEN_ENV_KEYS[0]
    monkeypatch.setenv(forbidden, "ambient-production-capability")
    captured = {}

    class _ExitedProc:
        returncode = 0

    async def _capture_spawn(*_argv, **kwargs):
        captured["env"] = kwargs["env"]
        return _ExitedProc()

    async def _connected_without_tools(_proc, _server):
        return []

    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", _capture_spawn)
    monkeypatch.setattr(probe, "_handshake", _connected_without_tools)

    result = await probe._list_tools_from_mcp_server(
        "molecule-platform",
        {"command": "fake-mcp", "env": {forbidden: "descriptor-capability"}},
    )

    assert result == []
    assert forbidden not in captured["env"]


# ---------------------------------------------------------------------------
# (d) broken-server enumeration leaves None + doesn't crash
# ---------------------------------------------------------------------------

class TestDegradeSafe:
    def test_broken_server_yields_none_and_does_not_crash(self, tmp_path):
        server = _write_fake_server(tmp_path, tools=[], broken=True)
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )
        ids = _enumerate_root_sync(config_root)
        # Only declared server is broken -> NOTHING enumerated -> None (not []),
        # so the heartbeat omits the field and core's grace window applies.
        assert ids is None

    def test_unspawnable_command_yields_none(self, tmp_path):
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": "/nonexistent/definitely-not-a-binary"}},
        )
        ids = _enumerate_root_sync(config_root)
        assert ids is None

    def test_no_command_in_spec_is_skipped(self, tmp_path):
        # An http/url-transport server (no stdio command) is skipped, not crashed.
        config_root = _claude_settings_with(
            tmp_path, {"remote": {"url": "https://example/mcp"}}
        )
        ids = _enumerate_root_sync(config_root)
        assert ids is None

    def test_one_broken_one_good_reports_only_the_good(self, tmp_path):
        good = _write_fake_server(tmp_path, tools=["create_workspace"], name="good.py")
        bad = _write_fake_server(tmp_path, tools=[], broken=True, name="bad.py")
        config_root = _claude_settings_with(
            tmp_path,
            {
                "molecule-platform": {"command": sys.executable, "args": [str(good)]},
                "broken": {"command": sys.executable, "args": [str(bad)]},
            },
        )
        ids = _enumerate_root_sync(config_root)
        # The good server still reports; the broken one is silently dropped.
        assert ids == ["mcp__molecule-platform__create_workspace"]

    def test_no_servers_declared_yields_none(self, tmp_path):
        config_root = _claude_settings_with(tmp_path, {})
        ids = _enumerate_root_sync(config_root)
        assert ids is None

    def test_unreadable_config_yields_none(self, tmp_path):
        # No .claude/settings.json at all -> reader returns {} -> None.
        ids = _enumerate_root_sync(str(tmp_path))
        assert ids is None

    @pytest.mark.asyncio
    async def test_capture_leaves_producer_none_on_no_observation(self, tmp_path):
        """(b) None observation -> producer stays None -> gate payload OMITS key."""
        config_root = _claude_settings_with(tmp_path, {})
        observed = await probe.capture_loaded_mcp_tools_at_init(
            _ProbeAdapter(), _cfg(config_root), force=True
        )
        assert observed is None
        assert loaded_mcp_tools() is None
        assert "loaded_mcp_tools" not in identity_gate_payload()

    @pytest.mark.asyncio
    async def test_capture_never_raises_even_if_reader_explodes(self, tmp_path, monkeypatch):
        # Defense-in-depth: an unexpected error inside enumeration is swallowed.
        # ADR-004: the BaseAdapter default reads via mcp_render.read_json_mcp_servers
        # (the deleted probe.read_mcp_servers by-name indirection is gone). Blow up
        # THAT reader and assert capture still maps to None (never raises into boot).
        def _boom(*_a, **_k):
            raise RuntimeError("simulated reader failure")

        import molecule_runtime.mcp_render as _mcp_render
        monkeypatch.setattr(_mcp_render, "read_json_mcp_servers", _boom)
        observed = await probe.capture_loaded_mcp_tools_at_init(
            _ProbeAdapter(), _cfg(tmp_path), force=True
        )
        assert observed is None
        assert loaded_mcp_tools() is None


# ---------------------------------------------------------------------------
# BOOT-SAFETY (core#3082 must-fix): a server that answers `initialize` then
# STALLS on `tools/list` must NOT hang boot. The probe's per-read asyncio
# timeout must trip and map it to None within a tight bound — proving no-hang.
# ---------------------------------------------------------------------------

class TestBootSafetyStall:
    @pytest.mark.asyncio
    async def test_stall_after_init_returns_none_without_hanging(
        self, tmp_path, monkeypatch
    ):
        """The regression test for the boot-blocker.

        Without the async per-read timeout, the probe's blocking readline() on the
        unanswered tools/list would hang forever (had to SIGKILL, exit 124). With
        it, the stalled server is killed and reported as not-loaded (None), and
        the whole enumeration completes well under a tight wall-clock bound.
        """
        # Tighten the timeouts so the test is fast AND so a regression (blocking
        # read) would visibly blow the wall-clock assertion rather than pass.
        monkeypatch.setattr(probe, "_MCP_READ_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(probe, "_MCP_HANDSHAKE_TIMEOUT_SECONDS", 2.0)
        monkeypatch.setattr(probe, "_MCP_ENUMERATION_TIMEOUT_SECONDS", 4.0)

        server = _write_fake_server(tmp_path, tools=[], hang_after_init=True)
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )

        started = time.monotonic()
        ids = await _enumerate_root_async(config_root)
        elapsed = time.monotonic() - started

        # Stalled server -> not-loaded -> None (so the grace window applies).
        assert ids is None
        # PROOF OF NO-HANG: completes within a few seconds (well under the old
        # forever-block). Generous ceiling vs. the 1s/2s/4s configured budgets so
        # CI subprocess-spawn jitter doesn't flake, but tiny vs. "infinite".
        assert elapsed < 8.0, f"enumeration took {elapsed:.1f}s — boot-stall regression"

    @pytest.mark.asyncio
    async def test_sync_wrapper_also_bounded_on_stall(self, tmp_path, monkeypatch):
        """The sync entry point (asyncio.run wrapper) is equally non-hanging."""
        monkeypatch.setattr(probe, "_MCP_READ_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(probe, "_MCP_HANDSHAKE_TIMEOUT_SECONDS", 2.0)
        monkeypatch.setattr(probe, "_MCP_ENUMERATION_TIMEOUT_SECONDS", 4.0)

        server = _write_fake_server(tmp_path, tools=[], hang_after_init=True)
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )

        started = time.monotonic()
        # enumerate_loaded_mcp_tools is sync (asyncio.run under the hood); run it
        # off-thread so it can't fight this test's running event loop.
        ids = await asyncio.to_thread(
            _enumerate_root_sync, config_root
        )
        elapsed = time.monotonic() - started
        assert ids is None
        assert elapsed < 8.0, f"sync wrapper took {elapsed:.1f}s — boot-stall regression"

    @pytest.mark.asyncio
    async def test_overall_deadline_bounds_many_stalling_servers(
        self, tmp_path, monkeypatch
    ):
        """Even a fleet of stalling servers is bounded by the overall deadline."""
        monkeypatch.setattr(probe, "_MCP_READ_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(probe, "_MCP_HANDSHAKE_TIMEOUT_SECONDS", 2.0)
        monkeypatch.setattr(probe, "_MCP_ENUMERATION_TIMEOUT_SECONDS", 3.0)

        servers = {}
        for i in range(5):
            s = _write_fake_server(
                tmp_path, tools=[], hang_after_init=True, name=f"stall{i}.py"
            )
            servers[f"stall{i}"] = {"command": sys.executable, "args": [str(s)]}
        config_root = _claude_settings_with(tmp_path, servers)

        started = time.monotonic()
        ids = await _enumerate_root_async(config_root)
        elapsed = time.monotonic() - started
        assert ids is None
        # 5 servers x 2s per-server would be 10s sequentially; the 3s OVERALL
        # deadline must cut it short well before that.
        assert elapsed < 8.0, f"overall deadline not enforced ({elapsed:.1f}s)"


# ---------------------------------------------------------------------------
# kind=platform GATE (REQUIREMENT 3): capture_loaded_mcp_tools_at_init only
# enumerates for the concierge (on_platform_agent_image); every other workspace
# skips it (no spawn, no enumeration), so tenants declaring MCP servers don't
# pay the cost or amplify the hang blast-radius.
# ---------------------------------------------------------------------------

class TestKindPlatformGate:
    @pytest.mark.asyncio
    async def test_non_platform_workspace_skips_enumeration(self, tmp_path, monkeypatch):
        # Ordinary tenant: NOT the baked image AND no management molecule-platform
        # MCP wired in (the plugin is org-root entitlement-gated, #50). Both gate
        # signals are False -> enumeration must be skipped entirely, even though
        # the tenant may declare OTHER (non-management) MCP servers.
        monkeypatch.delenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", raising=False)
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.mcp_server_present",
            lambda: False,
        )

        # A non-management MCP (image-gen) is declared — the gate must STILL skip:
        # only the concierge (management MCP present) enumerates.
        server = _write_fake_server(tmp_path, tools=["generate_image"])
        config_root = _claude_settings_with(
            tmp_path,
            {"image-gen": {"command": sys.executable, "args": [str(server)]}},
        )

        # ADR-004: the kind=platform gate short-circuits BEFORE the adapter's
        # enumerate runs, so the surviving generic engine core (_probe_specs_async,
        # which _ProbeAdapter's base default reaches via enumerate_from_specs_async)
        # must never be entered on a non-platform workspace. Spy on it to prove the
        # skip (the deleted by-name enumerate_loaded_mcp_tools_async is gone).
        called = {"enum": False}
        real_engine = probe._probe_specs_async

        async def _spy(servers):
            called["enum"] = True
            return await real_engine(servers)

        monkeypatch.setattr(probe, "_probe_specs_async", _spy)

        observed = await probe.capture_loaded_mcp_tools_at_init(_ProbeAdapter(), _cfg(config_root))

        assert observed is None
        assert called["enum"] is False, "enumeration ran on a non-platform workspace"
        assert loaded_mcp_tools() is None
        assert "loaded_mcp_tools" not in identity_gate_payload()

    @pytest.mark.asyncio
    async def test_debaked_concierge_runs_via_mcp_server_present(self, tmp_path, monkeypatch):
        # THE de-bake regression test. The de-baked concierge runs the STANDARD
        # runtime image -> MOLECULE_PLATFORM_AGENT_IMAGE_BAKED is ABSENT
        # (on_platform_agent_image() == False). The gate must STILL run because
        # the management MCP is present (mcp_server_present() == True). Before the
        # fix this skipped -> producer None -> concierge stuck degraded forever.
        monkeypatch.delenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", raising=False)
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.mcp_server_present",
            lambda: True,
        )

        server = _write_fake_server(tmp_path, tools=["create_workspace"])
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )

        observed = await probe.capture_loaded_mcp_tools_at_init(_ProbeAdapter(), _cfg(config_root))

        assert observed == ["mcp__molecule-platform__create_workspace"]
        assert loaded_mcp_tools() == ["mcp__molecule-platform__create_workspace"]
        assert identity_gate_payload()["loaded_mcp_tools"] == [
            "mcp__molecule-platform__create_workspace"
        ]

    @pytest.mark.asyncio
    async def test_baked_concierge_runs_via_legacy_marker(self, tmp_path, monkeypatch):
        # Legacy baked concierge: the marker is set even though mcp_server_present
        # is forced False here -> the OR'd legacy fallback must still run it.
        monkeypatch.setenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", "1")
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.mcp_server_present",
            lambda: False,
        )

        server = _write_fake_server(tmp_path, tools=["create_workspace"])
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )

        observed = await probe.capture_loaded_mcp_tools_at_init(_ProbeAdapter(), _cfg(config_root))

        assert observed == ["mcp__molecule-platform__create_workspace"]
        assert identity_gate_payload()["loaded_mcp_tools"] == [
            "mcp__molecule-platform__create_workspace"
        ]

    @pytest.mark.asyncio
    async def test_force_bypasses_the_gate(self, tmp_path, monkeypatch):
        # force=True (used by the happy-path tests) runs enumeration regardless of
        # BOTH gate signals being off.
        monkeypatch.delenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", raising=False)
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.mcp_server_present",
            lambda: False,
        )
        server = _write_fake_server(tmp_path, tools=["create_workspace"])
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": sys.executable, "args": [str(server)]}},
        )
        observed = await probe.capture_loaded_mcp_tools_at_init(
            _ProbeAdapter(), _cfg(config_root), force=True
        )
        assert observed == ["mcp__molecule-platform__create_workspace"]


# ---------------------------------------------------------------------------
# Normalization unit (no subprocess)
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_bare_name_prefixed(self):
        assert (
            probe._normalize_tool_id("molecule-platform", "create_workspace")
            == "mcp__molecule-platform__create_workspace"
        )

    def test_already_namespaced_unchanged(self):
        assert (
            probe._normalize_tool_id("ignored", "mcp__a2a__send_message")
            == "mcp__a2a__send_message"
        )

    def test_build_server_command_none_without_command(self):
        assert probe._build_server_command({"url": "x"}) is None
        assert probe._build_server_command({"command": ""}) is None

    def test_build_server_command_with_args(self):
        assert probe._build_server_command(
            {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]}
        ) == ["npx", "-y", "@molecule-ai/mcp-server"]


# NOTE (ADR-004): the ``TestReadMcpServersFor`` class that tested the deleted
# by-name reader switch ``mcp_render.read_mcp_servers_for(runtime, config_path)``
# (via the deleted ``render_codex_config`` / ``render_hermes_config`` engine
# renderers) is GONE. Per-runtime native-config read/render is now OWNED by each
# adapter (in its template repo) and proven by the SDK conformance suite's
# render→read→present round-trip (``molecule_plugin.adapter_conformance`` — each
# template's ``tests/test_conformance.py`` runs 16 checks against its own Adapter).
# The engine keeps only the GENERIC JSON reader ``mcp_render.read_json_mcp_servers``
# (fail-closed coverage in ``tests/test_mcp_render_generic_helpers.py``), which the
# BaseAdapter default uses and which the boot-safety tests above exercise via
# ``_ProbeAdapter``.


class TestRetryUntilReady:
    """capture_loaded_mcp_tools_with_retry: no-turn fix — keep enumerating in the
    background until the management MCP becomes connectable, then publish."""

    @pytest.mark.asyncio
    async def test_succeeds_when_mcp_becomes_ready_late(self, monkeypatch):
        # Simulate the real timing: the MCP isn't connectable for the first 2
        # attempts (init enumeration finds nothing -> None), then becomes ready.
        calls = {"n": 0}
        result = ["mcp__molecule-platform__create_workspace"]

        async def _fake_capture(adapter, config, *, force=False, **_kw):
            calls["n"] += 1
            return None if calls["n"] < 3 else result

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _fake_capture)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            _ProbeAdapter(), _cfg("/cfg"), max_attempts=5, interval_seconds=0.01
        )
        assert out == result
        assert calls["n"] == 3, "should stop on first success, not keep retrying"

    @pytest.mark.asyncio
    async def test_gives_up_after_window_without_hanging(self, monkeypatch):
        # MCP never becomes ready -> always None -> give up after max_attempts,
        # returning None (the per-turn capture is then the fallback). Must not hang.
        calls = {"n": 0}

        async def _never(adapter, config, *, force=False, **_kw):
            calls["n"] += 1
            return None

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _never)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            _ProbeAdapter(), _cfg("/cfg"), max_attempts=4, interval_seconds=0.01
        )
        assert out is None
        assert calls["n"] == 4, "should attempt exactly max_attempts times"

    @pytest.mark.asyncio
    async def test_swallows_per_attempt_errors_and_keeps_retrying(self, monkeypatch):
        # A transient error on an attempt (e.g. config not written yet) must be
        # swallowed and retried, never crash the background task.
        calls = {"n": 0}
        result = ["mcp__molecule-platform__create_workspace"]

        async def _flaky(adapter, config, *, force=False, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("config not ready")
            if calls["n"] == 2:
                return None
            return result

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _flaky)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            _ProbeAdapter(), _cfg("/cfg"), max_attempts=5, interval_seconds=0.01
        )
        assert out == result
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_hard_launch_failure_short_circuits_the_retry_loop(self, monkeypatch):
        # runtime EV4: a DETERMINISTIC hard launch-failure (npx ETARGET: the pinned
        # mcp-server version is unresolvable on this image) will NEVER self-heal, so
        # the moment _classify_launch_failure records launch_failure_reason() the
        # retry loop must STOP — not re-spin all remaining attempts into the grace
        # window. The recorded reason is the refuse-online signal core reads.
        calls = {"n": 0}

        async def _fail_then_record(adapter, config, *, force=False, **_kw):
            calls["n"] += 1
            # The real init path records the reason as a side effect when it sees
            # the child exit non-zero; simulate that recording here.
            probe.record_launch_failure("mcp-server: exit=1 ETARGET")
            return None

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _fail_then_record)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            _ProbeAdapter(), _cfg("/cfg"), max_attempts=40, interval_seconds=0.01
        )
        assert out is None
        # Stopped after the FIRST attempt that recorded a hard fail — not 40.
        assert calls["n"] == 1, "should stop on the deterministic hard fail, not retry"
        # The refuse-online reason remains recorded for the heartbeat to surface.
        assert probe.launch_failure_reason() == "mcp-server: exit=1 ETARGET"

    @pytest.mark.asyncio
    async def test_transient_miss_without_launch_failure_still_retries(self, monkeypatch):
        # Guard the discrimination: a plain None (transient stall, no recorded
        # launch failure) must NOT short-circuit — the loop keeps retrying and
        # succeeds late, exactly as before EV4.
        calls = {"n": 0}
        result = ["mcp__molecule-platform__create_workspace"]

        async def _miss_then_ok(adapter, config, *, force=False, **_kw):
            calls["n"] += 1
            return None if calls["n"] < 3 else result

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _miss_then_ok)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            _ProbeAdapter(), _cfg("/cfg"), max_attempts=5, interval_seconds=0.01
        )
        assert out == result
        assert calls["n"] == 3


# ---------- runtime#181: the adapter-owns-discovery contract ----------

class TestAdapterEnumerationContract:
    """runtime#181: capture ALWAYS routes MCP-tool enumeration through the ADAPTER
    contract (adapter.enumerate_loaded_mcp_tools) — each runtime owns discovery,
    replacing the hardcoded core switch. capture takes only (adapter, config); the
    runtime name and configs dir are read off them (adapter.name() /
    config.config_path), never threaded through separately."""

    def teardown_method(self):
        from molecule_runtime.platform_agent_identity import set_loaded_mcp_tools
        set_loaded_mcp_tools(None)

    @pytest.mark.asyncio
    async def test_capture_routes_through_adapter_enumerate(self, monkeypatch):
        from molecule_runtime import platform_agent_identity as pai

        class _FakeAdapter:
            def name(self):
                return "hermes"

            async def enumerate_loaded_mcp_tools(self, config):
                return ["mcp__molecule-platform__provision_workspace"]

        # ADR-004: the deleted by-name engine switch
        # (probe.enumerate_loaded_mcp_tools_async / _probe_specs_async) must NOT be
        # consulted — the adapter owns discovery and returns canned ids WITHOUT
        # touching the engine. Booby-trap the surviving generic engine entry point
        # to prove capture went straight through the adapter, never the engine.
        async def _boom(servers):
            raise AssertionError("engine must not run — adapter owns discovery")
        monkeypatch.setattr(probe, "_probe_specs_async", _boom)

        observed = await probe.capture_loaded_mcp_tools_at_init(
            _FakeAdapter(), _cfg("/configs"), force=True
        )
        assert observed == ["mcp__molecule-platform__provision_workspace"]
        # published to the producer so the first heartbeat carries it
        assert pai.loaded_mcp_tools() == ["mcp__molecule-platform__provision_workspace"]

    @pytest.mark.asyncio
    async def test_capture_never_publishes_on_none_observation(self, monkeypatch):
        from molecule_runtime import platform_agent_identity as pai

        class _NoneAdapter:
            def name(self):
                return "hermes"

            async def enumerate_loaded_mcp_tools(self, config):
                return None  # nothing observed yet — grace window

        observed = await probe.capture_loaded_mcp_tools_at_init(
            _NoneAdapter(), _cfg("/configs"), force=True
        )
        assert observed is None
        assert pai.loaded_mcp_tools() is None

    @pytest.mark.asyncio
    async def test_base_adapter_default_reads_generic_config_and_feeds_engine(
        self, tmp_path, monkeypatch
    ):
        """ADR-004: the BaseAdapter default reads the adapter's OWN native config
        (the generic JSON ``mcpServers`` at ``<config_path>/.claude/settings.json``)
        and hands the resolved specs to the surviving generic engine
        ``enumerate_from_specs_async`` — it NO LONGER calls a by-name
        ``enumerate_loaded_mcp_tools_async(runtime, config_path)`` switch (deleted).
        """
        captured = {}

        async def _engine(servers, launch_env=None):
            captured["servers"] = servers
            captured["launch_env"] = launch_env
            return []
        monkeypatch.setattr(probe, "enumerate_from_specs_async", _engine)

        config_root = _claude_settings_with(
            tmp_path, {"molecule-platform": {"command": "npx", "args": ["x"]}}
        )
        out = await _ProbeAdapter("claude-code").enumerate_loaded_mcp_tools(
            _cfg(config_root)
        )
        # base default fed the engine the specs it read from the generic JSON config
        assert out == []
        assert captured["servers"] == {
            "molecule-platform": {"command": "npx", "args": ["x"]}
        }
        # ADR-004 mcp_launch_env socket: the base default contributes NO overlay
        # ({}), so the spawn inherits the process env (system interpreter on PATH).
        assert captured["launch_env"] == {}


# ---------------------------------------------------------------------------
# ADR-004 mcp_launch_env socket — DYNAMIC, adapter-resolved launch env.
#
# The live bug (hermes): the image bundles Node under $HERMES_HOME/node/bin but
# it is OFF the runtime process PATH, so a bare `npx @molecule-ai/mcp-server`
# child cannot resolve its interpreter and the management MCP never launches.
# These tests reproduce that shape hermetically — a stdio MCP server invoked by a
# BARE command name that is ONLY on PATH via a bin dir OFF the system PATH — and
# prove:
#   * base mcp_launch_env() == {}  -> no injection -> the bare command is
#     UNRESOLVABLE (spawn fails) -> None (the stuck-provisioning shape), and
#   * an adapter that prepends that bin dir to PATH via mcp_launch_env() makes the
#     SAME server resolvable -> its tools enumerate. Node-off-PATH now resolves via
#     the adapter, dynamically at launch, with zero runtime-name in the engine.
# ---------------------------------------------------------------------------


def _write_bare_named_server(tmp_path, *, tools, cmd_name="fake-npx"):
    """Write a fake stdio MCP server + a launcher exposed under a BARE command name
    in a bin dir. Returns ``(bin_dir, cmd_name)``.

    The launcher is invocable ONLY as the bare ``cmd_name`` resolved on PATH (like
    ``npx``), so a child whose PATH lacks ``bin_dir`` cannot spawn it — exactly the
    hermes node-off-PATH failure. The server script speaks the minimal
    initialize -> tools/list handshake (same shape as ``_write_fake_server``)."""
    server = _write_fake_server(tmp_path, tools=tools, name="bare_server.py")
    bin_dir = tmp_path / "bundled-bin"
    bin_dir.mkdir()
    launcher = bin_dir / cmd_name
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{server}" "$@"\n'
    )
    launcher.chmod(0o755)
    return bin_dir, cmd_name


class _LaunchEnvAdapter(_ProbeAdapter):
    """Adapter that injects a PATH overlay via the ADR-004 mcp_launch_env socket —
    the generic stand-in for a runtime (hermes) bundling its interpreter off PATH.
    ``_bin_dir=None`` models the base behaviour (no injection)."""

    def __init__(self, name="hermes", bin_dir=None):
        super().__init__(name=name)
        self._bin_dir = bin_dir

    def mcp_launch_env(self, config):
        if not self._bin_dir:
            return {}
        existing = os.environ.get("PATH", "")
        return {"PATH": f"{self._bin_dir}:{existing}" if existing else str(self._bin_dir)}


class TestAdapterMcpLaunchEnv:
    @pytest.mark.asyncio
    async def test_base_no_injection_leaves_bundled_interpreter_off_path_unresolvable(
        self, tmp_path, monkeypatch
    ):
        """With node/npx OFF the system PATH and the base mcp_launch_env() == {}
        (no injection), a bare-named MCP command cannot be spawned -> None. This IS
        the stuck-provisioning shape the adapter override exists to fix."""
        bin_dir, cmd = _write_bare_named_server(tmp_path, tools=["create_workspace"])
        # Ensure the bundled bin dir is NOT on the process PATH (node off PATH).
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        specs = {"molecule-platform": {"command": cmd}}
        # Base default overlay is {} -> bare cmd unresolvable -> None.
        out = await probe.enumerate_from_specs_async(specs, launch_env={})
        assert out is None
        assert str(bin_dir) not in os.environ.get("PATH", "")

    @pytest.mark.asyncio
    async def test_adapter_launch_env_prepends_bin_dir_and_resolves_node_off_path(
        self, tmp_path, monkeypatch
    ):
        """The adapter's mcp_launch_env() prepends the bundled bin dir to PATH, so
        the SAME bare-named MCP command resolves and its tools enumerate — even
        though the interpreter is OFF the process PATH. Dynamic, adapter-resolved,
        no runtime name in the engine."""
        bin_dir, cmd = _write_bare_named_server(tmp_path, tools=["create_workspace"])
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        specs = {"molecule-platform": {"command": cmd}}
        overlay = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        out = await probe.enumerate_from_specs_async(specs, launch_env=overlay)
        assert out == ["mcp__molecule-platform__create_workspace"]
        # The engine never mutated the parent process PATH — the overlay is
        # child-only (dynamic launch-time resolution, not a global mutation).
        assert str(bin_dir) not in os.environ.get("PATH", "")

    @pytest.mark.asyncio
    async def test_adapter_override_end_to_end_via_enumerate_loaded_mcp_tools(
        self, tmp_path, monkeypatch
    ):
        """End-to-end through the adapter socket: an adapter whose mcp_launch_env()
        returns the bin-dir overlay resolves the off-PATH interpreter, while the
        SAME adapter with no overlay (base behaviour) does not — proving the socket
        is what threads the launch env into the spawn."""
        bin_dir, cmd = _write_bare_named_server(tmp_path, tools=["create_workspace"])
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        # Seed the generic JSON config the base reader consumes (bare command).
        config_root = _claude_settings_with(tmp_path, {"molecule-platform": {"command": cmd}})

        # No overlay -> off-PATH interpreter unresolvable -> None.
        base_out = await _LaunchEnvAdapter(bin_dir=None).enumerate_loaded_mcp_tools(
            _cfg(config_root)
        )
        assert base_out is None

        # Overlay prepends the bundled bin dir -> resolves -> tools enumerate.
        inj_out = await _LaunchEnvAdapter(bin_dir=bin_dir).enumerate_loaded_mcp_tools(
            _cfg(config_root)
        )
        assert inj_out == ["mcp__molecule-platform__create_workspace"]
