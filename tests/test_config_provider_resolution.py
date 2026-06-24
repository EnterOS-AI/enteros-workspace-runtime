"""Regression tests for runtime config provider resolution.

Covers template-143 / molecule-ai-workspace-template-claude-code#143: a model id
like ``moonshot/kimi-k2.6`` must resolve to the provider declared for that id in
``runtime_config.models`` (``platform``), not to the raw namespace prefix
``moonshot`` that the claude-code adapter rejects as an unknown registry name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from molecule_runtime import config as config_mod


def _write_yaml(dest: Path, text: str) -> None:
    dest.write_text(text)


def test_load_config_uses_model_list_provider_for_namespaced_model(tmp_path, monkeypatch):
    """A namespaced model id listed with provider=platform resolves to platform."""
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    _write_yaml(
        configs_dir / "config.yaml",
        """
name: Test Workspace
model: moonshot/kimi-k2.6
runtime: claude-code
runtime_config:
  models:
    - id: moonshot/kimi-k2.6
      provider: platform
""",
    )

    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))
    # Disable /opt fallback.
    monkeypatch.setattr(
        config_mod,
        "Path",
        lambda p: Path(p)
        if str(p) != "/opt/molecule-platform-agent-template/config.yaml"
        else (tmp_path / "opt_missing" / "config.yaml"),
    )

    loaded = config_mod.load_config()
    assert loaded.model == "moonshot/kimi-k2.6"
    assert loaded.provider == "platform"
    assert loaded.runtime_config.provider == "platform"


def test_load_config_falls_back_to_prefix_when_model_not_listed(tmp_path, monkeypatch):
    """An unlisted namespaced model still falls back to prefix derivation."""
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    _write_yaml(
        configs_dir / "config.yaml",
        """
name: Test Workspace
model: openai/gpt-4
runtime: claude-code
""",
    )

    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))
    monkeypatch.setattr(
        config_mod,
        "Path",
        lambda p: Path(p)
        if str(p) != "/opt/molecule-platform-agent-template/config.yaml"
        else (tmp_path / "opt_missing" / "config.yaml"),
    )

    loaded = config_mod.load_config()
    assert loaded.model == "openai/gpt-4"
    assert loaded.provider == "openai"


def test_resolve_provider_from_models_normalizes_separator():
    """Colon-style model ids (LangChain convention) match slash-style entries."""
    models = [
        {"id": "anthropic/claude-opus-4-7", "provider": "platform"},
    ]
    assert config_mod._resolve_provider_from_models("anthropic:claude-opus-4-7", models) == "platform"
    assert config_mod._resolve_provider_from_models("anthropic/claude-opus-4-7", models) == "platform"


def test_resolve_provider_from_models_returns_empty_when_no_provider():
    """A listed model with no provider field does not block fallback derivation."""
    models = [{"id": "sonnet"}]
    assert config_mod._resolve_provider_from_models("sonnet", models) == ""


def test_explicit_env_provider_wins_over_model_list(tmp_path, monkeypatch):
    """LLM_PROVIDER env must still override the model-list inference."""
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic-api")

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    _write_yaml(
        configs_dir / "config.yaml",
        """
name: Test Workspace
model: moonshot/kimi-k2.6
runtime: claude-code
runtime_config:
  models:
    - id: moonshot/kimi-k2.6
      provider: platform
""",
    )

    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))
    monkeypatch.setattr(
        config_mod,
        "Path",
        lambda p: Path(p)
        if str(p) != "/opt/molecule-platform-agent-template/config.yaml"
        else (tmp_path / "opt_missing" / "config.yaml"),
    )

    loaded = config_mod.load_config()
    assert loaded.provider == "anthropic-api"
