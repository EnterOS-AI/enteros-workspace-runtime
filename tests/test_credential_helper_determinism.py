"""Regression: credential_helper must pick a single SCM provider deterministically.

PR/Issue: runtime#104 — "Bootstrap should pick ONE git-credential flow
deterministically — stacking GITHUB_TOKEN onto a Gitea agent hijacks it."

These tests assert the new contract:
  1. ``GIT_PROVIDER=gitea`` → install_credential_helper() is a no-op
     (no helper scripts extracted, no git config, no daemon, no gh auth).
  2. ``GIT_PROVIDER=github`` → install_credential_helper() installs the
     GitHub machinery regardless of what other creds are present.
  3. Unset, only Gitea creds → gitea path, no GitHub machinery.
  4. Unset, only GitHub creds → github path, machinery installed.
  5. Unset, BOTH creds → gitea default + LOUD warning (the bug class
     from the 2026-06-08 MiniMax cred incident).
  6. Unset, neither creds → no-op (no machinery installed).

The tests patch shutil.which (git + gh) so they don't require a real
git/gh binary, and patch subprocess so we can assert calls without
actually running anything.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from unittest import mock

import pytest


@pytest.fixture
def fresh_helper(monkeypatch, tmp_path):
    """Fresh import of credential_helper with HOME pointed at tmp_path.

    The module reads HOME at import time to resolve its install/cache
    directories, so we re-import per test. We also clear any cached
    gitconfig by pointing HOME at an empty tmp dir.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Drop any GIT_PROVIDER / Gitea / GitHub creds from the env so tests
    # are independent. The test sets what it needs.
    for k in (
        "GIT_PROVIDER",
        "GIT_HTTP_USERNAME", "GIT_HTTP_PASSWORD",
        "GITEA_TOKEN",
        "GH_TOKEN", "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    # Force a re-import so the module re-reads HOME.
    sys.modules.pop("molecule_runtime.credential_helper", None)
    import molecule_runtime.credential_helper as ch
    importlib.reload(ch)
    yield ch
    # Teardown
    sys.modules.pop("molecule_runtime.credential_helper", None)


@pytest.fixture
def no_git_or_gh(monkeypatch):
    """Make shutil.which return None for git and gh (we never want real
    subprocess in these tests)."""
    monkeypatch.setattr(
        "shutil.which", lambda name: None,
    )


def _call_install(monkeypatch, ch):
    """Call ch.install_credential_helper() with all subprocess + which
    patched so it's a no-op at the OS level. Returns the captured log
    records for assertion.
    """
    import logging
    records = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    cap = _CaptureHandler(level=logging.DEBUG)
    ch.log.addHandler(cap)
    try:
        # Patch everything that would do real work.
        # NOTE: do NOT set extract.return_value to a str — install_credential_helper
        # does `helper_dir / _DAEMON_SCRIPT` which fails on str/str
        # (TypeError at line ~330). Let the MagicMock default return a
        # MagicMock that supports `/` with any operand.
        with mock.patch.object(ch, "_extract_scripts") as extract, \
             mock.patch.object(ch, "_TOKEN_CACHE_DIR") as cache_dir, \
             mock.patch.object(ch, "_initial_gh_auth") as initial_gh, \
             mock.patch.object(ch, "_start_refresh_daemon") as daemon:
            cache_dir.mkdir = mock.MagicMock()
            cache_dir.chmod = mock.MagicMock()
            ch.install_credential_helper()
            return {
                "extract_called": extract.called,
                "initial_gh_called": initial_gh.called,
                "daemon_called": daemon.called,
                "records": records,
            }
    finally:
        ch.log.removeHandler(cap)


# ---------- 1. GIT_PROVIDER=gitea → no-op ----------

def test_git_provider_gitea_is_noop(monkeypatch, fresh_helper, no_git_or_gh):
    """GIT_PROVIDER=gitea → no helper install, no daemon, no gh auth."""
    monkeypatch.setenv("GIT_PROVIDER", "gitea")
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-b")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "secret")
    # Even if GitHub creds are also present, gitea wins.
    monkeypatch.setenv("GH_TOKEN", "ghs_should_not_be_used")

    result = _call_install(monkeypatch, fresh_helper)
    assert not result["extract_called"], "GIT_PROVIDER=gitea must not extract helper scripts"
    assert not result["initial_gh_called"], "GIT_PROVIDER=gitea must not call _initial_gh_auth"
    assert not result["daemon_called"], "GIT_PROVIDER=gitea must not start the refresh daemon"
    msgs = [r.getMessage() for r in result["records"]]
    assert any("provider=gitea" in m for m in msgs), (
        f"expected a 'provider=gitea' log line; got: {msgs!r}"
    )


# ---------- 2. GIT_PROVIDER=github → install even if Gitea creds present ----------

def test_git_provider_github_installs_even_with_gitea_creds(monkeypatch, fresh_helper, no_git_or_gh):
    """GIT_PROVIDER=github is an explicit override; install regardless of
    other creds. (The agent operator chose github on purpose.)"""
    monkeypatch.setenv("GIT_PROVIDER", "github")
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-b")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "secret")
    monkeypatch.setenv("GH_TOKEN", "ghs_real_token")

    result = _call_install(monkeypatch, fresh_helper)
    assert result["extract_called"], "GIT_PROVIDER=github must extract helper scripts"
    assert result["initial_gh_called"], "GIT_PROVIDER=github must run _initial_gh_auth"
    assert result["daemon_called"], "GIT_PROVIDER=github must start the refresh daemon"


# ---------- 3. Unset + only Gitea creds → gitea path ----------

def test_unset_only_gitea_creds_uses_gitea(monkeypatch, fresh_helper, no_git_or_gh):
    """No GIT_PROVIDER, only Gitea creds present → gitea path, no
    GitHub machinery. (The KIMI-style healthy agent — only Gitea
    creds, never GitHub.)"""
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-a")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "secret")

    result = _call_install(monkeypatch, fresh_helper)
    assert not result["extract_called"]
    assert not result["initial_gh_called"]
    assert not result["daemon_called"]
    msgs = [r.getMessage() for r in result["records"]]
    assert any("provider=gitea" in m for m in msgs), (
        f"gitea-only env should land in the gitea provider; got: {msgs!r}"
    )


# ---------- 4. Unset + only GitHub creds → github path ----------

def test_unset_only_github_creds_uses_github(monkeypatch, fresh_helper, no_git_or_gh):
    """No GIT_PROVIDER, only GitHub creds present → github path, machinery
    installed. (Pre-2026-06-08 healthy state — only the GitHub App flow.)"""
    monkeypatch.setenv("GH_TOKEN", "ghs_real_token")

    result = _call_install(monkeypatch, fresh_helper)
    assert result["extract_called"]
    assert result["initial_gh_called"]
    assert result["daemon_called"]


# ---------- 5. Unset + BOTH creds → gitea default + LOUD warning ----------

def test_unset_both_creds_defaults_to_gitea_with_warning(monkeypatch, fresh_helper, no_git_or_gh):
    """No GIT_PROVIDER, BOTH Gitea and GitHub creds → gitea default
    (org canonical) + a STACKED-FLOWS warning. This is the regression
    case from the 2026-06-08 MiniMax incident."""
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-b")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "real_gitea_creds")
    monkeypatch.setenv("GH_TOKEN", "ghs_remediation_added_this")

    result = _call_install(monkeypatch, fresh_helper)
    # gitea path → no machinery
    assert not result["extract_called"], (
        "Stacked flows should default to gitea → no GitHub machinery"
    )
    assert not result["initial_gh_called"]
    assert not result["daemon_called"]
    msgs = [r.getMessage() for r in result["records"]]
    assert any("STACKED" in m for m in msgs), (
        f"Stacked flows must emit a LOUD warning; got: {msgs!r}"
    )
    assert any("provider=gitea" in m for m in msgs)


def test_unset_both_creds_git_provider_github_overrides_default(monkeypatch, fresh_helper, no_git_or_gh):
    """GIT_PROVIDER=github is an explicit override of the stacked-flows
    default. Even with both creds present, the explicit choice wins
    (and the warning still fires so the operator sees the stack)."""
    monkeypatch.setenv("GIT_PROVIDER", "github")
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-b")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "real")
    monkeypatch.setenv("GH_TOKEN", "ghs_real")

    result = _call_install(monkeypatch, fresh_helper)
    assert result["extract_called"]
    assert result["initial_gh_called"]


# ---------- 6. Unset + neither creds → no-op ----------

def test_unset_no_creds_is_noop(monkeypatch, fresh_helper, no_git_or_gh):
    """No GIT_PROVIDER, no creds of any kind → no machinery. (Pre-helper
    workspace state — git just uses env-based auth, which may be empty.)"""
    result = _call_install(monkeypatch, fresh_helper)
    assert not result["extract_called"]
    assert not result["initial_gh_called"]
    assert not result["daemon_called"]
    msgs = [r.getMessage() for r in result["records"]]
    assert any("no SCM provider configured" in m for m in msgs), (
        f"unset-and-empty should log 'no SCM provider configured'; got: {msgs!r}"
    )


# ---------- 7. Unrecognized GIT_PROVIDER value is loud, not silent ----------

def test_unrecognized_git_provider_warns(monkeypatch, fresh_helper, no_git_or_gh):
    """GIT_PROVIDER=garbage → warn loudly, treat as unset."""
    monkeypatch.setenv("GIT_PROVIDER", "garbage")
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-b")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "secret")

    result = _call_install(monkeypatch, fresh_helper)
    msgs = [r.getMessage() for r in result["records"]]
    assert any("not a recognized value" in m for m in msgs), (
        f"unrecognized GIT_PROVIDER must be loud; got: {msgs!r}"
    )
    # And the gitea creds should still let us default to gitea
    assert any("provider=gitea" in m for m in msgs)


# ---------- 8. The OLD implicit/greedy GH_TOKEN order is GONE ----------

def test_github_creds_alone_no_longer_cause_git_config_when_gitea_provider(monkeypatch, fresh_helper, no_git_or_gh):
    """The OLD bug: presence of GH_TOKEN shadowed a working Gitea flow.
    Verify the NEW behavior: GIT_PROVIDER=gitea + GH_TOKEN set → still
    no-op on the GitHub side. (The smoke-check for the 2026-06-08 RC.)"""
    monkeypatch.setenv("GIT_PROVIDER", "gitea")
    monkeypatch.setenv("GIT_HTTP_USERNAME", "agent-dev-b")
    monkeypatch.setenv("GIT_HTTP_PASSWORD", "real_creds")
    monkeypatch.setenv("GH_TOKEN", "ghs_should_NOT_take_over")

    result = _call_install(monkeypatch, fresh_helper)
    # gitea wins → no GitHub machinery
    assert not result["extract_called"], (
        "GIT_PROVIDER=gitea must NOT install GitHub machinery even when "
        "GH_TOKEN is present (the regression from runtime#104)"
    )
    assert not result["initial_gh_called"]
    assert not result["daemon_called"]
