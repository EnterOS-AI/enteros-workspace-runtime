"""Regression tests for RBAC fail-secure fix (issue #11, CWE-285)."""

from unittest import mock


class TestGetWorkspaceRolesFailSecure:
    """get_workspace_roles() must return read-only when config is unavailable."""

    def test_returns_read_only_when_config_load_fails(self):
        """When config cannot be loaded, deny-by-default (read-only), not operator."""
        import molecule_runtime.builtin_tools.audit as audit_mod

        # Ensure cache is cold before patching
        audit_mod._load_workspace_config.cache_clear()

        with mock.patch.object(
            audit_mod, "_load_workspace_config", return_value=None
        ):
            roles, custom = audit_mod.get_workspace_roles()
            assert roles == ["read-only"], (
                f"Expected ['read-only'] when config unavailable, got {roles}. "
                "Fail-open grants operator (full delegate/approve/memory.write)! "
                "CWE-285: Improper Authorization."
            )
            assert custom == {}

        audit_mod._load_workspace_config.cache_clear()

    def test_returns_configured_roles_when_config_loads(self):
        """When config loads, configured roles are returned verbatim."""
        import molecule_runtime.builtin_tools.audit as audit_mod

        mock_cfg = mock.MagicMock()
        mock_cfg.rbac.roles = ["operator"]
        mock_cfg.rbac.allowed_actions = {}

        audit_mod._load_workspace_config.cache_clear()
        with mock.patch.object(audit_mod, "_load_workspace_config", return_value=mock_cfg):
            roles, custom = audit_mod.get_workspace_roles()
            assert roles == ["operator"]

        audit_mod._load_workspace_config.cache_clear()

    def test_read_only_role_denies_delegate(self):
        """The read-only fallback role must deny delegate, approve, memory.write."""
        import molecule_runtime.builtin_tools.audit as audit_mod

        assert not audit_mod.check_permission("delegate", ["read-only"]), (
            "read-only role should deny 'delegate'"
        )
        assert not audit_mod.check_permission("approve", ["read-only"]), (
            "read-only role should deny 'approve'"
        )
        assert not audit_mod.check_permission("memory.write", ["read-only"]), (
            "read-only role should deny 'memory.write'"
        )
        assert audit_mod.check_permission("memory.read", ["read-only"]), (
            "read-only role should allow 'memory.read'"
        )
