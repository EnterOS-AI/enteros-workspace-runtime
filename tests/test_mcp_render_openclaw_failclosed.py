"""Phase P4 — openclaw concierge management-MCP render + present-reader.

Phase P1 (#178) mapped openclaw as an INTERIM fail-loud stub (renderer raised
NotImplementedError; present-reader returned False) so an openclaw concierge
failed CLOSED + diagnosably instead of silently falling through ``_spec_for`` to
the ``claude_code`` default and being judged against ``.claude/settings.json`` —
a file the openclaw runtime never reads (the #3159 cross-runtime mis-attribution).

Phase P4 (this change) replaces that stub with the CONCRETE implementation,
pinned against a live ``openclaw@2026.5.7`` CLI: ``openclaw mcp set <name>
'<json>'`` persists the stdio descriptor (``{command, args?, env?}``) under
``mcp.servers.<name>`` in ``~/.openclaw/openclaw.json``. The renderer writes that
exact shape and the present-reader parses it — both fail-CLOSED on a missing /
malformed config, so a genuinely MCP-less openclaw concierge still degrades.

These tests prove: the present-reader reads the openclaw-NATIVE config (never
``.claude/settings.json``), the renderer writes ``~/.openclaw/openclaw.json``
(NOT ``.claude/settings.json``), and openclaw is no longer ``_UNVERIFIED``.
"""

from __future__ import annotations

import json

import pytest

from molecule_runtime import mcp_render

MANAGEMENT_MCP_NAME = "molecule-platform"


def _seed_openclaw_config(home, servers: dict) -> None:
    """Write an ~/.openclaw/openclaw.json carrying ``mcp.servers = servers``."""
    cfg = home / ".openclaw" / "openclaw.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"mcp": {"servers": servers}}))


# ── openclaw present-reader: reads the openclaw-native config, fail-closed ──

def test_openclaw_present_does_not_probe_claude_settings(tmp_path, monkeypatch):
    """An openclaw concierge must NOT be judged against ``.claude/settings.json``.

    PROVE-FAIL: pre-P4-stub openclaw fell back to claude_code, whose
    present-reader reads ``<config>/.claude/settings.json``. We seed exactly that
    file declaring the MCP but leave the openclaw-native config empty, and assert
    present is still False — only an openclaw-native present-reader can hold this.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(
        json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "npx"}}})
    )
    # No ~/.openclaw/openclaw.json → present is False despite the claude file.
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_openclaw_present_true_when_registered(tmp_path, monkeypatch):
    """present=True when ``~/.openclaw/openclaw.json`` declares
    ``mcp.servers.molecule-platform`` (the shape ``openclaw mcp set`` writes)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_openclaw_config(
        tmp_path,
        {MANAGEMENT_MCP_NAME: {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]}},
    )
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is True
    )


def test_openclaw_present_false_when_only_other_server(tmp_path, monkeypatch):
    """An UNRELATED server registered (but not molecule-platform) → False."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_openclaw_config(tmp_path, {"some-other": {"command": "uvx"}})
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_openclaw_present_false_on_malformed_config(tmp_path, monkeypatch):
    """Stays fail-closed (False) on a malformed/garbage native config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".openclaw" / "openclaw.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{ not json at all")
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


def test_openclaw_present_false_when_nothing_wired(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.openclaw/openclaw.json
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is False
    )


# ── openclaw renderer: writes openclaw-native config, NOT claude settings.json ──

def test_openclaw_render_writes_native_config_not_claude(tmp_path, monkeypatch):
    """The renderer writes ``~/.openclaw/openclaw.json`` ``mcp.servers.<name>`` —
    NOT ``.claude/settings.json``.

    PROVE-FAIL: the P1 stub raised NotImplementedError (no file). The pre-stub
    code wrote ``.claude/settings.json``. After P4 it writes the openclaw-native
    file with the stdio descriptor and the claude file is never touched.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_settings = tmp_path / ".claude" / "settings.json"

    target = mcp_render.render_for_runtime(
        "openclaw", tmp_path, MANAGEMENT_MCP_NAME,
        {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"],
         "env": {"MOLECULE_MCP_MODE": "management"}},
    )

    assert not claude_settings.exists(), (
        "openclaw render must NOT write .claude/settings.json (the #3159 bug)"
    )
    expected = tmp_path / ".openclaw" / "openclaw.json"
    assert target == expected
    data = json.loads(expected.read_text())
    server = data["mcp"]["servers"][MANAGEMENT_MCP_NAME]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@molecule-ai/mcp-server"]
    assert server["env"] == {"MOLECULE_MCP_MODE": "management"}
    # And the present-reader agrees with what the renderer just wrote.
    assert (
        mcp_render.management_mcp_present_for("openclaw", tmp_path, MANAGEMENT_MCP_NAME)
        is True
    )


def test_openclaw_render_is_additive_and_idempotent(tmp_path, monkeypatch):
    """Re-rendering preserves other servers + other top-level keys (idempotent)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".openclaw" / "openclaw.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "commands": {"native": "auto"},
        "mcp": {"servers": {"keep-me": {"command": "uvx"}}},
    }))

    mcp_render.render_for_runtime(
        "openclaw", tmp_path, MANAGEMENT_MCP_NAME, {"command": "npx"}
    )
    data = json.loads(cfg.read_text())
    # Other top-level key + the pre-existing server both survive.
    assert data["commands"] == {"native": "auto"}
    assert data["mcp"]["servers"]["keep-me"] == {"command": "uvx"}
    assert data["mcp"]["servers"][MANAGEMENT_MCP_NAME] == {"command": "npx"}


def test_openclaw_render_fn_directly_writes(tmp_path):
    """The renderer fn writes the descriptor under mcp.servers at the given path."""
    out = tmp_path / "openclaw.json"
    mcp_render.render_openclaw_config(out, MANAGEMENT_MCP_NAME, {"command": "npx"})
    data = json.loads(out.read_text())
    assert data["mcp"]["servers"][MANAGEMENT_MCP_NAME] == {"command": "npx"}


# ── openclaw is now a VERIFIED (concrete) runtime ──

def test_openclaw_is_supported_runtime():
    """PROVE-FAIL: the P1 stub kept openclaw in _UNVERIFIED_RUNTIMES (unsupported).
    After P4 it has a concrete renderer → supported, and is OUT of the stub set."""
    assert mcp_render.is_runtime_supported("openclaw") is True
    assert "openclaw" not in mcp_render._UNVERIFIED_RUNTIMES


@pytest.mark.parametrize("runtime", ["openclaw", "OpenClaw", "  OPENCLAW "])
def test_openclaw_normalization_variants_are_supported(runtime):
    """Case/whitespace variants normalize to the same concrete entry
    (``normalize_runtime`` lowercases + strips)."""
    assert mcp_render.is_runtime_supported(runtime) is True


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
