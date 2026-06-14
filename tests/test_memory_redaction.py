"""Tests for ``molecule_runtime.memory_redaction``.

Issue #2832 — credential redaction before auto-memory persistence.

Credential literals are intentionally built from string concatenation so
that static secret-scanning hooks don't mistake the test fixtures for
real leaked credentials.
"""

from __future__ import annotations

import pytest

from molecule_runtime.memory_redaction import (
    CredentialRedactor,
    redact_credentials,
    redact_credentials_text,
)

# Fake credential building blocks that satisfy the redactor patterns without
# looking like real leaked secrets to pre-commit scanners.
_GHP_STUB = "ghp_testredact_" + "a" * 25
_SK_STUB = "sk-test_" + "a" * 20
_AWS_STUB = "AKIA" + "EXAMPLETWOEXAMPL"


class TestPositiveMatches:
    """Credential-shaped input must be replaced."""

    def test_bearer_token(self):
        token = "Bearer " + "tokentokentokentokentoken"
        text = f"Authorization: {token}"
        out, meta = redact_credentials(text)
        assert "[REDACTED:bearer_token]" in out
        assert "tokentokentokentokentoken" not in out
        assert meta.get("bearer_token") == 1

    def test_env_token(self):
        text = f"GITHUB_TOKEN={_GHP_STUB}"
        out, meta = redact_credentials(text)
        assert out == "GITHUB_TOKEN=[REDACTED:env_credential]"
        assert meta.get("env_assignment") == 1

    def test_env_secret_quoted(self):
        text = 'DATABASE_PASSWORD="sup3r_s3cr3t_passw0rd_12345"'
        out, meta = redact_credentials(text)
        assert out == 'DATABASE_PASSWORD="[REDACTED:env_credential]"'
        assert meta.get("env_assignment") == 1

    def test_database_url(self):
        text = "DATABASE_URL=postgresql://app:s3cr3t-p@ss@db.example.com:5432/appdb"
        out, meta = redact_credentials(text)
        assert "s3cr3t-p@ss" not in out
        assert "[REDACTED:connection_password]" in out
        assert "postgresql://app:" in out
        assert "@db.example.com:5432/appdb" in out
        assert meta.get("connection_string") == 1

    def test_database_url_password_with_at_sign(self):
        # Regression: old regex stopped at the first '@' and left a fragment.
        text = "DATABASE_URL=postgresql://app:my@secret@db.example.com:5432/appdb"
        out, meta = redact_credentials(text)
        assert "my@secret" not in out
        assert "secret" not in out
        assert "[REDACTED:connection_password]" in out
        assert "postgresql://app:" in out
        assert "@db.example.com:5432/appdb" in out
        assert meta.get("connection_string") == 1

    def test_database_url_password_with_colon(self):
        text = "DATABASE_URL=postgresql://app:p:ssw0rd@db.example.com/appdb"
        out, meta = redact_credentials(text)
        assert "p:ssw0rd" not in out
        assert "ssw0rd" not in out
        assert "[REDACTED:connection_password]" in out
        assert "postgresql://app:" in out
        assert "@db.example.com/appdb" in out
        assert meta.get("connection_string") == 1

    def test_database_url_password_with_at_and_colon(self):
        text = "DATABASE_URL=postgresql://app:my@secret:value@db.example.com/appdb"
        out, meta = redact_credentials(text)
        assert "my@secret:value" not in out
        assert "my@secret" not in out
        assert "secret:value" not in out
        assert "value" not in out
        assert "[REDACTED:connection_password]" in out
        assert "postgresql://app:" in out
        assert "@db.example.com/appdb" in out
        assert meta.get("connection_string") == 1

    def test_private_key(self):
        text = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
            "QyNTUxOQAAACD----------------fake--------------------AAAAFHRlc3R0ZXN0dGVzdHRl\n"
            "c3QAAAAJ\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        out, meta = redact_credentials(text)
        assert "[REDACTED:private_key]" in out
        assert "b3BlbnNzaC1rZXk" not in out
        assert meta.get("private_key") == 1

    def test_jwt(self):
        text = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMe"
        out, meta = redact_credentials(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
        assert meta.get("jwt") == 1

    def test_aws_access_key(self):
        text = f"AWS_ACCESS_KEY_ID={_AWS_STUB}"
        out, meta = redact_credentials(text)
        assert "EXAMPLETWOEXAMPLE" not in out
        assert "[REDACTED:aws_access_key]" in out
        assert meta.get("aws_access_key") == 1

    def test_openai_key(self):
        text = _SK_STUB
        out, meta = redact_credentials(text)
        assert out == "[REDACTED:openai_key]"
        assert meta.get("openai_key") == 1

    def test_github_pat(self):
        text = _GHP_STUB
        out, meta = redact_credentials(text)
        assert out == "[REDACTED:github_pat]"
        assert meta.get("github_pat") == 1

    def test_multiple_secrets(self):
        text = (
            f"GITHUB_TOKEN={_GHP_STUB}\n"
            f"OPENAI_API_KEY={_SK_STUB}\n"
            "DATABASE_URL=postgresql://app:secret123@db.example.com/appdb"
        )
        out, meta = redact_credentials(text)
        assert meta.get("env_assignment") == 2
        assert meta.get("connection_string") == 1
        assert "ghp_" not in out
        assert "sk-" not in out
        assert "secret123" not in out


class TestNegativeMatches:
    """Non-secret text must be left untouched."""

    def test_ordinary_prose(self):
        text = "The quick brown fox jumps over the lazy dog. Key to success is practice."
        out, meta = redact_credentials(text)
        assert out == text
        assert not meta

    def test_short_env_value(self):
        text = "MY_TOKEN=abc123"
        out, meta = redact_credentials(text)
        assert out == text
        assert not meta

    def test_word_token_in_prose(self):
        text = "I need a token for the subway. My password hint is 'pets name'."
        out, meta = redact_credentials(text)
        assert out == text
        assert not meta

    def test_already_redacted(self):
        text = "GITHUB_TOKEN=[REDACTED:env_credential] and DATABASE_URL=postgresql://app:[REDACTED:connection_password]@db"
        out, meta = redact_credentials(text)
        assert out == text
        assert not meta

    def test_common_code_identifiers(self):
        text = "function getApiKey() { return config.key; }"
        out, meta = redact_credentials(text)
        assert out == text
        assert not meta


class TestRedactorCustomization:
    def test_custom_placeholder(self):
        redactor = CredentialRedactor(placeholder="<SCRUBBED:{kind}>")
        out = redactor.redact_text(_GHP_STUB)
        assert out == "<SCRUBBED:github_pat>"

    def test_invalid_placeholder_raises(self):
        with pytest.raises(ValueError):
            CredentialRedactor(placeholder="[SCRUBBED]")


class TestIdempotency:
    def test_redacting_twice(self):
        text = f"GITHUB_TOKEN={_GHP_STUB}"
        once = redact_credentials_text(text)
        twice = redact_credentials_text(once)
        assert once == twice
        assert "ghp_" not in once
