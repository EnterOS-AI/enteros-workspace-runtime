"""Tests for npm_auth.install_npm_gitea_auth — gitea npm-registry auth (RCA 2026-06-24).

The concierge MCP (`npx @molecule-ai/mcp-server`) needs the gitea registry +
_authToken in ~/.npmrc or it ETARGETs the private package and never starts.
These lock in: writes the right lines, SSOT token precedence, no-op without a
token, idempotent/additive, correct key derivation, and no token in logs.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from molecule_runtime.npm_auth import _auth_key, install_npm_gitea_auth

_ALL_TOKEN_VARS = ("MOLECULE_TEMPLATE_REPO_TOKEN", "GITEA_TOKEN", "GIT_HTTP_USERNAME",
                   "GIT_HTTP_PASSWORD", "MOLECULE_GITEA_NPM_REGISTRY")


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
    # MOLECULE_TEMPLATE_REPO_TOKEN wins over GITEA_TOKEN; the ambiguous gitea
    # HTTPS-auth vars are NOT a token source and are ignored entirely.
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-GHU")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-CANON")
    install_npm_gitea_auth()
    assert "_authToken=tok-CANON" in _npmrc(tmp_path).read_text()


def test_git_http_vars_are_not_a_token_source(monkeypatch, tmp_path):
    # The gitea HTTPS-auth pair is ambiguous: core's concierge uses
    # GIT_HTTP_USERNAME=<PAT> + GIT_HTTP_PASSWORD="x-oauth-basic", while
    # credential_helper uses USERNAME=<name> + PASSWORD=<secret>. Neither var
    # reliably holds the token, so with only GIT_HTTP_* set (no canonical token
    # var) we no-op rather than write a wrong _authToken — e.g. the literal
    # "x-oauth-basic" or an account name.
    monkeypatch.setenv("GIT_HTTP_USERNAME", "9f90deadbeef")  # PAT-as-username pattern
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
