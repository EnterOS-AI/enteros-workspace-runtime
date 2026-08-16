"""Tests for npm_auth — @molecule-ai npm scope config + shared forge SSOT.

The concierge MCP (`npx @molecule-ai/mcp-server`) needs the @molecule-ai scope
registry in ~/.npmrc to resolve. PLATFORM CONTRACT: the registry serves the
scope ANONYMOUSLY (packument + tarball, proven live 2026-07-09), so the scope
line is UNCONDITIONAL and anonymous is the safe floor. npm auth is ADDITIVE-ONLY
and only from a DESIGNATED package token — a mis-scoped token (e.g. a git
repo-transport PAT) is 401-rejected by the registry, strictly worse than no
token, so git-transport creds are NEVER written as an npm _authToken.

Registry host is derived from the forge base-host SSOT (audit finding C1) shared
with git (plugin_sources): resolve_gitea_base / resolve_npm_registry. The
provider-neutral MOLECULE_NPM_REGISTRY override wins over the legacy Gitea alias.

Split-resolver boundary: git (plugin_sources) uses gitea_read_token, which
KEEPS the git-http path (a read:repository transport cred is exactly what a
private-repo git clone needs); npm uses _npm_package_token, which does NOT — the
same forge host, two protocols, two scope needs.

These lock in: unconditional scope line, package-token precedence
(MOLECULE_NPM_TOKEN > repo-token vars), the 401-poison guard (git-http creds
never become an _authToken), the HOME-split + clobber-reassert contracts,
idempotent/additive writes, the C1 registry deriver + provider-neutral override,
git's git-http token path is preserved, correct key derivation, and no token in
logs.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from molecule_runtime.npm_auth import (
    _auth_key,
    gitea_read_token,
    install_npm_gitea_auth,
    resolve_npm_registry,
)

_ALL_TOKEN_VARS = ("MOLECULE_TEMPLATE_REPO_TOKEN", "GITEA_TOKEN", "MOLECULE_NPM_TOKEN",
                   "GIT_HTTP_USERNAME", "GIT_HTTP_PASSWORD",
                   "MOLECULE_GITEA_NPM_REGISTRY", "MOLECULE_NPM_REGISTRY",
                   "MOLECULE_GITEA_BASE_URL", "MOLECULE_PLUGIN_REGISTRY")

_DEFAULT_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Isolate HOME + clear all token/registry env so tests are deterministic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for v in _ALL_TOKEN_VARS:
        monkeypatch.delenv(v, raising=False)
    yield


def _npmrc(tmp_path: Path) -> Path:
    return tmp_path / ".npmrc"


# ---------------------------------------------------------------------------
# install_npm_gitea_auth — scope line, token, HOME-split, clobber.
# ---------------------------------------------------------------------------
def test_writes_registry_and_authtoken(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-AAA")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/" in content
    assert "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=tok-AAA" in content


def test_no_token_still_writes_scope_registry(tmp_path):
    """HARD CONTRACT: the @molecule-ai scope-registry line is UNCONDITIONAL.

    The gitea registry serves the scope anonymously, and the concierge
    management MCP (npx @molecule-ai/mcp-server) must resolve on EVERY
    runtime — including tokenless self-host boots. The pre-2026-07-09
    token-coupled skip is exactly what fail-closed the hermes concierge on
    the core#3082 loaded_mcp_tools gate. A regression back to "no token →
    no scope line" must go RED here.
    """
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/" in content
    # no token → no auth line (never write an empty/placeholder secret)
    assert "_authToken" not in content


def test_npmrc_is_chmod_0600(monkeypatch, tmp_path):
    # The token is at rest in ~/.npmrc — it must be 0600 (created restricted,
    # no world-readable window). Guards the chmod/hardening regression.
    import stat
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-AAA")
    install_npm_gitea_auth()
    mode = stat.S_IMODE(_npmrc(tmp_path).stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_repo_scoped_tokens_are_NOT_written_as_npm_token(monkeypatch, tmp_path):
    """MOLECULE_TEMPLATE_REPO_TOKEN / GITEA_TOKEN must NEVER become the npm token.

    Both are general forge credentials, not package tokens. Measured on prod
    2026-08-15, MOLECULE_TEMPLATE_REPO_TOKEN returns 200 on the repo API, **401
    on the packages API**, and 403 on whoami — while the SAME request with no
    Authorization header returns 200. Writing it therefore converts a working
    anonymous fetch into a hard 401 and fail-closes every not-pre-baked MCP
    plugin at launch (molecule-ai-plugin-image-gen#2).

    Anonymous is the floor and it beats a rejected token. A regression that
    re-admits either var to the precedence list must go RED here.
    """
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-GHU")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "tok-GHP")
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-CANON")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    # the scope line is still unconditional …
    assert "@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/" in content
    # … and NOTHING is written as a credential
    assert "_authToken" not in content
    for leaked in ("tok-CANON", "tok-GITEA", "tok-GHU", "tok-GHP"):
        assert leaked not in content, f"{leaked} must never reach .npmrc"


def test_designated_npm_token_still_wins_over_forge_tokens(monkeypatch, tmp_path):
    """A real package token IS written, even alongside forge credentials.

    The fix narrows WHICH var may supply the token; it does not disable auth.
    A genuinely private @molecule-ai package is still reachable by setting
    MOLECULE_NPM_TOKEN to a read:package token.
    """
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-CANON")
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-PKG")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "_authToken=tok-PKG" in content
    assert "tok-CANON" not in content and "tok-GITEA" not in content


def test_molecule_npm_token_highest_precedence(monkeypatch, tmp_path):
    # MOLECULE_NPM_TOKEN is the ONLY var that may supply the npm token; the
    # forge vars below are not candidates at all any more.
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-REPO")
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-NPM")
    install_npm_gitea_auth()
    assert "_authToken=tok-NPM" in _npmrc(tmp_path).read_text()


def test_git_http_transport_creds_are_NOT_written_as_npm_token(monkeypatch, tmp_path):
    """HARD CONTRACT (401-poison guard): a git-TRANSPORT credential must NEVER
    become the npm _authToken.

    Proven live 2026-07-09: the Gitea npm registry serves @molecule-ai
    ANONYMOUSLY (packument + tarball, HTTP 200), but a token WITHOUT read:package
    is REJECTED with HTTP 401. The git-http PAT is repo-scoped, so writing it
    turns working anonymous access into a hard 401 — strictly worse than no
    token. Both the x-oauth-basic concierge shape AND the plain
    password-as-token shape must resolve to ANONYMOUS (scope line only), never
    an _authToken. A regression that re-introduces the git-http token path (as
    an earlier version of this module did, which fail-closed the hermes
    concierge) must go RED here.
    """
    # x-oauth-basic concierge shape (PAT in username)
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-PAT")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=" in content
    assert "_authToken" not in content, "git-http PAT must NOT be an npm token"
    assert "tok-PAT" not in content
    assert "x-oauth-basic" not in content

    # plain password-as-token shape — also a transport cred, also anonymous
    _npmrc(tmp_path).unlink()
    monkeypatch.setenv("GIT_HTTP_USERNAME", "user")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "tok-PASS")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=" in content
    assert "_authToken" not in content
    assert "tok-PASS" not in content


def test_scope_written_to_agent_home_too(monkeypatch, tmp_path):
    """HOME-split contract: the scope config lands in the canonical agent home
    even when $HOME differs (runtime main as root, MCP spawn as agent)."""
    from molecule_runtime import npm_auth

    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    run_home = tmp_path / "root-home"
    run_home.mkdir()
    monkeypatch.setenv("HOME", str(run_home))
    monkeypatch.setattr(npm_auth, "_AGENT_HOME", agent_home)
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-BOTH")
    install_npm_gitea_auth()
    for home in (run_home, agent_home):
        content = (home / ".npmrc").read_text()
        assert "@molecule-ai:registry=" in content, home
        assert "_authToken=tok-BOTH" in content, home


def test_reassert_after_clobber_restores_scope(monkeypatch, tmp_path):
    """Clobber contract: a template setup step that rewrites ~/.npmrc (hermes's
    node installer) is healed by the post-adapter-setup re-assert call."""
    install_npm_gitea_auth()
    npmrc = _npmrc(tmp_path)
    assert "@molecule-ai:registry=" in npmrc.read_text()
    # simulate the clobber: setup replaces the file with its own config
    npmrc.write_text("registry=https://registry.npmjs.org/\n")
    install_npm_gitea_auth()
    content = npmrc.read_text()
    assert "@molecule-ai:registry=" in content
    assert "registry=https://registry.npmjs.org/" in content  # additive — theirs kept


def test_idempotent_and_additive(monkeypatch, tmp_path):
    # Pre-existing .npmrc: one unrelated line + a STALE authToken for our key.
    npmrc = _npmrc(tmp_path)
    npmrc.write_text(
        "registry=https://registry.npmjs.org/\n"
        "//git.moleculesai.app/api/packages/molecule-ai/npm/:_authToken=STALE\n"
        "@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/\n"
    )
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-NEW")
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
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-X")
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
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "supersecret-TOKEN-zzz")
    with caplog.at_level(logging.INFO):
        install_npm_gitea_auth()
    assert "supersecret-TOKEN-zzz" not in caplog.text
    # the file has it, the logs do not
    assert "supersecret-TOKEN-zzz" in _npmrc(tmp_path).read_text()


# ---------------------------------------------------------------------------
# Registry deriver precedence (audit finding C1, runtime-side) + provider-neutral.
#
# Before C1 the registry host was hardcoded and MOLECULE_GITEA_BASE_URL — SET by
# entrypoints — was never read here, so a forge-host migration left npm on the
# stale host while git honored the override. resolve_npm_registry now derives the
# registry from the shared base-host resolver. Precedence:
#   1. provider-neutral MOLECULE_NPM_REGISTRY full URL (wins outright);
#   2. legacy MOLECULE_GITEA_NPM_REGISTRY full URL;
#   3. derive from base host (MOLECULE_PLUGIN_REGISTRY > MOLECULE_GITEA_BASE_URL);
#   4. the documented default literal.
# ---------------------------------------------------------------------------
def test_registry_defaults_when_nothing_set():
    # Neither an override nor any base var set → documented default.
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


def test_registry_explicit_gitea_override_wins(monkeypatch):
    # The explicit full-URL override beats derivation from the base host, even
    # when a base var is also set. Trailing slash is normalized on.
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://gitea.mirror.corp")
    monkeypatch.setenv("MOLECULE_GITEA_NPM_REGISTRY", "https://npm.custom.io/api/packages/acme/npm")
    assert resolve_npm_registry() == "https://npm.custom.io/api/packages/acme/npm/"


def test_provider_neutral_registry_env_takes_precedence(monkeypatch, tmp_path):
    """MOLECULE_NPM_REGISTRY (provider-neutral) beats the legacy Gitea alias —
    the contract is "the scope resolves", not "it resolves from Gitea"."""
    monkeypatch.setenv("MOLECULE_GITEA_NPM_REGISTRY", "https://legacy.example.com/npm")
    monkeypatch.setenv("MOLECULE_NPM_REGISTRY", "https://npm.other-provider.example.com/molecule")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://npm.other-provider.example.com/molecule/" in content
    assert "legacy.example.com" not in content


def test_install_honors_base_url_override(monkeypatch, tmp_path):
    # End-to-end through install_npm_gitea_auth: the derived registry (and its
    # matching _authToken key) reflect MOLECULE_GITEA_BASE_URL — the set-but-unread
    # bug is fixed at the .npmrc-write layer, not just the deriver.
    monkeypatch.setenv("MOLECULE_NPM_TOKEN", "tok-BASE")
    monkeypatch.setenv("MOLECULE_GITEA_BASE_URL", "https://gitea.mirror.corp")
    install_npm_gitea_auth()
    content = _npmrc(tmp_path).read_text()
    assert "@molecule-ai:registry=https://gitea.mirror.corp/api/packages/molecule-ai/npm/" in content
    assert "//gitea.mirror.corp/api/packages/molecule-ai/npm/:_authToken=tok-BASE" in content


# ---------------------------------------------------------------------------
# git's shared token resolver (plugin_sources) — the git-http path is PRESERVED.
#
# npm dropped the git-http path (401-poison), but GIT legitimately needs the
# read:repository git-transport credential (a private plugin repo's 401 triggers
# git's credential helper, which supplies gitea_read_token). These lock in that
# the npm poison-drop did NOT regress git's credential resolution.
# ---------------------------------------------------------------------------
def test_gitea_read_token_prefers_canonical(monkeypatch):
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-GHU")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "tok-GHP")
    monkeypatch.setenv("GITEA_TOKEN", "tok-GITEA")
    monkeypatch.setenv("MOLECULE_TEMPLATE_REPO_TOKEN", "tok-CANON")
    assert gitea_read_token() == "tok-CANON"


def test_gitea_read_token_git_http_password_shape(monkeypatch):
    # Normal basic-auth shape: the token is GIT_HTTP_PASSWORD (not the sentinel).
    monkeypatch.setenv("GIT_HTTP_USERNAME", "user")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "tok-PASS")
    assert gitea_read_token() == "tok-PASS"


def test_gitea_read_token_x_oauth_basic_uses_username_pat(monkeypatch):
    # VERIFIED live concierge shape: PAT in GIT_HTTP_USERNAME, password is the
    # x-oauth-basic sentinel. git must resolve the PAT from the username — the
    # sentinel is never returned as a token.
    monkeypatch.setenv("GIT_HTTP_USERNAME", "tok-PAT")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    assert gitea_read_token() == "tok-PAT"


def test_gitea_read_token_sentinel_only_is_empty(monkeypatch):
    # Only the sentinel, no username PAT → "" (never return the literal sentinel).
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "x-oauth-basic")
    assert gitea_read_token() == ""
