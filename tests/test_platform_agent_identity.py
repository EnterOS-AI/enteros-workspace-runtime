import os
from unittest.mock import patch

import pytest

from molecule_runtime.platform_agent_identity import (
    MCPSERVER_PATH,
    identity_gate_payload,
    mcp_server_present,
)


class TestMCPServerPresent:
    def test_true_when_binary_exists(self, tmp_path, monkeypatch):
        fake = tmp_path / "molecule-mcp-server"
        fake.write_text("#!/bin/sh\necho ok")
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH", str(fake)
        )
        assert mcp_server_present() is True

    def test_false_when_binary_missing(self, tmp_path, monkeypatch):
        missing = tmp_path / "not-there"
        monkeypatch.setattr(
            "molecule_runtime.platform_agent_identity.MCPSERVER_PATH", str(missing)
        )
        assert mcp_server_present() is False

    def test_default_path_is_opt_molecule_mcp_server(self):
        assert MCPSERVER_PATH == "/opt/molecule-mcp-server"

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
