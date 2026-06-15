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

    def test_empty_workspace_config_path_uses_fallback(self, tmp_path, monkeypatch):
        """issue #118: an empty-but-set WORKSPACE_CONFIG_PATH must be treated as
        unset so the configs_dir fallback is used and the agent is not silently
        degraded to read-only."""
        import molecule_runtime.builtin_tools.audit as audit_mod
        from molecule_runtime.config import load_config

        # Write a config.yaml in the fallback directory with operator role.
        (tmp_path / "config.yaml").write_text("rbac:\n  roles:\n    - operator\n")

        monkeypatch.setenv("WORKSPACE_CONFIG_PATH", "")
        monkeypatch.setattr(
            "molecule_runtime.configs_dir.resolve", lambda: tmp_path
        )
        # Clear cache that may retain a previous config load.
        audit_mod._load_workspace_config.cache_clear()

        cfg = load_config()
        assert cfg is not None
        assert "operator" in cfg.rbac.roles

        roles, _custom = audit_mod.get_workspace_roles()
        assert roles == ["operator"], (
            f"empty WORKSPACE_CONFIG_PATH should fall back to configs_dir; got {roles}"
        )

        audit_mod._load_workspace_config.cache_clear()

    def test_whitespace_only_workspace_config_path_uses_fallback(self, tmp_path, monkeypatch):
        """issue #118 CR2 nit: a whitespace-only WORKSPACE_CONFIG_PATH must also
        be treated as unset so the configs_dir fallback is used."""
        import molecule_runtime.builtin_tools.audit as audit_mod
        from molecule_runtime.config import load_config

        (tmp_path / "config.yaml").write_text("rbac:\n  roles:\n    - operator\n")

        monkeypatch.setenv("WORKSPACE_CONFIG_PATH", "   ")
        monkeypatch.setattr(
            "molecule_runtime.configs_dir.resolve", lambda: tmp_path
        )
        audit_mod._load_workspace_config.cache_clear()

        cfg = load_config()
        assert cfg is not None
        assert "operator" in cfg.rbac.roles

        roles, _custom = audit_mod.get_workspace_roles()
        assert roles == ["operator"], (
            f"whitespace-only WORKSPACE_CONFIG_PATH should fall back to configs_dir; got {roles}"
        )

        audit_mod._load_workspace_config.cache_clear()

    def test_fail_secure_logs_error(self, caplog):
        """issue #118: when config loading fails, get_workspace_roles must log an
        ERROR so operators can diagnose silent read-only degradation."""
        import logging
        import molecule_runtime.builtin_tools.audit as audit_mod
        import molecule_runtime.config as config_mod

        audit_mod._load_workspace_config.cache_clear()
        with caplog.at_level(logging.ERROR, logger="molecule_runtime.builtin_tools.audit"):
            with mock.patch.object(config_mod, "load_config", side_effect=RuntimeError("boom")):
                roles, _custom = audit_mod.get_workspace_roles()

        assert roles == ["read-only"]
        assert "fail-securing to read-only" in caplog.text, (
            f"expected loud degradation log, got: {caplog.text}"
        )

        audit_mod._load_workspace_config.cache_clear()
