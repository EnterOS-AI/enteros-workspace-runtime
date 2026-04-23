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
