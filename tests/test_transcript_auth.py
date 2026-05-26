"""Tests for molecule_runtime.transcript_auth (RCA #328)."""

from __future__ import annotations

from molecule_runtime import transcript_auth


class TestTranscriptAuthorized:
    def test_none_expected_token_fails_closed(self) -> None:
        """None expected_token → 401 (fail closed). This is the #328 fix.

        Previously missing token → skip auth entirely. Containers on same
        Docker network could read session logs during provisioning.
        """
        assert transcript_auth.transcript_authorized(None, "Bearer anything") is False
        assert transcript_auth.transcript_authorized("", "Bearer anything") is False

    def test_empty_auth_header_fails_when_token_set(self) -> None:
        """Non-empty expected + empty auth → fail closed."""
        assert transcript_auth.transcript_authorized("secret-token", "") is False

    def test_wrong_bearer_prefix_fails(self) -> None:
        """Wrong prefix (Basic vs Bearer) fails even with correct token."""
        assert transcript_auth.transcript_authorized("secret-token", "Basic secret-token") is False

    def test_wrong_token_fails(self) -> None:
        """Mismatched token fails."""
        assert transcript_auth.transcript_authorized("correct-token", "Bearer wrong-token") is False

    def test_correct_token_and_header_succeeds(self) -> None:
        """Strict equality: correct token + correct Bearer prefix → authorized."""
        assert transcript_auth.transcript_authorized("secret-token", "Bearer secret-token") is True

    def test_bearer_prefix_case_sensitive(self) -> None:
        """Bearer prefix is case-sensitive (matches platform wsauth contract)."""
        assert transcript_auth.transcript_authorized("token", "bearer token") is False
        assert transcript_auth.transcript_authorized("token", "BEARER token") is False