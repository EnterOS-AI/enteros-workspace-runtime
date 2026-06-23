import json
import os
from unittest.mock import patch

import pytest

from molecule_runtime.platform_agent_identity import (
    MANAGEMENT_MCP_NAME,
    MANAGEMENT_MCP_SPEC,
    MCPSERVER_PATH,
    PLATFORM_AGENT_IMAGE_ENV,
    ensure_management_mcp_in_settings,
    identity_gate_payload,
    loaded_mcp_tools,
    mcp_server_present,
    on_platform_agent_image,
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
        payload = identity_gate_payload()
        assert payload["mcp_server_present"] is True
        # loaded_mcp_tools omitted until a live turn reports a tool list.
        assert "loaded_mcp_tools" not in payload
        # cp#3164: the box-level diagnostic ships in the payload.
        assert set(payload["platform_mcp_diag"]) == {
            "on_platform_agent_image",
            "mcp_binary_present",
            "mcp_settings_entry",
            "mcp_command_resolved",
        }


@pytest.fixture
def patch_mcp_present(monkeypatch):
    # main + heartbeat now build the wire payload via identity_gate_payload(),
    # which calls mcp_server_present() from platform_agent_identity. Patching the
    # SOURCE function flows through both call sites — the per-module
    # `mcp_server_present` bindings were removed when main/heartbeat switched to
    # importing identity_gate_payload (RC 12977).
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.mcp_server_present",
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


class TestOnPlatformAgentImage:
    def test_true_when_marker_set(self, monkeypatch):
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        assert on_platform_agent_image() is True

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv(PLATFORM_AGENT_IMAGE_ENV, raising=False)
        assert on_platform_agent_image() is False

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "  "])
    def test_false_for_falsey_values(self, monkeypatch, val):
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, val)
        assert on_platform_agent_image() is False


class TestEnsureManagementMCPInSettings:
    """The protected-entry self-heal that keeps the management MCP from being
    evicted by a user-plugin install/restart (RCA #2970, concierge fail-closed).
    """

    def _point_settings_at(self, monkeypatch, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.SETTINGS_PATH", str(settings)
        )
        return settings

    def test_noop_on_ordinary_workspace_image(self, tmp_path, monkeypatch):
        # Ordinary workspaces must NEVER declare the org-admin MCP.
        monkeypatch.delenv(PLATFORM_AGENT_IMAGE_ENV, raising=False)
        settings = self._point_settings_at(monkeypatch, tmp_path)
        assert ensure_management_mcp_in_settings() is False
        assert not settings.exists()

    def test_seeds_protected_entry_when_settings_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        settings = self._point_settings_at(monkeypatch, tmp_path)
        assert ensure_management_mcp_in_settings() is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"][MANAGEMENT_MCP_NAME] == MANAGEMENT_MCP_SPEC
        # The gate now sees the management MCP.
        with patch.object(
            __import__("molecule_runtime.platform_agent_identity", fromlist=["x"]),
            "MCPSERVER_PATH",
            str(tmp_path / "no-binary"),
        ):
            with patch.object(
                __import__("molecule_runtime.platform_agent_identity", fromlist=["x"]),
                "SETTINGS_PATH",
                str(settings),
            ):
                assert mcp_server_present() is True

    def test_additive_preserves_user_plugin_entry(self, tmp_path, monkeypatch):
        # The exact bug: a user plugin (image-gen) is in settings.json after a
        # fresh-box rebuild; the self-heal must ADD molecule-platform WITHOUT
        # evicting the user plugin.
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        settings = self._point_settings_at(monkeypatch, tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(*)"]},
                    "mcpServers": {"image-gen": {"command": "npx", "args": ["x"]}},
                }
            )
        )
        assert ensure_management_mcp_in_settings() is True
        data = json.loads(settings.read_text())
        # BOTH present — additive, never evict.
        assert MANAGEMENT_MCP_NAME in data["mcpServers"]
        assert "image-gen" in data["mcpServers"]
        assert data["mcpServers"]["image-gen"] == {"command": "npx", "args": ["x"]}
        # Unrelated keys untouched.
        assert data["permissions"] == {"allow": ["Bash(*)"]}

    def test_idempotent_when_already_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        settings = self._point_settings_at(monkeypatch, tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: MANAGEMENT_MCP_SPEC}}))
        # Already identical → no rewrite.
        assert ensure_management_mcp_in_settings() is False

    def test_overwrites_drifted_management_entry(self, tmp_path, monkeypatch):
        # A stale/wrong management entry (e.g. an old npx spec from a prior
        # plugin merge) is re-asserted to the canonical baked-binary spec.
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        settings = self._point_settings_at(monkeypatch, tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "stale"}}})
        )
        assert ensure_management_mcp_in_settings() is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"][MANAGEMENT_MCP_NAME] == MANAGEMENT_MCP_SPEC

    def test_rebuilds_on_corrupt_settings(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        settings = self._point_settings_at(monkeypatch, tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{ this is not json")
        assert ensure_management_mcp_in_settings() is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"][MANAGEMENT_MCP_NAME] == MANAGEMENT_MCP_SPEC

    def test_user_plugin_install_simulation_keeps_management_mcp(self, tmp_path, monkeypatch):
        """End-to-end of the reported bug: simulate a fresh-box boot where only
        the PUBLIC user plugin re-merged (the PRIVATE management-MCP plugin
        fetch failed), then run the boot self-heal. The management MCP must be
        present afterward AND the user plugin must persist.
        """
        from molecule_runtime.mcp_render import render_claude_settings
        from pathlib import Path

        monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
        settings = self._point_settings_at(monkeypatch, tmp_path)

        # Simulate the runtime's per-plugin MCP wiring for the user plugin only
        # (the private molecule-platform-mcp plugin fetch failed at boot). The
        # user plugin's mcpServers now lands in settings.json via the MCP-wiring
        # PORT's claude renderer (render_claude_settings) — the same path the
        # default BaseAdapter.register_mcp_server_hook uses — rather than the old
        # _merge_settings_fragment mcpServers branch (which now skips mcpServers).
        render_claude_settings(Path(settings), "image-gen", {"command": "npx", "args": ["y"]})

        # Pre-self-heal: only the user plugin is present (the bug state).
        pre = json.loads(settings.read_text())
        assert MANAGEMENT_MCP_NAME not in pre["mcpServers"]
        assert "image-gen" in pre["mcpServers"]

        # Boot self-heal runs → protected entry restored, user plugin kept.
        assert ensure_management_mcp_in_settings() is True
        post = json.loads(settings.read_text())
        assert post["mcpServers"][MANAGEMENT_MCP_NAME] == MANAGEMENT_MCP_SPEC
        assert post["mcpServers"]["image-gen"] == {"command": "npx", "args": ["y"]}
