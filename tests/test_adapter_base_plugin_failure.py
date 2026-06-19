"""Regression tests for adapter-level plugin install failure propagation."""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

import pytest

from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter
from molecule_runtime.plugins import LoadedPlugins, Plugin
from molecule_runtime.plugins_registry.builtins import _PRIVILEGED_MCP_PLUGIN


@dataclass
class _FakeAdaptor:
    raise_on_install: bool = False

    async def install(self, ctx):
        if self.raise_on_install:
            raise RuntimeError("privileged setup.sh failed")
        return object()


class _FakeAdapter(BaseAdapter):
    @staticmethod
    def name() -> str:
        return "claude-code"

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


@pytest.mark.asyncio
async def test_install_plugins_via_registry_propagates_privileged_failure(tmp_path: Path, monkeypatch):
    """A RuntimeError from the privileged MCP plugin install must not be swallowed."""
    adapter = _FakeAdapter()
    config = AdapterConfig(model="anthropic:claude-sonnet-4-6", config_path=str(tmp_path))

    plugins = LoadedPlugins(
        plugins=[Plugin(name=_PRIVILEGED_MCP_PLUGIN, path=str(tmp_path / "plugin"))]
    )

    def _fake_resolve(plugin_name, runtime, plugin_root):
        return _FakeAdaptor(raise_on_install=True), "registry"

    monkeypatch.setattr("molecule_runtime.plugins_registry.resolve", _fake_resolve)

    with pytest.raises(RuntimeError, match="privileged setup.sh failed"):
        await adapter.install_plugins_via_registry(config, plugins)


@pytest.mark.asyncio
async def test_install_plugins_via_registry_logs_non_privileged_failure(tmp_path: Path, monkeypatch):
    """Non-privileged plugin install failures are still logged and swallowed."""
    adapter = _FakeAdapter()
    config = AdapterConfig(model="anthropic:claude-sonnet-4-6", config_path=str(tmp_path))

    plugins = LoadedPlugins(
        plugins=[Plugin(name="ordinary-plugin", path=str(tmp_path / "plugin"))]
    )

    def _fake_resolve(plugin_name, runtime, plugin_root):
        return _FakeAdaptor(raise_on_install=True), "registry"

    monkeypatch.setattr("molecule_runtime.plugins_registry.resolve", _fake_resolve)

    results = await adapter.install_plugins_via_registry(config, plugins)
    assert results == []
