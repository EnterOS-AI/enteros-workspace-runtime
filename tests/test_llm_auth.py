"""Unit tests for molecule_runtime.llm_auth.normalise_llm_env."""

from molecule_runtime.llm_auth import normalise_llm_env


def test_no_token_is_noop():
    env: dict[str, str] = {}
    r = normalise_llm_env(env)
    assert r.detected_kind == "none"
    assert env == {}
    assert r.renamed_to is None


def test_oauth_token_moved_to_oauth_env_var():
    env = {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-abc123",
        "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert r.renamed_to == "CLAUDE_CODE_OAUTH_TOKEN"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc123"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" in r.cleared_vars
    assert "ANTHROPIC_BASE_URL" in r.cleared_vars


def test_oauth_token_keeps_anthropic_base_url():
    # If base URL is actually Anthropic, keep it (no-op on that var).
    env = {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-abc",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert env.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com"
    assert "ANTHROPIC_BASE_URL" not in r.cleared_vars


def test_api_key_moved_to_anthropic_api_key():
    env = {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-api03-xyz789",
        "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "api_key"
    assert r.renamed_to == "ANTHROPIC_API_KEY"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-xyz789"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env


def test_proxy_token_left_alone():
    env = {
        "ANTHROPIC_AUTH_TOKEN": "sk-cp-minimax-token-foo",
        "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "proxy"
    assert r.renamed_to is None
    # Proxies need both vars unchanged
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cp-minimax-token-foo"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert r.warning is None


def test_proxy_token_without_base_url_warns():
    env = {"ANTHROPIC_AUTH_TOKEN": "sk-cp-something"}
    r = normalise_llm_env(env)
    assert r.detected_kind == "proxy"
    assert r.warning is not None
    assert "ANTHROPIC_BASE_URL" in r.warning


def test_unknown_prefix_leaves_env_and_warns():
    env = {"ANTHROPIC_AUTH_TOKEN": "garbage-prefix-xyz"}
    r = normalise_llm_env(env)
    assert r.detected_kind == "unknown"
    assert r.renamed_to is None
    assert env["ANTHROPIC_AUTH_TOKEN"] == "garbage-prefix-xyz"
    assert r.warning is not None
    assert "unrecognised prefix" in r.warning


def test_existing_oauth_env_takes_precedence():
    # Operator set CLAUDE_CODE_OAUTH_TOKEN deliberately; don't overwrite.
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-deliberate",
        "ANTHROPIC_AUTH_TOKEN": "sk-cp-stale-proxy-value",
        "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-deliberate"
    # Conflicting ANTHROPIC_AUTH_TOKEN cleared so SDK picks the right one
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env


def test_idempotent_second_call():
    env = {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-once"}
    normalise_llm_env(env)
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-once"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_summary_renders_without_error():
    env = {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-abc"}
    r = normalise_llm_env(env)
    line = r.summary()
    assert "oauth" in line
    assert "CLAUDE_CODE_OAUTH_TOKEN" in line


def test_uses_os_environ_by_default(monkeypatch):
    import os
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-real")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    r = normalise_llm_env()
    assert r.detected_kind == "oauth"
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-real"
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_strips_whitespace_and_newlines_from_token():
    env = {"ANTHROPIC_AUTH_TOKEN": "  sk-ant-oat01-abc\n"}
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"
    # Trailing newline must not survive into the renamed var
    assert "\n" not in env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert " " not in env["CLAUDE_CODE_OAUTH_TOKEN"]


def test_unknown_prefix_does_not_leak_token_to_warning():
    # Security: warning must not contain any bytes of the secret.
    sensitive = "ghs_supersecrettoken123"
    env = {"ANTHROPIC_AUTH_TOKEN": sensitive}
    r = normalise_llm_env(env)
    assert r.detected_kind == "unknown"
    assert r.warning is not None
    # No substring of the token — not even a prefix — is allowed in logs.
    for i in range(4, len(sensitive)):
        assert sensitive[:i] not in r.warning, (
            f"token prefix leaked to warning: {sensitive[:i]!r} found in "
            f"{r.warning!r}"
        )


def test_base_url_substring_false_positive_blocked():
    # A hostile URL that contains 'anthropic.com' as a substring but is not
    # actually Anthropic MUST still be cleared when switching to OAuth mode.
    env = {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x",
        "ANTHROPIC_BASE_URL": "https://proxy.anthropic.com.evil.example/",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_BASE_URL" in r.cleared_vars


def test_actual_anthropic_base_url_preserved():
    for url in (
        "https://api.anthropic.com",
        "https://api.anthropic.com/v1",
        "http://api.anthropic.com/",  # plain http unlikely but shouldn't crash
    ):
        env = {
            "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x",
            "ANTHROPIC_BASE_URL": url,
        }
        normalise_llm_env(env)
        assert env.get("ANTHROPIC_BASE_URL") == url, (
            f"native Anthropic URL {url!r} should be preserved, got "
            f"{env.get('ANTHROPIC_BASE_URL')!r}"
        )


def test_malformed_base_url_does_not_crash():
    # If the URL is garbled, the normaliser shouldn't crash — fall through
    # to clearing it, which is the safe choice for OAuth mode.
    env = {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x",
        "ANTHROPIC_BASE_URL": "not a url",
    }
    r = normalise_llm_env(env)
    assert r.detected_kind == "oauth"
    assert "ANTHROPIC_BASE_URL" not in env


# --- Provider-honoring guard (drain fix 2026-05-29) ---------------------------


def test_provider_minimax_drops_inherited_oauth():
    # Shared tenant global leaks CLAUDE_CODE_OAUTH_TOKEN into a minimax
    # workspace; it must be dropped so claude-code can't bill Anthropic.
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-tenant-shared"}
    r = normalise_llm_env(env, provider="minimax")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.cleared_vars
    assert r.detected_kind == "none"


def test_provider_anthropic_keeps_oauth():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-legit"}
    r = normalise_llm_env(env, provider="anthropic")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-legit"
    assert r.detected_kind == "oauth"


def test_provider_claude_code_alias_keeps_oauth():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-legit"}
    r = normalise_llm_env(env, provider="claude-code")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-legit"
    assert r.detected_kind == "oauth"


def test_provider_minimax_keeps_proxy_token_after_dropping_oauth():
    # minimax via claude-code proxy mode: the inherited oauth must go, but the
    # proxy token + base_url stay so the call actually reaches minimax.
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-shared",
        "ANTHROPIC_AUTH_TOKEN": "sk-cp-minimax-tok",
        "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    }
    r = normalise_llm_env(env, provider="minimax")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert r.detected_kind == "proxy"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cp-minimax-tok"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"


def test_empty_provider_preserves_legacy_oauth_behaviour():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    r = normalise_llm_env(env, provider="")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    assert r.detected_kind == "oauth"


def test_none_provider_preserves_legacy_oauth_behaviour():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    r = normalise_llm_env(env)  # no provider arg = legacy call sites
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    assert r.detected_kind == "oauth"


def test_provider_case_insensitive_drop():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    r = normalise_llm_env(env, provider="MiniMax")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_provider_openai_drops_oauth():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    r = normalise_llm_env(env, provider="openai")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert r.detected_kind == "none"


# --- CP-proxy foreign-OAuth drop (platform-agent concierge 401 fix) -----------

_CP_PROXY_BASE = "https://controlplane.example.com/api/v1/internal/llm/anthropic"
_ADMIN_TOKEN = "abc123def456ghi789jkl012"  # 24-hex per-workspace admin token


def test_cp_proxy_base_url_drops_inherited_oauth_even_with_empty_provider():
    # The platform-agent concierge bug: an inherited tenant OAuth token co-exists
    # with the CP proxy base URL + the per-workspace admin token. With an empty
    # provider (the rebuilt-from-DB payload), the old provider-only guard skipped
    # and the OAuth short-circuit hijacked the request → the OAuth bearer hit the
    # CP proxy → 401. The OAuth token MUST be dropped here, UN-GATED on provider.
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-inherited-tenant",
        "ANTHROPIC_AUTH_TOKEN": _ADMIN_TOKEN,
        "ANTHROPIC_BASE_URL": _CP_PROXY_BASE,
    }
    r = normalise_llm_env(env, provider="")  # empty provider — the failing case
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.cleared_vars
    # Proof the drop ran BEFORE the OAuth short-circuit: the admin token + proxy
    # URL survive (the short-circuit would have cleared both and gone native).
    assert env["ANTHROPIC_AUTH_TOKEN"] == _ADMIN_TOKEN
    assert env["ANTHROPIC_BASE_URL"] == _CP_PROXY_BASE
    assert r.detected_kind != "oauth"


def test_cp_proxy_base_url_drops_oauth_even_for_anthropic_provider():
    # Un-gated on provider too: even provider='anthropic' cannot keep an OAuth
    # token when the base URL is the CP proxy — OAuth can't authenticate there.
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-inherited",
        "ANTHROPIC_AUTH_TOKEN": _ADMIN_TOKEN,
        "ANTHROPIC_BASE_URL": _CP_PROXY_BASE,
    }
    normalise_llm_env(env, provider="anthropic")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == _ADMIN_TOKEN
    assert env["ANTHROPIC_BASE_URL"] == _CP_PROXY_BASE


def test_byok_oauth_direct_native_url_untouched():
    # BYOK-via-OAuth-direct: a workspace legitimately pointing at native Anthropic
    # with its own OAuth token is NOT the proxy case → OAuth kept, base preserved.
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-byok-legit",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    }
    r = normalise_llm_env(env, provider="")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-byok-legit"
    assert r.detected_kind == "oauth"
    assert env.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com"


def test_is_molecule_cp_proxy_base_url_matching():
    from molecule_runtime.llm_auth import _is_molecule_cp_proxy_base_url as m

    # Matches the CP proxy path prefix, host-agnostic (prod/staging/local).
    assert m("https://controlplane.example.com/api/v1/internal/llm/anthropic")
    assert m("https://cp.staging.moleculesai.app/api/v1/internal/llm/openai/v1")
    assert m("http://localhost:8080/api/v1/internal/llm/anthropic")
    # Prefix-anchored: a direct-provider proxy, native Anthropic, a deeper path,
    # a query-string match, a bare path (no host), and empty all fail.
    assert not m("https://api.minimax.io/anthropic")
    assert not m("https://api.anthropic.com")
    assert not m("https://evil.example/redirect/api/v1/internal/llm/x")
    assert not m("https://evil.example/?x=/api/v1/internal/llm/anthropic")
    assert not m("/api/v1/internal/llm/anthropic")
    assert not m("")
