"""Credential redaction for auto-memory persistence.

Issue #2832 (SECURITY). Before any workspace memory or snapshot is persisted,
its content is run through ``CredentialRedactor`` so that pasted or captured
credentials never reach the database in reversible form.

Supported credential shapes
---------------------------
- Private keys (PEM blocks, any variant).
- Connection strings / ``DATABASE_URL`` values that embed a password.
- Env-style assignments like ``*_TOKEN=...``, ``*_KEY=...``, ``*_SECRET=...``.
- HTTP ``Bearer`` tokens.
- JWT-shaped strings.
- AWS access key IDs (``AKIA...``).
- Well-known provider tokens: GitHub (``ghp_`` / ``ghs_`` / ``github_pat_``),
  OpenAI/Anthropic (``sk-...``), Cloudflare (``cfut_``), Molecule partner keys
  (``mol_pk_``), context7 (``ctx7_``).
- Long high-entropy base64 blobs as a catch-all (40+ chars).

Replacement tokens are non-reversible (``[REDACTED:<kind>]``) and the redactor
is idempotent — re-running it on already-redacted content is a no-op.
"""

from __future__ import annotations

import re
from typing import Callable


DEFAULT_PLACEHOLDER = "[REDACTED:{kind}]"
_MIN_SECRET_LEN = 8


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Private key PEM blocks (any common variant).
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"
    r".*?"
    r"-----END (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----",
    re.DOTALL,
)

# Connection strings / DATABASE_URL-style values with embedded password.
_CONNECTION_STRING_RE = re.compile(
    r"(?i)"
    r"((?:postgresql|postgres|mysql|mongodb\+srv|mongodb|redis|"
    r"http|https|ftp|sftp|amqp|sqlite|mssql|oracle)://)"
    r"([^:\s]+)"
    r":"
    r"([^@\s]+)"
    r"(@[^\s]+)"
)

# Env-style credential assignments.
_ENV_ASSIGNMENT_RE = re.compile(
    r"(?i)"
    r"\b([A-Z][A-Z0-9_]*_(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS))\s*=\s*"
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s\'"";]+)'
)

# HTTP Bearer tokens.
_BEARER_TOKEN_RE = re.compile(
    r"(?i)(Bearer\s+)([A-Za-z0-9_\-\.]{20,})"
)

# JWT-shaped strings.
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*"
)

# AWS access key ID.
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# GitHub tokens.
_GITHUB_PAT_RE = re.compile(r"\bghp_[A-Za-z0-9_]{36,}\b")
_GITHUB_SERVER_TOKEN_RE = re.compile(r"\bghs_[A-Za-z0-9_]{36,}\b")
_GITHUB_PAT_V2_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}\b")

# OpenAI / Anthropic style keys.
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")

# Cloudflare API token.
_CF_TOKEN_RE = re.compile(r"\bcfut_[A-Za-z0-9]{32,}\b")

# Molecule partner API key.
_MOL_PK_RE = re.compile(r"\bmol_pk_[A-Za-z0-9]{20,}\b")

# context7 token.
_CTX7_TOKEN_RE = re.compile(r"\bctx7_[A-Za-z0-9]+\b")

# High-entropy base64 catch-all.
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

def _counted_sub(
    pattern: re.Pattern[str],
    repl: Callable[[re.Match[str]], str],
    text: str,
) -> tuple[str, int]:
    """``re.sub`` that also returns the number of substitutions made."""
    count = 0

    def counted_repl(m: re.Match[str]) -> str:
        nonlocal count
        replacement = repl(m)
        if replacement == m.group(0):
            return replacement
        count += 1
        return replacement

    return pattern.sub(counted_repl, text), count


def _placeholder_for(kind: str, placeholder: str = DEFAULT_PLACEHOLDER) -> str:
    return placeholder.format(kind=kind)


def _bearer_token_repl(m: re.Match[str]) -> str:
    # Preserve the "Bearer " prefix so the redacted line remains readable.
    return f"{m.group(1)}{_placeholder_for('bearer_token')}"


def _connection_string_repl(m: re.Match[str]) -> str:
    # Preserve scheme://user:@host so debugging knows *which* service, but
    # drop the password. Skip already-redacted or trivial passwords.
    password = m.group(3)
    if _too_short_or_placeholder(password):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}:{_placeholder_for('connection_password')}{m.group(4)}"


def _too_short_or_placeholder(value: str) -> bool:
    """Avoid redacting short/innocuous values or already-redacted tokens."""
    if len(value) < _MIN_SECRET_LEN:
        return True
    if value.startswith("[REDACTED"):
        return True
    return False


def _env_assignment_repl(m: re.Match[str]) -> str:
    key = m.group(1)
    raw_value = m.group(2)
    # Preserve quoting so downstream parsers don't get confused.
    if raw_value.startswith('"') and raw_value.endswith('"') and len(raw_value) >= 2:
        inner = raw_value[1:-1]
        if _too_short_or_placeholder(inner):
            return m.group(0)
        return f'{key}="{_placeholder_for("env_credential")}"'
    if raw_value.startswith("'") and raw_value.endswith("'") and len(raw_value) >= 2:
        inner = raw_value[1:-1]
        if _too_short_or_placeholder(inner):
            return m.group(0)
        return f"{key}='{_placeholder_for('env_credential')}'"
    if _too_short_or_placeholder(raw_value):
        return m.group(0)
    return f"{key}={_placeholder_for('env_credential')}"


class CredentialRedactor:
    """Stateful redactor that can scrub credential-shaped values from text.

    Parameters
    ----------
    placeholder:
        Format string used for replacements. Must contain ``{kind}``.
    """

    def __init__(self, placeholder: str = DEFAULT_PLACEHOLDER) -> None:
        if "{kind}" not in placeholder:
            raise ValueError("placeholder must contain '{kind}'")
        self.placeholder = placeholder
        self._patterns: list[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str] | None]] = [
            # Most specific / structured first.
            ("private_key", _PRIVATE_KEY_RE, None),
            ("connection_string", _CONNECTION_STRING_RE, _connection_string_repl),
            ("env_assignment", _ENV_ASSIGNMENT_RE, _env_assignment_repl),
            ("bearer_token", _BEARER_TOKEN_RE, _bearer_token_repl),
            ("jwt", _JWT_RE, None),
            ("aws_access_key", _AWS_ACCESS_KEY_RE, None),
            ("github_pat", _GITHUB_PAT_RE, None),
            ("github_server_token", _GITHUB_SERVER_TOKEN_RE, None),
            ("github_pat_v2", _GITHUB_PAT_V2_RE, None),
            ("openai_key", _OPENAI_KEY_RE, None),
            ("cloudflare_token", _CF_TOKEN_RE, None),
            ("molecule_partner_key", _MOL_PK_RE, None),
            ("context7_token", _CTX7_TOKEN_RE, None),
            # Catch-all: very long base64-looking blobs.
            ("base64_blob", _BASE64_BLOB_RE, None),
        ]

    def redact(self, content: str) -> tuple[str, dict[str, int]]:
        """Return ``(redacted_content, metadata)``.

        ``metadata`` maps each matched credential kind to the number of
        occurrences removed.
        """
        metadata: dict[str, int] = {}
        result = content
        for kind, pattern, repl in self._patterns:
            if repl is None:
                token = self._placeholder(kind)

                def default_repl(m: re.Match[str], token: str = token) -> str:  # noqa: ANN001
                    return token

                current_repl: Callable[[re.Match[str]], str] = default_repl
            else:
                current_repl = repl
            new_result, count = _counted_sub(pattern, current_repl, result)
            if count:
                metadata[kind] = metadata.get(kind, 0) + count
                result = new_result
        return result, metadata

    def redact_text(self, content: str) -> str:
        """Convenience wrapper returning only the redacted string."""
        return self.redact(content)[0]

    def _placeholder(self, kind: str) -> str:
        return self.placeholder.format(kind=kind)


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

_default_redactor = CredentialRedactor()


def redact_credentials(content: str) -> tuple[str, dict[str, int]]:
    """Redact credential-shaped values from *content*.

    Returns ``(redacted_text, metadata)`` where ``metadata`` counts the
    number of redactions per credential kind.
    """
    return _default_redactor.redact(content)


def redact_credentials_text(content: str) -> str:
    """Return only the redacted text (no metadata)."""
    return _default_redactor.redact_text(content)
