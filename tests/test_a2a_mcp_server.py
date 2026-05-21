"""Regression tests for A2A MCP server RBAC gate (issue #12 / task #343, CWE-862).

Originally landed in standalone repo (#12 c72fbfc area) and skipped during the
standalone-as-SSOT migration because the monorepo-base import lacked the gate.
Re-restored by task #343 atop the migration: ``_tool_permission_check`` is back
in ``a2a_mcp_server.py`` and wired into ``handle_tool_call``.
"""

import pytest
from unittest import mock


class TestMcpServerRbacGate:
    """The A2A MCP server must enforce RBAC on sensitive tools."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self, monkeypatch, tmp_path):
        """Keep audit config cache cold so each test starts clean."""
        monkeypatch.setenv("WORKSPACE_CONFIG_PATH", str(tmp_path / "missing-config"))
        import molecule_runtime.builtin_tools.audit as audit_mod
        audit_mod._load_workspace_config.cache_clear()
        yield
        audit_mod._load_workspace_config.cache_clear()

    def test_unmapped_tools_allowed_without_config(self):
        """Tools with no RBAC gate (list_peers, recall_memory) always pass."""
        import molecule_runtime.a2a_mcp_server as mcp_mod

        # No config available → roles=["read-only"], but these tools have no gate
        rbac_error = mcp_mod._tool_permission_check("list_peers", {})
        assert rbac_error is None, "list_peers should not be gated"

        rbac_error = mcp_mod._tool_permission_check("get_workspace_info", {})
        assert rbac_error is None, "get_workspace_info should not be gated"

        rbac_error = mcp_mod._tool_permission_check("recall_memory", {"query": ""})
        assert rbac_error is None, "recall_memory should not be gated"

    def test_delegate_task_denied_for_read_only(self):
        """delegate_task must be blocked for read-only role (CWE-862)."""
        import molecule_runtime.a2a_mcp_server as mcp_mod

        rbac_error = mcp_mod._tool_permission_check("delegate_task", {
            "workspace_id": "some-workspace",
            "task": "do work",
        })
        assert rbac_error is not None, "read-only should be denied delegate_task"
        assert "delegate" in rbac_error or "RBAC" in rbac_error

    def test_commit_memory_denied_for_read_only(self):
        """commit_memory must be blocked for read-only role (CWE-862)."""
        import molecule_runtime.a2a_mcp_server as mcp_mod

        rbac_error = mcp_mod._tool_permission_check("commit_memory", {
            "content": "test",
            "scope": "LOCAL",
        })
        assert rbac_error is not None, "read-only should be denied commit_memory"
        assert "memory.write" in rbac_error or "RBAC" in rbac_error

    def test_send_message_to_user_denied_for_read_only(self):
        """send_message_to_user must be blocked for read-only role (CWE-862)."""
        import molecule_runtime.a2a_mcp_server as mcp_mod

        rbac_error = mcp_mod._tool_permission_check("send_message_to_user", {
            "message": "hello",
        })
        assert rbac_error is not None, "read-only should be denied send_message_to_user"
        assert "approve" in rbac_error or "RBAC" in rbac_error

    def test_delegate_task_allowed_for_operator(self):
        """delegate_task passes when roles include operator (config present)."""
        import molecule_runtime.a2a_mcp_server as mcp_mod

        mock_cfg = mock.MagicMock()
        mock_cfg.rbac.roles = ["operator"]
        mock_cfg.rbac.allowed_actions = {}

        import molecule_runtime.builtin_tools.audit as audit_mod
        with mock.patch.object(audit_mod, "_load_workspace_config", return_value=mock_cfg):
            rbac_error = mcp_mod._tool_permission_check("delegate_task", {
                "workspace_id": "target",
                "task": "work",
            })
        assert rbac_error is None, f"operator should be allowed delegate_task: {rbac_error}"
