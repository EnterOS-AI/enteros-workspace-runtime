"""cp#3164 Layer-2 observability tests for platform_agent_identity.

PR #535/#579 added the fail-closed management-MCP gates in
``platform_agent_identity`` but the gate decisions are SILENT — a
stuck-False case leaves operators debugging without signal. cp#3164
hit exactly that: the concierge LLM didn't see the management MCP
tool and the only signal in the logs was the failure-mode symptom,
not which gate returned what.

This module pins the new structured INFO logs added to
``on_platform_agent_image``, ``mcp_server_present``, and
``ensure_management_mcp_in_settings``. Each test asserts BOTH the
gate's return value AND the log line, so a future refactor that
silently changes the decision logic fails the test loudly.

Log lines are scoped to ``platform-agent.identity`` so a single
``grep platform-agent.identity`` on the concierge's boot logs
returns the full decision chain.
"""
from __future__ import annotations

import logging

import pytest


def _capture_log(caplog, level=logging.INFO):
    """Helper: configure caplog to capture our module's logs at INFO+."""
    # Pin the explicit logger name (CR2 RC 13372 fix). The previous
    # `__name__`-derived logger would surface as
    # `molecule_runtime.platform_agent_identity` (varies with sys.path /
    # package layout), breaking the documented "grep
    # platform-agent.identity" operator contract.
    caplog.set_level(level, logger="platform-agent.identity")
    return caplog


def test_logger_name_is_explicit_and_stable():
    """CR2 RC 13372: the gate-decision logger name MUST be the stable
    explicit string ``platform-agent.identity`` so the documented
    ``grep platform-agent.identity`` operator contract holds regardless
    of how Python's import system spells the module path.
    Without this, the observability benefit of the structured logs
    is null — an operator's grep misses the lines on any package
    layout that differs from the development layout (embedded,
    pyoxidized, sys.path-stripped, etc.).
    """
    import logging

    from molecule_runtime.platform_agent_identity import (
        PLATFORM_AGENT_IDENTITY_LOGGER,
        logger,
    )

    assert PLATFORM_AGENT_IDENTITY_LOGGER == "platform-agent.identity"
    assert logger.name == "platform-agent.identity"
    # And the logger is a real, registered logger — not the root logger
    # masquerading under our name.
    resolved = logging.getLogger("platform-agent.identity")
    assert resolved is logger


def test_on_platform_agent_image_logs_env_var_value(caplog, monkeypatch):
    """The env-var value MUST appear in the boot log so operators can
    diagnose a stale-image incident without ssh-ing in."""
    monkeypatch.setenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", "1")
    _capture_log(caplog)

    from molecule_runtime.platform_agent_identity import on_platform_agent_image

    result = on_platform_agent_image()
    assert result is True
    matched = [r for r in caplog.records if "platform-agent.identity" in r.message]
    assert matched, "no platform-agent.identity log emitted on the env-check path"
    # The env var NAME + VALUE + RESULT must all be present so an operator
    # can see the full state in one line.
    msg = matched[-1].message
    assert "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED" in msg
    assert "'1'" in msg or "1" in msg
    assert "on_platform_agent_image=True" in msg


def test_on_platform_agent_image_logs_unset_state(caplog, monkeypatch):
    """When the env var is unset, the log must show BOTH the unset state
    AND the False result — this is the cp#3164 hot path."""
    monkeypatch.delenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", raising=False)
    _capture_log(caplog)

    from molecule_runtime.platform_agent_identity import on_platform_agent_image

    result = on_platform_agent_image()
    assert result is False
    matched = [r for r in caplog.records if "platform-agent.identity" in r.message]
    assert matched
    msg = matched[-1].message
    assert "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED" in msg
    # The value should show as None / unset so the operator sees the
    # problem immediately rather than guessing why the gate is False.
    assert "None" in msg or "env=" in msg


def test_mcp_server_present_logs_both_delivery_paths(caplog, tmp_path, monkeypatch):
    """mcp_server_present MUST log both delivery checks (binary +
    settings.json entry) so a False is immediately diagnosable. This is
    the cp#3164 gate that fail-closed the concierge when the plugin
    install pipeline dropped the management MCP entry."""

    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
        str(tmp_path / "no-binary"),  # binary absent
    )
    settings = tmp_path / "settings.json"
    settings.write_text('{"mcpServers": {}}')
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.SETTINGS_PATH",
        str(settings),
    )
    _capture_log(caplog)

    from molecule_runtime.platform_agent_identity import mcp_server_present

    result = mcp_server_present()
    assert result is False
    matched = [r for r in caplog.records if "platform-agent.identity" in r.message]
    assert matched
    msg = matched[-1].message
    # All three booleans + the path must appear so an operator can read
    # the full state from one log line.
    assert "mcp_server_present=False" in msg
    assert "binary=False" in msg
    assert "settings_has_entry=False" in msg
    assert str(tmp_path / "no-binary") in msg


def test_mcp_server_present_logs_when_binary_satisfies(caplog, tmp_path, monkeypatch):
    """When the BAKED binary is present (the platform-agent image
    case), the log must show binary=True as the satisfying condition."""
    binary = tmp_path / "molecule-mcp-server"
    binary.write_text("#!/bin/sh\necho ok")
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.MCPSERVER_PATH",
        str(binary),
    )
    settings = tmp_path / "settings.json"  # absent
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.SETTINGS_PATH",
        str(settings),
    )
    _capture_log(caplog)

    from molecule_runtime.platform_agent_identity import mcp_server_present

    assert mcp_server_present() is True
    matched = [r for r in caplog.records if "platform-agent.identity" in r.message]
    assert matched
    msg = matched[-1].message
    assert "mcp_server_present=True" in msg
    assert "binary=True" in msg


def test_ensure_management_mcp_in_settings_logs_skip_on_ordinary_image(
    caplog, tmp_path, monkeypatch
):
    """On an ordinary workspace image, the self-heal MUST log its no-op
    decision — otherwise a cp#3164-style investigation sees only
    symptom-level logs (concierge can't see the tool) without the
    upstream cause (the image isn't a platform-agent)."""
    monkeypatch.delenv("MOLECULE_PLATFORM_AGENT_IMAGE_BAKED", raising=False)
    _capture_log(caplog)

    from molecule_runtime.platform_agent_identity import ensure_management_mcp_in_settings

    assert ensure_management_mcp_in_settings() is False
    matched = [r for r in caplog.records if "platform-agent.identity" in r.message]
    assert matched
    msg = matched[-1].message
    assert "ensure_management_mcp_in_settings" in msg
    assert "skipped" in msg
    assert "not on platform-agent image" in msg