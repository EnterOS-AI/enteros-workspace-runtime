"""Tests for npm_auth.install_npm_gitea_auth — gitea npm-registry auth (RCA 2026-06-24).

The concierge MCP (`npx @molecule-ai/mcp-server`) needs the gitea registry +
_authToken in ~/.npmrc or it ETARGETs the private package and never starts.
These lock in: writes the right lines, SSOT token precedence (canonical vars
beat the gitea HTTPS-auth pair), the verified live concierge shape (PAT in
GIT_HTTP_USERNAME when GIT_HTTP_PASSWORD is the x-oauth-basic sentinel), the
normal basic-auth shape (token in GIT_HTTP_PASSWORD), never writing the literal
x-oauth-basic as the secret, no-op without a token, idempotent/additive, correct
key derivation, and no token in logs.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from molecule_runtime.npm_auth import (
    _auth_key,
    install_npm_gitea_auth,
    resolve_npm_registry,
)

_ALL_TOKEN_VARS = ("MOLECULE_TEMPLATE_REPO_TOKEN", "GITEA_TOKEN", "GIT_HTTP_USERNAME",
                   "GIT_HTTP_PASSWORD", "MOLECULE_GITEA_NPM_REGISTRY",
                   "MOLECULE_GITEA_BASE_URL", "MOLECULE_PLUGIN_REGISTRY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Isolate HOME + clear all token/registry env so tests are deterministic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for v in _ALL_TOKEN_VARS:
        monkeypatch.delenv(v, raising=False)
    yield


def _npmrc(tmp_path: Path) -> Path:
    return tmp_path / ".npmrc"


def test_writes_registry_and_authtoken(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-AAA")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/" in content
    assert "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=tok-AAA" in content


def test_no_token_is_noop(tmp_path):
    install_npm_gitea_auth()
    assert not _npmrc(tmp_path).exists()


def test_npmrc_is_chmod_0600(monkeypatch, tmp_path):
    # The token is at rest in ~/.npmrc — it must be 0600 (created restricted,
    # no world-readable window). Guards the chmod/hardening regression.
    import stat
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-AAA")
    install_npm_gitea_auth()
    mode = stat.S_IMODE(_npmrc(tmp_path).stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_token_precedence_prefers_canonical(monkeypatch, tmp_path):
    # MOLECULE_TEMPLATE_REPO_TOKEN wins over GITEA_TOKEN, and both canonical vars
    # take precedence over the gitea HTTPS-auth pair (GIT_HTTP_USERNAME/PASSWORD).
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-GHU")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "tok-GHP")
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-CANON")
    install_npm_gitea_auth()
    assert "_authToken=tok-CANON" in _npmrc(tmp_path).read_text()


def test_gitea_token_precedence_over_git_http(monkeypatch, tmp_path):
    # GITEA_TOKEN (canonical alias) still beats the gitea HTTPS-auth pair.
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-GHU")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    install_npm_gitea_auth()
    assert "_authToken=tok-GITEA" in _npmrc(tmp_path).read_text()


def test_git_http_x_oauth_basic_uses_username_pat(monkeypatch, tmp_path):
    # VERIFIED live concierge shape (workspace-server conciergePlatformMCPEnv):
    # GIT_HTTP_USERNAME=<PAT>, GIT_HTTP_PASSWORD="x-oauth-basic" (the sentinel).
    # With no canonical token var, the PAT must be resolved from the USERNAME and
    # written as the _authToken — and the literal sentinel must NEVER be written.
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-PAT")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=tok-PAT" in content
    assert "x-oauth-basic" not in content


def test_git_http_password_as_token(monkeypatch, tmp_path):
    # Normal basic-auth shape: the secret/token lives in GIT_HTTP_PASSWORD (when
    # it is not the x-oauth-basic sentinel). It is used as the _authToken.
    monkeypatch.setenv("GIT_HTTP_USERNAME", "user")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "tok-PASS")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=tok-PASS" in content


def test_x_oauth_basic_never_written_as_secret(monkeypatch, tmp_path):
    # If the only signal is the x-oauth-basic sentinel with NO username PAT to
    # fall back to, we no-op rather than write the literal sentinel as a token.
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    install_npm_gitea_auth()
    assert not _npmrc(tmp_path).exists()


def test_idempotent_and_additive(monkeypatch, tmp_path):
    # Pre-existing .npmrc: one unrelated line + a STALE authToken for our key.
    npmrc = _npmrc(tmp_path)
    npmrc.write_text(
        "registry=https://registry.npmjs.org/\n"
        "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=STALE\n"
        "@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/\n"
    )
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-NEW")
    install_npm_gitea_auth()
    lines = npmrc.read_text().splitlines()
    # unrelated line preserved
    assert "registry=https://registry.npmjs.org/" in lines
    # stale token replaced, exactly once (no duplicates)
    assert sum(1 for ln in lines if ln.startswith("//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=")) == 1
    assert "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=tok-NEW" in lines
    assert "STALE" not in npmrc.read_text()
    # registry line present exactly once
    assert sum(1 for ln in lines if ln.startswith("@molecule-ai:registry=")) == 1


def test_custom_registry_key_derivation(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-X")
    monkeypatch.setenv("MOLECULE_GITEA_NPM_REGISTRY", "https://gitea.example.com/api/packages/acme/npm")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://gitea.example.com/api/packages/acme/npm/" in content
    assert "//gitea.example.com/api/packages/acme/npm/:_authToken=tok-X" in content


def test_auth_key_derivation_unit():
    assert _auth_key("https://git.moleculesai.app/api/packages/molecule-ai/npm/") == \
        "//git.moleculesai.app/api/packages/molecule-ai/npm/"
    assert _auth_key("https://h/p/npm") == "//h/p/npm/"
    assert _auth_key("no-scheme") is None


def test_token_value_never_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "supersecret-TOKEN-zzz")
    with caplog.at_level(logging.INFO):
        install_npm_gitea_auth()
    assert "supersecret-TOKEN-zzz" not in caplog.text
    # the file has it, the logs do not
    assert "supersecret-TOKEN-zzz" in _npmrc(tmp_path).read_text()


# ---------------------------------------------------------------------------
# Registry deriver precedence (audit finding C1, runtime-side).
#
# Before C1 the registry host was hardcoded and MOLECULE_GITEA_BASE_URL — SET by
# entrypoints — was never read here, so a forge-host migration left npm on the
# stale host while git honored the override. resolve_npm_registry now derives the
# registry from the shared base-host resolver. These lock the precedence:
#   1. explicit MOLECULE_GITEA_NPM_REGISTRY full URL wins;
#   2. else derive from base host (MOLECULE_PLUGIN_REGISTRY > MOLECULE_GITEA_BASE_URL);
#   3. else the documented default literal.
# ---------------------------------------------------------------------------
_DEFAULT_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"


def test_registry_defaults_when_nothing_set():
    # Neither the explicit override nor any base var set → documented default.
    # Unset envs must behave exactly as before the C1 change (backward-compat).
    assert resolve_npm_registry() == _DEFAULT_REGISTRY


def test_registry_derives_from_base_url(monkeypatch):
    # THE C1 FIX: MOLECULE_GITEA_BASE_URL is now honored — the registry is derived
    # from it + the canonical npm path suffix instead of the hardcoded host.
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://gitea.mirror.corp")
    assert resolve_npm_registry() == \
        "https://gitea.mirror.corp/api/packages/molecule-ai/npm/"


def test_registry_base_url_trailing_slash_normalized(monkeypatch):
    # A base host WITH a trailing slash must compose to exactly one separator —
    # no doubled slash before /api.
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://gitea.mirror.corp/")
    assert resolve_npm_registry() == \
        "https://gitea.mirror.corp/api/packages/molecule-ai/npm/"


def test_registry_plugin_registry_beats_base_url(monkeypatch):
    # MOLECULE_PLUGIN_REGISTRY (the provider-agnostic name core SETS) takes
    # precedence over the MOLECULE_GITEA_BASE_URL back-compat alias — same order
    # git resolution uses, so both follow one migration knob.
    monkeypatch.setenv("MOLECULE_PLUGIN_REGISTRY", "https://gitea.internal.corp")
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://git.moleculesai.app")
    assert resolve_npm_registry() == \
        "https://gitea.internal.corp/api/packages/molecule-ai/npm/"


def test_registry_explicit_override_wins(monkeypatch):
    # The explicit full-URL override beats derivation from the base host, even
    # when a base var is also set. Trailing slash is normalized on.
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://gitea.mirror.corp")
    monkeypatch.setenv("MOLECULE_GITEA_NPM_REGISTRY", "https://npm.custom.io/api/packages/acme/npm")
    assert resolve_npm_registry() == "https://npm.custom.io/api/packages/acme/npm/"


def test_install_honors_base_url_override(monkeypatch, tmp_path):
    # End-to-end through install_npm_gitea_auth: the derived registry (and its
    # matching _authToken key) reflect MOLECULE_GITEA_BASE_URL — the set-but-unread
    # bug is fixed at the .npmrc-write layer, not just the deriver.
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-BASE")
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://gitea.mirror.corp")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://gitea.mirror.corp/api/packages/molecule-ai/npm/" in content
    assert "//gitea.mirror.corp/api/packages/molecule-ai/npm/:_authToken=tok-BASE" in content
