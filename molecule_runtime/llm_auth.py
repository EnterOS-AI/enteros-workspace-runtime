"""LLM auth-env normalisation.

Platform stores per-workspace LLM credentials under a single key,
``ANTHROPIC_AUTH_TOKEN``. But the CLI/SDK tools we invoke downstream
expect *different* env var names depending on the token type:

    Token prefix          Correct env var             Base URL needed
    ------------------    ------------------------    ----------------
    sk-ant-oat01-*        CLAUDE_CODE_OAUTH_TOKEN     none (Claude handles)
    sk-ant-api03-*        ANTHROPIC_API_KEY           none (Claude default)
    sk-cp-*               ANTHROPIC_AUTH_TOKEN        proxy URL (MiniMax etc.)
    other/unknown         (leave as-is)               (leave as-is)

Without this normalisation, passing an OAuth token as
``ANTHROPIC_AUTH_TOKEN`` causes the Claude SDK to send it as a bearer
token to ``api.anthropic.com``, which responds:

    401 {"error":{"type":"authentication_error",
         "message":"OAuth authentication is currently not supported."}}

Call :func:`normalise_llm_env` once, early in the runtime bootstrap
(before any adapter/executor is created). The function mutates
``os.environ`` in place and returns a report of what changed so the
boot log shows the mapping.

Safe to call multiple times — idempotent.

Provider is SSOT (internal#718). The platform injects the tenant's shared
global secrets into *every* workspace, so a non-Anthropic workspace
(``provider=minimax``/``openai``/``moonshot``…) inherits a stray
``CLAUDE_CODE_OAUTH_TOKEN`` that belongs to the tenant's Claude agents.
claude-code auto-prefers that OAuth token and silently bills Anthropic
instead of the configured provider — the 2026-05-28 drain. Pass the
resolved ``provider`` to :func:`normalise_llm_env`: when it is not an
Anthropic-OAuth provider, the OAuth token is dropped here so downstream
auth follows the configured provider (and preflight fails *clearly* if
that provider's own key is absent — never a silent Anthropic fallback).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


# Providers that legitimately authenticate via CLAUDE_CODE_OAUTH_TOKEN (the
# Claude Code subscription OAuth path). For every other provider the OAuth
# token is a foreign credential inherited from shared tenant globals and MUST
# NOT be used — keeping it is the silent-Anthropic-drain bug. Compared
# case-insensitively; the empty string means "provider unknown" and is treated
# as "do not touch" for backward compatibility with pre-provider templates.
_ANTHROPIC_OAUTH_PROVIDERS = frozenset({
    "anthropic",
    "anthropic-oauth",
    "claude",
    "claude-code",
})


@dataclass
class NormalisationResult:
    """What normalise_llm_env did. Safe to print in boot logs."""

    detected_kind: str = "none"  # "oauth" | "api_key" | "proxy" | "unknown" | "none"
    renamed_to: Optional[str] = None
    cleared_vars: list[str] = field(default_factory=list)
    warning: Optional[str] = None

    def summary(self) -> str:
        if self.detected_kind == "none":
            base = "llm-auth: no ANTHROPIC_AUTH_TOKEN set"
            if self.cleared_vars:
                base += f" (cleared: {', '.join(self.cleared_vars)})"
            if self.warning:
                base += f" [WARN: {self.warning}]"
            return base
        line = f"llm-auth: detected {self.detected_kind}"
        if self.renamed_to:
            line += f" → exported as {self.renamed_to}"
        if self.cleared_vars:
            line += f" (cleared: {', '.join(self.cleared_vars)})"
        if self.warning:
            line += f" [WARN: {self.warning}]"
        return line


# Anthropic's native API hostnames. Treat as "direct Anthropic" for OAuth /
# API-key mode. Anything else in ANTHROPIC_BASE_URL is assumed to be a proxy
# and gets cleared when we switch to direct-Anthropic auth.
_ANTHROPIC_NATIVE_HOSTS = frozenset({
    "api.anthropic.com",
    "anthropic.com",
})


def _is_native_anthropic_base_url(base_url: str) -> bool:
    """Return True only if the base URL points at an Anthropic-native host.

    Substring matching on ``"anthropic.com"`` would falsely accept
    ``https://my-proxy.anthropic.com.evil.example/`` — parse the URL
    properly and compare the exact hostname.
    """
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").lower().strip()
    except Exception:
        return False
    return host in _ANTHROPIC_NATIVE_HOSTS


def _prefix_of(token: str) -> str:
    """Classify a token string by its well-known prefix."""
    if token.startswith("sk-ant-oat01-"):
        return "oauth"
    if token.startswith("sk-ant-api03-"):
        return "api_key"
    if token.startswith("sk-cp-"):
        return "proxy"
    return "unknown"


def normalise_llm_env(
    env: Optional[dict[str, str]] = None,
    provider: Optional[str] = None,
) -> NormalisationResult:
    """Inspect and rewrite LLM auth env vars in place.

    Parameters
    ----------
    env
        The env mapping to mutate. Defaults to ``os.environ``.
        Passing a dict is useful for tests.
    provider
        The workspace's resolved LLM provider slug (SSOT — ``config.provider``).
        When set to a non-Anthropic provider, any inherited
        ``CLAUDE_CODE_OAUTH_TOKEN`` is dropped so the runtime authenticates
        with the *configured* provider rather than silently falling back to
        the tenant's Claude OAuth (the 2026-05-28 Anthropic drain). ``None``
        or empty preserves the legacy behaviour (no provider-scoped clearing).

    Returns
    -------
    NormalisationResult
        Describes what was detected and what was changed, for logging.
    """
    if env is None:
        env = os.environ

    result = NormalisationResult()

    # Provider-honoring guard (drain fix, 2026-05-29). Provider is SSOT: a
    # non-Anthropic workspace must never authenticate via an inherited
    # CLAUDE_CODE_OAUTH_TOKEN from shared tenant globals. Drop it BEFORE any
    # detection so the OAuth short-circuit below can't hijack a minimax /
    # openai / moonshot workspace into billing Anthropic. If the configured
    # provider's own key is missing, preflight fails loudly — no silent
    # fallback, no drain.
    prov = (provider or "").strip().lower()
    if prov and prov not in _ANTHROPIC_OAUTH_PROVIDERS:
        if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            result.cleared_vars.append("CLAUDE_CODE_OAUTH_TOKEN")
            result.warning = (
                f"dropped inherited CLAUDE_CODE_OAUTH_TOKEN for "
                f"provider='{prov}' (provider is SSOT; Anthropic OAuth is "
                f"not used by this provider — prevents silent Anthropic drain)"
            )

    # Priority: explicit CLAUDE_CODE_OAUTH_TOKEN wins if already present
    # (operator set it deliberately — don't override). NB: a non-Anthropic
    # provider already had this dropped by the guard above.
    existing_oauth = env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if existing_oauth:
        result.detected_kind = "oauth"
        result.renamed_to = None
        # If ANTHROPIC_AUTH_TOKEN is ALSO set with a conflicting value,
        # clear it so the SDK doesn't pick the wrong one.
        auth = env.get("ANTHROPIC_AUTH_TOKEN", "")
        if auth and auth != existing_oauth:
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            result.cleared_vars.append("ANTHROPIC_AUTH_TOKEN")
        # Base URL is irrelevant for OAuth mode; remove the proxy URL
        # so the SDK uses Claude defaults.
        base = env.get("ANTHROPIC_BASE_URL", "")
        if base and not _is_native_anthropic_base_url(base):
            env.pop("ANTHROPIC_BASE_URL", None)
            result.cleared_vars.append("ANTHROPIC_BASE_URL")
        return result

    # No explicit CLAUDE_CODE_OAUTH_TOKEN — detect from ANTHROPIC_AUTH_TOKEN.
    # Strip whitespace because operators frequently paste tokens with
    # trailing newlines from terminals, and the SDK will reject those as
    # malformed before auth is even attempted.
    raw_tok = env.get("ANTHROPIC_AUTH_TOKEN", "")
    tok = raw_tok.strip()
    if not tok:
        return result
    if tok != raw_tok:
        env["ANTHROPIC_AUTH_TOKEN"] = tok  # persist the cleaned value

    kind = _prefix_of(tok)
    result.detected_kind = kind

    if kind == "oauth":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        result.cleared_vars.append("ANTHROPIC_AUTH_TOKEN")
        result.renamed_to = "CLAUDE_CODE_OAUTH_TOKEN"
        # Proxy base URL must go — OAuth flow uses Anthropic's own endpoint
        base = env.get("ANTHROPIC_BASE_URL", "")
        if base and not _is_native_anthropic_base_url(base):
            env.pop("ANTHROPIC_BASE_URL", None)
            result.cleared_vars.append("ANTHROPIC_BASE_URL")

    elif kind == "api_key":
        # Anthropic API keys can ride ANTHROPIC_API_KEY (strongly preferred by
        # claude-code) OR ANTHROPIC_AUTH_TOKEN. Moving it to ANTHROPIC_API_KEY
        # is the safer default because claude-code in non-bare mode reads
        # ANTHROPIC_API_KEY first.
        env["ANTHROPIC_API_KEY"] = tok
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        result.cleared_vars.append("ANTHROPIC_AUTH_TOKEN")
        result.renamed_to = "ANTHROPIC_API_KEY"
        # Clear proxy base URL for direct Anthropic calls
        base = env.get("ANTHROPIC_BASE_URL", "")
        if base and not _is_native_anthropic_base_url(base):
            env.pop("ANTHROPIC_BASE_URL", None)
            result.cleared_vars.append("ANTHROPIC_BASE_URL")

    elif kind == "proxy":
        # sk-cp-* = Claude proxy token (MiniMax, custom gateways). KEEP
        # ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL as-is; that's the correct
        # shape for proxies.
        result.renamed_to = None
        base = env.get("ANTHROPIC_BASE_URL", "")
        if not base:
            result.warning = (
                "proxy token detected but ANTHROPIC_BASE_URL is empty — "
                "proxy calls will fail without a base URL"
            )

    else:
        # unknown — be conservative, leave env untouched but warn.
        # Do NOT include the token value in the warning. Even a prefix
        # leaks bytes of a secret into logs (which get shipped to
        # Langfuse / CloudWatch / sentry / slack-firehose).
        result.warning = (
            "ANTHROPIC_AUTH_TOKEN has an unrecognised prefix; not "
            "normalising. Known prefixes: sk-ant-oat01-* (OAuth), "
            "sk-ant-api03-* (API key), sk-cp-* (proxy)."
        )

    return result
