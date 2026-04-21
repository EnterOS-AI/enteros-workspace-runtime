"""Regression tests for WORKSPACE_ID validation (issue #14, CWE-20)."""

import os
import re

import pytest


class TestValidateWorkspaceId:
    """validate_workspace_id() must reject injection characters."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self, monkeypatch):
        """Clear module-level caches and env before each test."""
        import molecule_runtime.platform_auth as pa_mod
        pa_mod._validated_workspace_id = None
        monkeypatch.delenv("WORKSPACE_ID", raising=False)

    def test_rejects_empty_string(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "")

        with pytest.raises(ValueError, match="empty"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_whitespace_only(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "   ")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_slash(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "ws/foo")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_double_dot(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "ws..foo")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_hash_fragment(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "ws#foo")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_question_mark(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "ws?foo")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_backslash(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "ws\\foo")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_newline(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "ws\nfoo")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_accepts_valid_uuid(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "53255246-1f34-432c-87e5-ae07f888f905")

        assert pa_mod.validate_workspace_id("53255246-1f34-432c-87e5-ae07f888f905") == "53255246-1f34-432c-87e5-ae07f888f905"

    def test_accepts_simple_alphanumeric(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "test-workspace")

        assert pa_mod.validate_workspace_id("test-workspace") == "test-workspace"

    def test_rejects_uppercase(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "Test-Workspace")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_rejects_starts_with_hyphen(self, monkeypatch):
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "-test-workspace")

        with pytest.raises(ValueError, match="invalid characters"):
            pa_mod.validate_workspace_id(os.environ["WORKSPACE_ID"])

    def test_cache_returns_same_result(self, monkeypatch):
        """Second call returns cached result without re-validation."""
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "my-workspace-123")

        first = pa_mod.validate_workspace_id("my-workspace-123")
        second = pa_mod.validate_workspace_id("my-workspace-123")
        assert first == second == "my-workspace-123"


class TestWorkspaceIdConstantInApproval:
    """approval.py imports WORKSPACE_ID from platform_auth."""

    def test_approval_imports_validated_id(self, monkeypatch):
        """approval.py uses platform_auth's validated WORKSPACE_ID constant."""
        import molecule_runtime.builtin_tools.approval as approval_mod
        from molecule_runtime.platform_auth import WORKSPACE_ID

        # Verify it's the same object
        assert approval_mod.WORKSPACE_ID is WORKSPACE_ID

        # Verify the value contains no injection chars
        assert "\n" not in approval_mod.WORKSPACE_ID
        assert "/" not in approval_mod.WORKSPACE_ID
        assert "\\" not in approval_mod.WORKSPACE_ID
        assert ".." not in approval_mod.WORKSPACE_ID


class TestWorkspaceIdConstantInDelegation:
    """delegation.py imports WORKSPACE_ID from platform_auth."""

    def test_delegation_imports_validated_id(self, monkeypatch):
        """delegation.py uses platform_auth's validated WORKSPACE_ID constant."""
        import molecule_runtime.builtin_tools.delegation as delegation_mod
        from molecule_runtime.platform_auth import WORKSPACE_ID

        assert delegation_mod.WORKSPACE_ID is WORKSPACE_ID
        assert "\n" not in delegation_mod.WORKSPACE_ID
        assert "/" not in delegation_mod.WORKSPACE_ID
        assert "\\" not in delegation_mod.WORKSPACE_ID
        assert ".." not in delegation_mod.WORKSPACE_ID
