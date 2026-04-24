"""Tests for sanitize_agent_error() — specifically the stderr surface in A2A responses."""

from __future__ import annotations

import pytest

from molecule_runtime.executor_helpers import sanitize_agent_error


class TestSanitizeAgentError:
    """sanitize_agent_error() must surface stderr in A2A error messages."""

    def test_plain_error_no_stderr(self):
        """No stderr → simple message, no trailing colon."""
        class DummyError(Exception):
            pass

        result = sanitize_agent_error(exc=DummyError("boom"))
        assert result == "Agent error (DummyError)"

    def test_stderr_included_in_message(self):
        """stderr= kwarg is included verbatim so A2A callers can show it."""
        result = sanitize_agent_error(category="subprocess_error", stderr="You hit your rate limit · resets Apr 17")
        assert result == "Agent error (subprocess_error): You hit your rate limit · resets Apr 17"

    def test_stderr_truncated_by_caller(self):
        """Caller is responsible for capping; function does not truncate."""
        long_stderr = "x" * 5000
        result = sanitize_agent_error(category="subprocess_error", stderr=long_stderr)
        assert result.endswith(long_stderr)

    def test_category_wins_over_exc_type(self):
        """When both category and exc are passed, category is the tag."""
        result = sanitize_agent_error(exc=ValueError("oops"), category="rate_limited", stderr="rate limit")
        assert "Agent error (rate_limited)" in result
        assert "ValueError" not in result

    def test_exc_name_used_when_no_category_or_stderr(self):
        """Fallback to exc type name when category is absent."""
        class ToolNotFoundError(Exception):
            pass

        result = sanitize_agent_error(exc=ToolNotFoundError("some tool not found"))
        assert result == "Agent error (ToolNotFoundError)"

    def test_unknown_tag_when_no_exc_no_category(self):
        """Neither exc nor category → defaults to 'unknown'."""
        result = sanitize_agent_error()
        assert result == "Agent error (unknown)"
