"""Hermes concierge management-MCP render + present-reader.

Hermes Agent configures MCP servers in ``~/.hermes/config.yaml`` under the
``mcp_servers`` map. These tests pin the runtime-native contract so a Hermes
platform agent is never judged against ``.claude/settings.json``.
"""

from __future__ import annotations

import json

import pytest
import yaml

from molecule_runtime import mcp_render

MANAGEMENT_MCP_NAME = "molecule-platform"


def _seed_hermes_config(home, servers: dict, extra: dict | None = None) -> None:
    cfg = home / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data = dict(extra or {})
    data["mcp_servers"] = servers
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))


def test_hermes_present_does_not_probe_claude_settings(tmp_path, monkeypatch):
    """A Hermes concierge must not be judged against Claude settings."""
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(
        json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "npx"}}})
    )

    assert (
        mcp_render.management_mcp_present_for("hermes", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_hermes_present_true_when_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_hermes_config(
        tmp_path,
        {MANAGEMENT_MCP_NAME: {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]}},
    )

    assert (
        mcp_render.management_mcp_present_for("hermes", tmp_path, MANAGEMENT_MCP_NAME)
        is True
    )


def test_hermes_present_false_when_only_other_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_hermes_config(tmp_path, {"some-other": {"command": "uvx"}})

    assert (
        mcp_render.management_mcp_present_for("hermes", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_hermes_present_false_on_malformed_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("mcp_servers:\n  molecule-platform: [")

    assert (
        mcp_render.management_mcp_present_for("hermes", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_hermes_render_writes_native_config_not_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_settings = tmp_path / ".claude" / "settings.json"

    target = mcp_render.render_for_runtime(
        "hermes",
        tmp_path,
        MANAGEMENT_MCP_NAME,
        {
            "command": "npx",
            "args": ["-y", "@molecule-ai/mcp-server"],
            "env": {"MOLECULE_MCP_MODE": "management"},
        },
    )

    assert not claude_settings.exists()
    expected = tmp_path / ".hermes" / "config.yaml"
    assert target == expected
    data = yaml.safe_load(expected.read_text())
    server = data["mcp_servers"][MANAGEMENT_MCP_NAME]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@molecule-ai/mcp-server"]
    assert server["env"] == {"MOLECULE_MCP_MODE": "management"}
    assert (
        mcp_render.management_mcp_present_for("hermes", tmp_path, MANAGEMENT_MCP_NAME)
        is True
    )


def test_hermes_render_is_additive_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_hermes_config(
        tmp_path,
        {"keep-me": {"command": "uvx"}},
        extra={"model": "nous:test-model"},
    )
    cfg = tmp_path / ".hermes" / "config.yaml"

    mcp_render.render_for_runtime(
        "hermes", tmp_path, MANAGEMENT_MCP_NAME, {"command": "npx"}
    )
    first = cfg.read_text()
    mcp_render.render_for_runtime(
        "hermes", tmp_path, MANAGEMENT_MCP_NAME, {"command": "npx"}
    )
    data = yaml.safe_load(cfg.read_text())

    assert cfg.read_text() == first
    assert data["model"] == "nous:test-model"
    assert data["mcp_servers"]["keep-me"] == {"command": "uvx"}
    assert data["mcp_servers"][MANAGEMENT_MCP_NAME] == {"command": "npx"}


def test_hermes_render_fn_directly_writes(tmp_path):
    out = tmp_path / "config.yaml"
    mcp_render.render_hermes_config(out, MANAGEMENT_MCP_NAME, {"command": "npx"})
    data = yaml.safe_load(out.read_text())
    assert data["mcp_servers"][MANAGEMENT_MCP_NAME] == {"command": "npx"}


def test_hermes_is_supported_runtime():
    assert mcp_render.is_runtime_supported("hermes") is True
    assert "hermes" not in mcp_render._UNVERIFIED_RUNTIMES


@pytest.mark.parametrize("runtime", ["hermes", "Hermes", "  HERMES "])
def test_hermes_normalization_variants_are_supported(runtime):
    assert mcp_render.is_runtime_supported(runtime) is True
