"""Phase P1 — cross-runtime fail-closed online gate for the concierge.

Before this change ``mcp_render._RUNTIME_SPECS`` had no ``openclaw`` entry, so an
openclaw concierge fell through ``_spec_for`` to the ``claude_code`` DEFAULT and
was judged against ``.claude/settings.json`` — a file the openclaw runtime never
reads. That is the #3159 cross-runtime mis-attribution: it both false-NEGATIVES a
healthy openclaw concierge (its real native config is never consulted) and
silently mis-renders the privileged MCP to the wrong file.

These tests prove the INTERIM fix: openclaw is mapped as an explicit fail-loud
stub — present-reader returns False (and does NOT probe claude settings.json),
renderer raises NotImplementedError — so an openclaw concierge fails CLOSED and
DIAGNOSABLY. They FAIL against the pre-change source (where openclaw silently
maps to claude_code) and pass after.
"""

from __future__ import annotations

import json

import pytest

from molecule_runtime import mcp_render

MANAGEMENT_MCP_NAME = "molecule-platform"


# ── openclaw present-reader: False AND does not probe claude settings.json ──

def test_openclaw_present_false_and_does_not_probe_claude_settings(tmp_path):
    """An openclaw concierge whose MCP is NOT wired must report present=False —
    AND must NOT be judged against ``.claude/settings.json``.

    PROVE-FAIL: pre-change, openclaw fell back to claude_code, whose
    present-reader (_json_settings_has) reads ``<config>/.claude/settings.json``.
    If that file happens to declare molecule-platform, the pre-change code
    returns True (the false-positive mis-attribution). We seed exactly that file
    and assert the openclaw reader still returns False — which can only hold once
    openclaw has its own (False-returning) present-reader.
    """
    # Seed a claude settings.json under the config dir that DOES declare the MCP.
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(
        json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "npx"}}})
    )

    # openclaw must NOT consult that claude file → present is False.
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_openclaw_present_false_on_malformed_config(tmp_path):
    """Stays fail-closed (False) even with a malformed/garbage config in play."""
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text("{ not json at all")
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_openclaw_present_false_when_nothing_wired(tmp_path):
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


# ── openclaw renderer: explicit fail-loud stub (not a silent claude write) ──

def test_openclaw_render_raises_not_implemented(tmp_path):
    """The renderer must raise NotImplementedError (fail-loud) rather than
    silently writing claude's settings.json.

    PROVE-FAIL: pre-change, render_for_runtime('openclaw', …) dispatched to the
    claude renderer and WROTE ``.claude/settings.json`` (returning a path, no
    raise). After: it raises, and no claude file is written.
    """
    claude_settings = tmp_path / ".claude" / "settings.json"
    with pytest.raises(NotImplementedError):
        mcp_render.render_for_runtime(
            "openclaw", tmp_path, MANAGEMENT_MCP_NAME, {"command": "npx"}
        )
    assert not claude_settings.exists(), (
        "openclaw render must NOT write .claude/settings.json (the #3159 bug)"
    )


def test_openclaw_render_fn_directly_raises(tmp_path):
    with pytest.raises(NotImplementedError):
        mcp_render.render_openclaw_config(
            tmp_path / "out", MANAGEMENT_MCP_NAME, {"command": "npx"}
        )


# ── openclaw is classified as an unverified (fail-loud) runtime ──

def test_openclaw_is_unverified_runtime():
    """PROVE-FAIL: pre-change ``is_runtime_supported('openclaw')`` is True
    (openclaw not in _UNVERIFIED_RUNTIMES → reported supported), which is exactly
    the silent claude fall-back. After: openclaw is unsupported (fail-loud)."""
    assert mcp_render.is_runtime_supported("openclaw") is False
    assert "openclaw" in mcp_render._UNVERIFIED_RUNTIMES


@pytest.mark.parametrize("runtime", ["openclaw", "OpenClaw", "  OPENCLAW "])
def test_openclaw_normalization_variants_are_unverified(runtime):
    """Case/whitespace variants normalize to the same fail-loud entry
    (``normalize_runtime`` lowercases + strips)."""
    assert mcp_render.is_runtime_supported(runtime) is False


# ── codex present-reader: True iff config.toml declares the MCP table ──

def test_codex_present_true_when_config_toml_declares_table(tmp_path, monkeypatch):
    """A codex concierge whose ~/.codex/config.toml declares
    [mcp_servers.molecule-platform] reports present=True.

    PROVE-FAIL is covered by its negative twin below; this asserts the positive
    direction reads the codex-native TOML (not claude settings.json)."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.molecule-platform]\ncommand = "npx"\n'
        'args = ["-y", "@molecule-ai/mcp-server"]\n'
    )
    # _codex_path ignores config_path and resolves ~/.codex/config.toml; point
    # HOME at tmp_path so the reader finds our seeded file.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        mcp_render.management_mcp_present_for("codex", tmp_path, MANAGEMENT_MCP_NAME)
        is True
    )


def test_codex_present_false_when_table_absent(tmp_path, monkeypatch):
    """No [mcp_servers.molecule-platform] table → present=False (fail-closed),
    even when an UNRELATED server table is declared."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.some-other]\ncommand = "uvx"\n'
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        mcp_render.management_mcp_present_for("codex", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_codex_present_false_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.codex/config.toml at all
    assert (
        mcp_render.management_mcp_present_for("codex", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


# ── main.py: probe-wiring failure is FATAL for a platform agent ─────────────
# Pre-change main.py caught the probe-wiring Exception and only WARNED, then
# fell back to the claude settings.json check (fail-OPEN). These prove the
# decision is now fail-closed for a platform agent and lenient otherwise.

PLATFORM_AGENT_IMAGE_ENV = "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED"


def test_probe_failure_fatal_on_platform_agent(monkeypatch):
    """PROVE-FAIL: pre-change there is no ``_probe_wiring_failure_is_fatal``
    helper (ImportError) and main.py only WARNED on probe-wiring failure. After:
    the helper returns True on a platform agent so main.py aborts the boot."""
    from molecule_runtime.main import _probe_wiring_failure_is_fatal

    monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
    assert _probe_wiring_failure_is_fatal() is True


def test_probe_failure_not_fatal_on_ordinary_workspace(monkeypatch):
    """An ordinary workspace doesn't gate on the management MCP — a probe-wiring
    hiccup must not abort its boot (the claude fallback is harmless there)."""
    from molecule_runtime.main import _probe_wiring_failure_is_fatal

    monkeypatch.delenv(PLATFORM_AGENT_IMAGE_ENV, raising=False)
    assert _probe_wiring_failure_is_fatal() is False
