"""Secret-scrubbing utilities for workspace runtime (#834 — C2).

Provides ``_redact_secrets()`` applied at every ``commit_memory`` call site
to prevent API keys and tokens from being persisted verbatim in the
memories table.

This module is now a thin wrapper around ``molecule_runtime.memory_redaction``,
which owns the comprehensive credential-pattern library used for both
runtime memory writes and hibernation snapshots (#2832).
"""

from __future__ import annotations

from molecule_runtime.memory_redaction import (
    CredentialRedactor,
    redact_credentials,
    redact_credentials_text,
)

#: Backwards-compatible plain replacement token. New code should prefer the
#: typed ``[REDACTED:<kind>]`` placeholders returned by ``redact_credentials``.
REDACTED: str = "[REDACTED]"


def _redact_secrets(content: str) -> str:
    """Scrub credential-shaped values from *content*.

    Returns
    -------
    str
        Copy of *content* with secrets replaced by ``[REDACTED:<kind>]``.
        If no secrets are found, the original string is returned unchanged.
        Calling this function on already-redacted content is safe.
    """
    return redact_credentials_text(content)


__all__ = [
    "CredentialRedactor",
    "REDACTED",
    "_redact_secrets",
    "redact_credentials",
    "redact_credentials_text",
]
