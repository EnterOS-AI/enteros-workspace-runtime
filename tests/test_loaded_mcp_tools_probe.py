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
import sys
import textwrap
import time

import pytest

from molecule_runtime import loaded_mcp_tools_probe as probe
from molecule_runtime.platform_agent_identity import (
    identity_gate_payload,
    loaded_mcp_tools,
    set_loaded_mcp_tools,
)


@pytest.fixture(autouse=True)
def _reset_producer():
    set_loaded_mcp_tools(None)
    yield
    set_loaded_mcp_tools(None)


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
    """Write a claude settings.json declaring the given {name: spec} servers and
    return the config_path root (tmp_path) the claude reader resolves from
    (<config_path>/.claude/settings.json)."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"mcpServers": servers}))
    return str(tmp_path)


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

        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)

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
            "claude-code", config_root, force=True
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
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
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
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
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
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
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
            "claude-code", config_root, force=True
        )
        assert observed == []
        assert loaded_mcp_tools() == []
        assert identity_gate_payload()["loaded_mcp_tools"] == []


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
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
        # Only declared server is broken -> NOTHING enumerated -> None (not []),
        # so the heartbeat omits the field and core's grace window applies.
        assert ids is None

    def test_unspawnable_command_yields_none(self, tmp_path):
        config_root = _claude_settings_with(
            tmp_path,
            {"molecule-platform": {"command": "/nonexistent/definitely-not-a-binary"}},
        )
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
        assert ids is None

    def test_no_command_in_spec_is_skipped(self, tmp_path):
        # An http/url-transport server (no stdio command) is skipped, not crashed.
        config_root = _claude_settings_with(
            tmp_path, {"remote": {"url": "https://example/mcp"}}
        )
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
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
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
        # The good server still reports; the broken one is silently dropped.
        assert ids == ["mcp__molecule-platform__create_workspace"]

    def test_no_servers_declared_yields_none(self, tmp_path):
        config_root = _claude_settings_with(tmp_path, {})
        ids = probe.enumerate_loaded_mcp_tools("claude-code", config_root)
        assert ids is None

    def test_unreadable_config_yields_none(self, tmp_path):
        # No .claude/settings.json at all -> reader returns {} -> None.
        ids = probe.enumerate_loaded_mcp_tools("claude-code", str(tmp_path))
        assert ids is None

    @pytest.mark.asyncio
    async def test_capture_leaves_producer_none_on_no_observation(self, tmp_path):
        """(b) None observation -> producer stays None -> gate payload OMITS key."""
        config_root = _claude_settings_with(tmp_path, {})
        observed = await probe.capture_loaded_mcp_tools_at_init(
            "claude-code", config_root, force=True
        )
        assert observed is None
        assert loaded_mcp_tools() is None
        assert "loaded_mcp_tools" not in identity_gate_payload()

    @pytest.mark.asyncio
    async def test_capture_never_raises_even_if_reader_explodes(self, tmp_path, monkeypatch):
        # Defense-in-depth: an unexpected error inside enumeration is swallowed.
        def _boom(*_a, **_k):
            raise RuntimeError("simulated reader failure")

        monkeypatch.setattr(probe, "read_mcp_servers", _boom)
        observed = await probe.capture_loaded_mcp_tools_at_init(
            "claude-code", str(tmp_path), force=True
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
        ids = await probe.enumerate_loaded_mcp_tools_async("claude-code", config_root)
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
            probe.enumerate_loaded_mcp_tools, "claude-code", config_root
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
        ids = await probe.enumerate_loaded_mcp_tools_async("claude-code", config_root)
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

        called = {"enum": False}
        real_enum = probe.enumerate_loaded_mcp_tools_async

        async def _spy(*a, **k):
            called["enum"] = True
            return await real_enum(*a, **k)

        monkeypatch.setattr(probe, "enumerate_loaded_mcp_tools_async", _spy)

        observed = await probe.capture_loaded_mcp_tools_at_init("claude-code", config_root)

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

        observed = await probe.capture_loaded_mcp_tools_at_init("claude-code", config_root)

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

        observed = await probe.capture_loaded_mcp_tools_at_init("claude-code", config_root)

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
            "claude-code", config_root, force=True
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


# ---------------------------------------------------------------------------
# read_mcp_servers_for — the native-config readers (inverse of renderers)
# ---------------------------------------------------------------------------

class TestReadMcpServersFor:
    def test_claude_reads_settings_json(self, tmp_path):
        from molecule_runtime.mcp_render import read_mcp_servers_for

        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {"mcpServers": {"molecule-platform": {"command": "npx", "args": ["x"]}}}
            )
        )
        got = read_mcp_servers_for("claude-code", str(tmp_path))
        assert got == {"molecule-platform": {"command": "npx", "args": ["x"]}}

    def test_codex_reads_config_toml(self, tmp_path, monkeypatch):
        from molecule_runtime.mcp_render import read_mcp_servers_for, render_codex_config

        # Point ~/.codex at a temp HOME and render a server, then read it back.
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = tmp_path / ".codex" / "config.toml"
        render_codex_config(
            cfg, "molecule-platform",
            {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"],
             "env": {"MOLECULE_MCP_MODE": "management"}},
        )
        got = read_mcp_servers_for("codex", str(tmp_path))
        assert "molecule-platform" in got
        assert got["molecule-platform"]["command"] == "npx"
        assert got["molecule-platform"]["args"] == ["-y", "@molecule-ai/mcp-server"]
        assert got["molecule-platform"]["env"] == {"MOLECULE_MCP_MODE": "management"}

    def test_unverified_runtime_reads_empty(self, tmp_path):
        from molecule_runtime.mcp_render import read_mcp_servers_for

        assert read_mcp_servers_for("hermes", str(tmp_path)) == {}
        # An unmapped/unknown runtime also fails closed to {}.
        assert read_mcp_servers_for("some-unmapped-runtime", str(tmp_path)) == {}

    def test_missing_config_reads_empty(self, tmp_path):
        from molecule_runtime.mcp_render import read_mcp_servers_for

        assert read_mcp_servers_for("claude-code", str(tmp_path)) == {}


class TestRetryUntilReady:
    """capture_loaded_mcp_tools_with_retry: no-turn fix — keep enumerating in the
    background until the management MCP becomes connectable, then publish."""

    @pytest.mark.asyncio
    async def test_succeeds_when_mcp_becomes_ready_late(self, monkeypatch):
        # Simulate the real timing: the MCP isn't connectable for the first 2
        # attempts (init enumeration finds nothing -> None), then becomes ready.
        calls = {"n": 0}
        result = ["mcp__molecule-platform__create_workspace"]

        async def _fake_capture(runtime, config_path, *, force=False):
            calls["n"] += 1
            return None if calls["n"] < 3 else result

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _fake_capture)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            "claude-code", "/cfg", max_attempts=5, interval_seconds=0.01
        )
        assert out == result
        assert calls["n"] == 3, "should stop on first success, not keep retrying"

    @pytest.mark.asyncio
    async def test_gives_up_after_window_without_hanging(self, monkeypatch):
        # MCP never becomes ready -> always None -> give up after max_attempts,
        # returning None (the per-turn capture is then the fallback). Must not hang.
        calls = {"n": 0}

        async def _never(runtime, config_path, *, force=False):
            calls["n"] += 1
            return None

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _never)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            "claude-code", "/cfg", max_attempts=4, interval_seconds=0.01
        )
        assert out is None
        assert calls["n"] == 4, "should attempt exactly max_attempts times"

    @pytest.mark.asyncio
    async def test_swallows_per_attempt_errors_and_keeps_retrying(self, monkeypatch):
        # A transient error on an attempt (e.g. config not written yet) must be
        # swallowed and retried, never crash the background task.
        calls = {"n": 0}
        result = ["mcp__molecule-platform__create_workspace"]

        async def _flaky(runtime, config_path, *, force=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("config not ready")
            if calls["n"] == 2:
                return None
            return result

        monkeypatch.setattr(probe, "capture_loaded_mcp_tools_at_init", _flaky)
        out = await probe.capture_loaded_mcp_tools_with_retry(
            "claude-code", "/cfg", max_attempts=5, interval_seconds=0.01
        )
        assert out == result
        assert calls["n"] == 3
