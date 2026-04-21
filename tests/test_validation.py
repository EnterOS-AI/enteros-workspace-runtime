"""Tests for builtin_tools/validation.py (WORKSPACE_ID validation, CWE-20/CWE-88)."""

from __future__ import annotations

import os
from unittest import mock

import pytest


class TestWorkspaceIdValidation:
    """Unit tests for _validate_workspace_id and get_validated_workspace_id."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Reset the module-level cache before each test."""
        import molecule_runtime.builtin_tools.validation as v

        v._cached_workspace_id = None
        v._cached_validated = False
        yield
        v._cached_workspace_id = None
        v._cached_validated = False

    # ── Valid IDs ────────────────────────────────────────────────────────────

    @pytest.mark.parametrize("ws_id", [
        "ws-abc-123",
        "molecule-dev-workspace-01",
        "ws_under_score",
        "WS.UPPER.DOT",
        "a" * 256,  # max length
        "ws-with-dashes-and_underscores.and.dots",
    ])
    def test_valid_ids_accepted(self, ws_id: str):
        from molecule_runtime.builtin_tools.validation import _validate_workspace_id

        # Must not raise
        _validate_workspace_id(ws_id)

    # ── Empty / missing ───────────────────────────────────────────────────────

    def test_empty_string_raises(self):
        from molecule_runtime.builtin_tools.validation import (
            WorkspaceIdValidationError,
            _validate_workspace_id,
        )

        with pytest.raises(WorkspaceIdValidationError) as exc_info:
            _validate_workspace_id("")
        assert "empty" in str(exc_info.value).lower()

    # ── Path-traversal chars ─────────────────────────────────────────────────

    @pytest.mark.parametrize("bad_id", [
        "ws/abc",          # forward slash
        "ws\\abc",         # backslash
        "ws/../etc",       # ..
        "ws/..",           # isolated ..
        "../../etc",       # leading ..
        "ws#fragment",     # hash
        "ws?query=x",      # question mark
        "ws&split=x",      # ampersand
        "ws\t\r\n",       # whitespace (not stripped by callers)
        "ws/workspace",    # mixed slash
    ])
    def test_invalid_chars_rejected(self, bad_id: str):
        from molecule_runtime.builtin_tools.validation import (
            WorkspaceIdValidationError,
            _validate_workspace_id,
        )

        with pytest.raises(WorkspaceIdValidationError) as exc_info:
            _validate_workspace_id(bad_id)
        assert "invalid format" in str(exc_info.value).lower()

    # ── get_validated_workspace_id caching ───────────────────────────────────

    def test_caches_result_on_success(self):
        from molecule_runtime.builtin_tools.validation import get_validated_workspace_id

        with mock.patch.dict(os.environ, {"WORKSPACE_ID": "ws-test-123"}):
            result1 = get_validated_workspace_id()
            result2 = get_validated_workspace_id()

        assert result1 == result2 == "ws-test-123"
        # Verify caching by checking the module-level flag directly
        import molecule_runtime.builtin_tools.validation as v

        assert v._cached_validated is True
        assert v._cached_workspace_id == "ws-test-123"

    def test_raises_after_first_failure(self):
        from molecule_runtime.builtin_tools.validation import (
            WorkspaceIdValidationError,
            get_validated_workspace_id,
        )

        with mock.patch.dict(os.environ, {"WORKSPACE_ID": ""}):
            with pytest.raises(WorkspaceIdValidationError):
                get_validated_workspace_id()

            # Second call on same empty ID must also raise (not cached as valid)
            with pytest.raises(WorkspaceIdValidationError):
                get_validated_workspace_id()

    def test_caller_context_in_error(self):
        from molecule_runtime.builtin_tools.validation import (
            WorkspaceIdValidationError,
            _validate_workspace_id,
        )

        with pytest.raises(WorkspaceIdValidationError) as exc_info:
            _validate_workspace_id("ws/../../../etc", caller="memory.commit_memory")
        assert "memory.commit_memory" in str(exc_info.value)

    # ── Error type ────────────────────────────────────────────────────────────

    def test_error_is_valueerror_subclass(self):
        from molecule_runtime.builtin_tools.validation import (
            WorkspaceIdValidationError,
            _validate_workspace_id,
        )

        with pytest.raises(WorkspaceIdValidationError):
            _validate_workspace_id("")

        assert issubclass(WorkspaceIdValidationError, ValueError)


class TestWorkspaceIdRegex:
    """Tests for the compiled regex pattern."""

    @pytest.mark.parametrize("valid_id", [
        "abc123",
        "my-workspace",
        "ws_001",
        "ws.example",
        "A1_B2-c3",
    ])
    def test_regex_accepts_valid(self, valid_id: str):
        from molecule_runtime.builtin_tools.validation import _WORKSPACE_ID_RE

        assert _WORKSPACE_ID_RE.match(valid_id)

    @pytest.mark.parametrize("invalid_id", [
        "",
        "/",
        "..",
        "..//..",
        "a/b",
        "a\\b",
        "a#b",
        "a?b",
        "a&b",
        "ws..example",   # embedded ..
        "ws../etc",       # leading ..
    ])
    def test_regex_rejects_invalid(self, invalid_id: str):
        from molecule_runtime.builtin_tools.validation import _WORKSPACE_ID_RE

        assert not _WORKSPACE_ID_RE.match(invalid_id)
