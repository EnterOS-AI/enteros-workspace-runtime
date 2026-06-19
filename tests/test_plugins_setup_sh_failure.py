"""Regression tests for setup.sh failure handling (issue #151)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from molecule_runtime.plugins_registry.builtins import AgentskillsAdaptor
from molecule_runtime.plugins_registry.protocol import InstallContext, InstallResult


def _make_context(tmp_path: Path) -> InstallContext:
    return InstallContext(
        configs_dir=tmp_path / "configs",
        workspace_id="ws-test",
        runtime="claude-code",
        plugin_root=tmp_path / "plugin",
        memory_filename="CLAUDE.md",
    )


@pytest.mark.asyncio
async def test_setup_sh_failure_records_error(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    setup_sh = plugin_root / "setup.sh"
    setup_sh.write_text("#!/bin/sh\necho BOOM >&2 && exit 1\n")
    setup_sh.chmod(0o755)

    adaptor = AgentskillsAdaptor("ordinary-plugin", "claude-code")
    result = await adaptor.install(_make_context(tmp_path))

    assert isinstance(result, InstallResult)
    assert any("setup.sh exited 1" in e for e in result.errors)
    assert any("setup.sh exited 1" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_setup_sh_failure_on_privileged_plugin_raises(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    setup_sh = plugin_root / "setup.sh"
    setup_sh.write_text("#!/bin/sh\necho MISSING_BINARY >&2 && exit 42\n")
    setup_sh.chmod(0o755)

    adaptor = AgentskillsAdaptor("molecule-platform-mcp", "claude-code")
    with pytest.raises(RuntimeError, match="setup.sh exited 42"):
        await adaptor.install(_make_context(tmp_path))


@pytest.mark.asyncio
async def test_setup_sh_success_has_no_errors(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    setup_sh = plugin_root / "setup.sh"
    setup_sh.write_text("#!/bin/sh\necho OK\n")
    setup_sh.chmod(0o755)

    adaptor = AgentskillsAdaptor("ordinary-plugin", "claude-code")
    result = await adaptor.install(_make_context(tmp_path))

    assert result.errors == []
    assert result.warnings == []


@pytest.mark.asyncio
async def test_setup_sh_timeout_records_error(monkeypatch, tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    setup_sh = plugin_root / "setup.sh"
    setup_sh.write_text("#!/bin/sh\necho OK\n")
    setup_sh.chmod(0o755)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=str(setup_sh), timeout=120)

    monkeypatch.setattr(
        "molecule_runtime.plugins_registry.builtins.subprocess.run", _timeout
    )

    adaptor = AgentskillsAdaptor("ordinary-plugin", "claude-code")
    result = await adaptor.install(_make_context(tmp_path))

    assert any("setup.sh timed out" in e for e in result.errors)
    assert any("setup.sh timed out" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_setup_sh_timeout_on_privileged_plugin_raises(monkeypatch, tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    setup_sh = plugin_root / "setup.sh"
    setup_sh.write_text("#!/bin/sh\necho OK\n")
    setup_sh.chmod(0o755)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=str(setup_sh), timeout=120)

    monkeypatch.setattr(
        "molecule_runtime.plugins_registry.builtins.subprocess.run", _timeout
    )

    adaptor = AgentskillsAdaptor("molecule-platform-mcp", "claude-code")
    with pytest.raises(RuntimeError, match="setup.sh timed out"):
        await adaptor.install(_make_context(tmp_path))