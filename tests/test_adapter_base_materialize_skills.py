"""BaseAdapter.materialize_skills — the boot hook's fail-loud-not-fatal
downgrade contract.

The PORT itself (skills_render) raises on unsatisfiable states; the ADAPTER
hook must (a) never let those raise out of the boot path, (b) never pass them
silently — every downgrade is a logger.error — and (c) return the native
target on success so main.py can print the boot evidence line.
"""
from __future__ import annotations

import logging
import os

import pytest

from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter

requires_symlink = pytest.mark.skipif(
    os.name == "nt" and not hasattr(os, "symlink"),
    reason="symlink support required",
)


def _fake_adapter(runtime: str) -> BaseAdapter:
    class _Fake(BaseAdapter):
        @staticmethod
        def name() -> str:
            return runtime

        @staticmethod
        def display_name() -> str:
            return "Fake"

        @staticmethod
        def description() -> str:
            return "Fake adapter for tests"

        async def setup(self, config: AdapterConfig) -> None:
            return None

        async def create_executor(self, config: AdapterConfig):
            return None

    return _Fake()


def _env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    return home


@requires_symlink
def test_hook_returns_native_target_on_success(tmp_path, monkeypatch):
    home = _env(monkeypatch, tmp_path)
    config = AdapterConfig(model="m", config_path=str(tmp_path / "configs"))

    target = _fake_adapter("claude-code").materialize_skills(config)

    assert target == home / ".claude" / "skills"
    assert target.is_symlink()


def test_hook_downgrades_unmapped_runtime_to_loud_error(tmp_path, monkeypatch, caplog):
    _env(monkeypatch, tmp_path)
    config = AdapterConfig(model="m", config_path=str(tmp_path / "configs"))

    with caplog.at_level(logging.ERROR, logger="molecule_runtime.adapter_base"):
        result = _fake_adapter("some-future-runtime").materialize_skills(config)

    assert result is None
    assert any(
        "NO verified native skill-discovery convention" in r.message
        for r in caplog.records
    ), "an unmapped runtime must be LOUD, not a silent no-op"


@requires_symlink
def test_hook_downgrades_unsatisfiable_state_to_loud_error(tmp_path, monkeypatch, caplog):
    home = _env(monkeypatch, tmp_path)
    real_dir = home / ".claude" / "skills"
    real_dir.mkdir(parents=True)  # squat the link target with a REAL dir
    config = AdapterConfig(model="m", config_path=str(tmp_path / "configs"))

    with caplog.at_level(logging.ERROR, logger="molecule_runtime.adapter_base"):
        result = _fake_adapter("claude-code").materialize_skills(config)

    assert result is None
    assert real_dir.is_dir() and not real_dir.is_symlink()
    assert any(
        "could not satisfy its native skill surface" in r.message
        for r in caplog.records
    )


def test_hook_google_adk_documented_skip_returns_none(tmp_path, monkeypatch, caplog):
    _env(monkeypatch, tmp_path)
    config = AdapterConfig(model="m", config_path=str(tmp_path / "configs"))

    with caplog.at_level(logging.INFO):
        result = _fake_adapter("google-adk").materialize_skills(config)

    assert result is None
    # Documented, not silent: the skip logs its reason.
    assert any("no on-disk skill discovery" in r.message for r in caplog.records)
