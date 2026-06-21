import json
import os
from unittest.mock import patch

import pytest

from molecule_runtime.platform_agent_identity import (
    MANAGEMENT_MCP_NAME,
    MCPSERVER_PATH,
    identity_gate_payload,
    loaded_mcp_tools,
    mcp_server_present,
    set_loaded_mcp_tools,
)


class TestMCPServerPresent:
    def test_true_when_binary_exists(self, tmp_path, monkeypatch):
        fake = tmp_path / "molecule-mcp-server"
        fake.write_text("#!/bin/sh\necho ok")
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH", str(fake)
        )
        # No settings file in play — binary alone must satisfy.
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH",
            str(tmp_path / "no-settings.json"),
        )
        assert mcp_server_present() is True

    def test_false_when_binary_missing_and_no_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
            str(tmp_path / "not-there"),
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH",
            str(tmp_path / "no-settings.json"),
        )
        assert mcp_server_present() is False

    def test_true_when_plugin_wired_settings(self, tmp_path, monkeypatch):
        """The claude-code + plugin concierge has no baked binary; the management
        MCP arrives via settings.json mcpServers."""
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "npx"}}})
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
            str(tmp_path / "not-there"),
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH", str(settings)
        )
        assert mcp_server_present() is True

    def test_false_when_settings_has_other_mcp_only(self, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"a2a": {"command": "x"}}}))
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
            str(tmp_path / "not-there"),
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH", str(settings)
        )
        assert mcp_server_present() is False

    def test_false_when_settings_malformed(self, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        settings.write_text("{ not json")
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
            str(tmp_path / "not-there"),
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH", str(settings)
        )
        assert mcp_server_present() is False

    def test_false_when_top_level_not_dict(self, tmp_path, monkeypatch):
        # A bare JSON list/scalar must not crash the isinstance(data, dict) guard.
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps([MANAGEMENT_MCP_NAME]))
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
            str(tmp_path / "not-there"),
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH", str(settings)
        )
        assert mcp_server_present() is False

    def test_false_when_mcpservers_not_dict(self, tmp_path, monkeypatch):
        # mcpServers present but the wrong type (list) must stay fail-closed.
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": [MANAGEMENT_MCP_NAME]}))
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
            str(tmp_path / "not-there"),
        )
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH", str(settings)
        )
        assert mcp_server_present() is False

    def test_default_path_is_opt_molecule_mcp_server(self):
        assert MCPSERVER_PATH == "/opt/molecule-mcp-server"

    def test_management_mcp_name_matches_plugin(self):
        # Must match the mcpServers entry the molecule-platform plugin writes
        # into settings.json (and what the claude-code executor loads).
        assert MANAGEMENT_MCP_NAME == "molecule-platform"

    def test_identity_gate_payload_shape(self, monkeypatch):
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.mcp_server_present",
            lambda: True,
        )
        assert identity_gate_payload() == {"mcp_server_present": True}


@pytest.fixture
def patch_mcp_present(monkeypatch):
    # The runtime modules import the helper by name, so we must patch the
    # module-local binding used at call sites, not just the source function.
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.mcp_server_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "molecule_runtime.main.mcp_server_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "molecule_runtime.heartbeat.mcp_server_present",
        lambda: True,
    )


class TestRegisterPayloadIncludesMCP:
    def test_main_register_payload_includes_mcp_server_present(
        self, patch_mcp_present,
    ):
        from molecule_runtime.main import register_with_platform

        recorded = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {}

        class _FakeClient:
            async def post(self, url, *, json, headers):
                recorded["json"] = json
                return _FakeResp()

        import asyncio

        asyncio.run(
            register_with_platform(
                _FakeClient(),
                platform_url="http://platform",
                workspace_id="ws-1",
                workspace_url="http://ws-1",
                agent_card={"name": "x"},
                headers={},
                max_attempts=1,
            )
        )

        assert recorded["json"]["mcp_server_present"] is True


class TestHeartbeatPayloadIncludesMCP:
    def test_heartbeat_body_includes_mcp_server_present(self, patch_mcp_present):
        from molecule_runtime.heartbeat import HeartbeatLoop

        hb = HeartbeatLoop("http://platform", "ws-1")
        recorded = {}

        class _FakeResp:
            def json(self):
                return {}

        class _FakeClient:
            def post(self, url, *, json, headers):
                recorded["json"] = json
                return _FakeResp()

        hb._send_heartbeat(_FakeClient())
        assert recorded["json"]["mcp_server_present"] is True


class TestLoadedMCPTools:
    """core#3082: the loaded_mcp_tools producer the heartbeat reports so the
    platform online/degraded gate can verify the management MCP's tools are
    actually live, not just declared."""

    def setup_method(self):
        set_loaded_mcp_tools(None)  # reset module-level holder per test

    def teardown_method(self):
        set_loaded_mcp_tools(None)

    def test_none_until_a_turn_runs(self):
        assert loaded_mcp_tools() is None
        p = identity_gate_payload()
        assert "mcp_server_present" in p  # always present
        assert "loaded_mcp_tools" not in p  # omitted pre-first-turn (fail-closed)

    def test_set_then_reported_and_in_payload(self):
        set_loaded_mcp_tools(
            ["mcp__molecule-platform__create_workspace", "Read"]
        )
        assert loaded_mcp_tools() == [
            "mcp__molecule-platform__create_workspace",
            "Read",
        ]
        assert identity_gate_payload()["loaded_mcp_tools"] == [
            "mcp__molecule-platform__create_workspace",
            "Read",
        ]

    def test_empty_list_is_a_meaningful_non_none_signal(self):
        # A turn ran but loaded no MCP tools — distinct from "no turn yet".
        set_loaded_mcp_tools([])
        assert loaded_mcp_tools() == []
        assert identity_gate_payload()["loaded_mcp_tools"] == []

    def test_none_clears(self):
        set_loaded_mcp_tools(["mcp__molecule-platform__create_workspace"])
        set_loaded_mcp_tools(None)
        assert loaded_mcp_tools() is None
        assert "loaded_mcp_tools" not in identity_gate_payload()

    def test_returns_a_copy(self):
        set_loaded_mcp_tools(["a"])
        got = loaded_mcp_tools()
        got.append("b")
        assert loaded_mcp_tools() == ["a"]  # internal state not mutable by caller

    def test_heartbeat_includes_loaded_tools_when_set(self):
        from molecule_runtime.heartbeat import HeartbeatLoop

        set_loaded_mcp_tools(["mcp__molecule-platform__create_workspace"])
        hb = HeartbeatLoop("http://platform", "ws-1")
        recorded = {}

        class _FakeResp:
            def json(self):
                return {}

        class _FakeClient:
            def post(self, url, *, json, headers):
                recorded["json"] = json
                return _FakeResp()

        hb._send_heartbeat(_FakeClient())
        assert recorded["json"]["loaded_mcp_tools"] == [
            "mcp__molecule-platform__create_workspace"
        ]
