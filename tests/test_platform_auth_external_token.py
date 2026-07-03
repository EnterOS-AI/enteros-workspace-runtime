"""Regression: external-runtime token resolution in ``platform_auth.get_token``.

An explicitly-set ``MOLECULE_WORKSPACE_TOKEN`` must win over a STALE on-disk
``.auth_token`` when the token file lives OUTSIDE the provisioner-owned
``/configs`` volume (external-runtime hosts: ``~/.molecule-workspace`` or an
explicit ``CONFIGS_DIR``). A leftover ``.auth_token`` from a prior session used
to silently 401 the inbox poller while the heartbeat used the env token — a
split-brain reproduced live on a laptop bridge on 2026-07-02. The old code was
unconditionally file-first, so ``test_env_token_wins_over_stale_file_external``
FAILS against it (proves the guard bites).

In-container (``/configs/.auth_token``) MUST stay file-first: the platform
rotates that file on restart, so it is the fresh SSOT and an env captured at
container start may be stale.
"""

from pathlib import Path

import pytest

import molecule_runtime.platform_auth as pa


@pytest.fixture(autouse=True)
def _reset_token_cache():
    # get_token() memoises into a module global; clear it around each case.
    pa._cached_token = None
    yield
    pa._cached_token = None


def test_env_token_wins_over_stale_file_external(monkeypatch, tmp_path):
    """External path + a stale file + an env token → the env token wins."""
    configs = tmp_path / ".molecule-workspace"
    configs.mkdir()
    (configs / ".auth_token").write_text("STALE-from-prior-session")
    monkeypatch.setenv("CONFIGS_DIR", str(configs))  # explicit, non-/configs
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "FRESH-env-token")

    assert pa.get_token() == "FRESH-env-token"


def test_file_used_external_when_no_env(monkeypatch, tmp_path):
    """External path, no env token → the on-disk file is still used."""
    configs = tmp_path / ".molecule-workspace"
    configs.mkdir()
    (configs / ".auth_token").write_text("file-token")
    monkeypatch.setenv("CONFIGS_DIR", str(configs))
    monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)

    assert pa.get_token() == "file-token"


def test_env_used_external_when_no_file(monkeypatch, tmp_path):
    """External path, env set, no file anywhere → env token."""
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))  # empty dir, no .auth_token
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "env-only")

    assert pa.get_token() == "env-only"


def test_in_container_file_wins_over_env(monkeypatch):
    """In-container (/configs) keeps file-first even when env is also set:
    the platform rotates /configs/.auth_token, so it is the fresh SSOT and an
    env var captured at container start may be stale. This preserves the prior
    in-container behavior."""

    class _ConfigsAuthToken:
        # Mimics ${/configs}/.auth_token: parent == /configs, readable file.
        parent = Path("/configs")

        def exists(self):
            return True

        def read_text(self, *a, **k):
            return "rotated-file-token"

    monkeypatch.setattr(pa, "_token_file", lambda: _ConfigsAuthToken())
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "stale-env-after-rotation")

    assert pa.get_token() == "rotated-file-token"


def test_none_when_neither_file_nor_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)

    assert pa.get_token() is None
