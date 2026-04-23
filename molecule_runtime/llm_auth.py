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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NormalisationResult:
    """What normalise_llm_env did. Safe to print in boot logs."""

    detected_kind: str = "none"  # "oauth" | "api_key" | "proxy" | "unknown" | "none"
    renamed_to: Optional[str] = None
    cleared_vars: list[str] = field(default_factory=list)
    warning: Optional[str] = None

    def summary(self) -> str:
        if self.detected_kind == "none":
            return "llm-auth: no ANTHROPIC_AUTH_TOKEN set"
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


def normalise_llm_env(env: Optional[dict[str, str]] = None) -> NormalisationResult:
    """Inspect and rewrite LLM auth env vars in place.

    Parameters
    ----------
    env
        The env mapping to mutate. Defaults to ``os.environ``.
        Passing a dict is useful for tests.

    Returns
    -------
    NormalisationResult
        Describes what was detected and what was changed, for logging.
    """
    if env is None:
        env = os.environ

    result = NormalisationResult()

    # Priority: explicit CLAUDE_CODE_OAUTH_TOKEN wins if already present
    # (operator set it deliberately — don't override).
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
