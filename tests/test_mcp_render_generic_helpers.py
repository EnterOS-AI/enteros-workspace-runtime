"""Generic mcp_render helpers (ADR-004) — the runtime-name-free surface the shared
engine KEEPS after the per-runtime renderers/readers/present-probes moved into the
adapters.

ADR-004 (`docs/adr/ADR-004-sdk-owns-adapter-contract-and-registry.md`) deleted the
engine's per-runtime MCP dispatch (``mcp_render._RUNTIME_SPECS`` /
``_RUNTIME_READERS`` / ``render_for_runtime`` / ``read_mcp_servers_for`` /
``management_mcp_present_for`` / ``mcp_settings_path_for`` and every
``render_<runtime>_config`` / ``_<runtime>_config_has`` / ``_read_<runtime>_mcp_servers``).
Each runtime's native render/read/present now lives IN its adapter's template repo
and is proven by the SDK conformance suite's render→read→present round-trip
(``molecule_plugin.adapter_conformance``, run by every template's
``tests/test_conformance.py``).

What the ENGINE keeps — and this file covers — is the generic, runtime-name-free
surface every adapter (official or third-party) and the BaseAdapter default reuse:
``normalize_runtime`` and the generic JSON ``mcpServers`` render / read / present
triple (+ the default JSON path). The by-name switch is GONE; the drift-DOWN
ratchet (``test_engine_no_runtime_dispatch_ratchet.py``) keeps it from coming back.
"""
from __future__ import annotations

import json

import pytest

from molecule_runtime import mcp_render

MANAGEMENT_MCP_NAME = "molecule-platform"


# ── normalize_runtime — the pure -/_ canonicalization (no runtime knowledge) ──

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude-code", "claude_code"),
        ("Claude-Code", "claude_code"),
        ("  HERMES ", "hermes"),
        ("openclaw", "openclaw"),
        ("", ""),
    ],
)
def test_normalize_runtime(raw, expected):
    assert mcp_render.normalize_runtime(raw) == expected


# ── generic JSON mcpServers render/read/present — the BaseAdapter default ──────

def test_render_json_writes_mcpservers_map(tmp_path):
    target = tmp_path / "settings.json"
    mcp_render.render_json_mcp_servers(
        target, MANAGEMENT_MCP_NAME,
        {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"],
         "env": {"MOLECULE_MCP_MODE": "management"}},
    )
    data = json.loads(target.read_text())
    server = data["mcpServers"][MANAGEMENT_MCP_NAME]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@molecule-ai/mcp-server"]
    assert server["env"] == {"MOLECULE_MCP_MODE": "management"}


def test_render_json_is_byte_stable_and_idempotent(tmp_path):
    target = tmp_path / "settings.json"
    spec = {"command": "npx", "args": ["x"]}
    mcp_render.render_json_mcp_servers(target, MANAGEMENT_MCP_NAME, spec)
    first = target.read_bytes()
    mcp_render.render_json_mcp_servers(target, MANAGEMENT_MCP_NAME, dict(spec))
    assert target.read_bytes() == first
    # explicit byte-shape: json.dumps(indent=2) + trailing newline
    assert first.endswith(b"\n")


def test_render_json_is_additive(tmp_path):
    target = tmp_path / "settings.json"
    mcp_render.render_json_mcp_servers(target, "keep-me", {"command": "uvx"})
    mcp_render.render_json_mcp_servers(target, MANAGEMENT_MCP_NAME, {"command": "npx"})
    data = json.loads(target.read_text())
    assert data["mcpServers"]["keep-me"] == {"command": "uvx"}
    assert data["mcpServers"][MANAGEMENT_MCP_NAME] == {"command": "npx"}


def test_json_present_true_when_declared(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "npx"}}}))
    assert mcp_render.json_mcp_servers_has(target, MANAGEMENT_MCP_NAME) is True


def test_json_present_fail_closed_on_absent_or_malformed(tmp_path):
    # Missing file → False.
    assert mcp_render.json_mcp_servers_has(tmp_path / "nope.json", MANAGEMENT_MCP_NAME) is False
    # Malformed JSON → False (fail-closed).
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    assert mcp_render.json_mcp_servers_has(bad, MANAGEMENT_MCP_NAME) is False
    # Structurally-unexpected (mcpServers not a dict) → False.
    weird = tmp_path / "weird.json"
    weird.write_text(json.dumps({"mcpServers": ["not", "a", "dict"]}))
    assert mcp_render.json_mcp_servers_has(weird, MANAGEMENT_MCP_NAME) is False


def test_read_json_returns_dict_valued_entries(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"mcpServers": {
        MANAGEMENT_MCP_NAME: {"command": "npx", "args": ["x"]},
        "junk": "not-a-dict",
    }}))
    got = mcp_render.read_json_mcp_servers(target)
    assert got == {MANAGEMENT_MCP_NAME: {"command": "npx", "args": ["x"]}}


def test_read_json_fail_closed_empty(tmp_path):
    # Missing / malformed → {} (never crashes the enumerate path).
    assert mcp_render.read_json_mcp_servers(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    assert mcp_render.read_json_mcp_servers(bad) == {}


def test_render_then_read_then_present_roundtrip(tmp_path):
    """The generic triple round-trips on one file (the BaseAdapter default's
    render→read→present lockstep — ADR-004's replacement for the deleted engine
    ``_RUNTIME_SPECS``-keyed round-trip)."""
    target = tmp_path / "settings.json"
    assert mcp_render.json_mcp_servers_has(target, MANAGEMENT_MCP_NAME) is False
    mcp_render.render_json_mcp_servers(target, MANAGEMENT_MCP_NAME, {"command": "npx", "args": ["x"]})
    assert mcp_render.json_mcp_servers_has(target, MANAGEMENT_MCP_NAME) is True
    assert mcp_render.read_json_mcp_servers(target) == {MANAGEMENT_MCP_NAME: {"command": "npx", "args": ["x"]}}


def test_default_json_settings_path(tmp_path):
    p = mcp_render.default_json_settings_path(tmp_path)
    assert str(p).endswith("/.claude/settings.json")


# ── main.py: probe-wiring failure is FATAL for a platform agent (rescued from the
# deleted test_mcp_render_openclaw_failclosed.py — unrelated to the engine gut,
# guards a live main.py boot decision). ────────────────────────────────────────

PLATFORM_AGENT_IMAGE_ENV = "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED"


def test_probe_failure_fatal_on_platform_agent(monkeypatch):
    """On a platform agent (baked-image marker set), a probe-wiring failure is
    FATAL — main.py aborts the boot rather than fail-open to the claude fallback."""
    from molecule_runtime.main import _probe_wiring_failure_is_fatal

    monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
    assert _probe_wiring_failure_is_fatal() is True


def test_probe_failure_not_fatal_on_ordinary_workspace(monkeypatch):
    """An ordinary workspace doesn't gate on the management MCP — a probe-wiring
    hiccup must not abort its boot."""
    from molecule_runtime.main import _probe_wiring_failure_is_fatal

    monkeypatch.delenv(PLATFORM_AGENT_IMAGE_ENV, raising=False)
    assert _probe_wiring_failure_is_fatal() is False


def test_render_json_write_is_atomic_no_temp_leftover(tmp_path):
    """review wf_3a7b849d #8: the config write goes through a temp+rename so a
    mid-write SIGKILL can't leave a torn config.yaml. After a normal write the
    target holds valid JSON and NO sibling .tmp.* file is left behind."""
    settings = tmp_path / "config.json"
    mcp_render.render_json_mcp_servers(settings, "svc", {"command": "sh"})
    # target parses as JSON (not torn) ...
    data = json.loads(settings.read_text())
    assert data["mcpServers"]["svc"] == {"command": "sh"}
    # ... and the atomic temp was renamed away, not orphaned.
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"atomic-write temp file(s) left behind: {leftovers}"
    # A second write (rename over an existing target) still leaves none.
    mcp_render.render_json_mcp_servers(settings, "svc2", {"url": "http://x/mcp"})
    assert [p.name for p in tmp_path.iterdir() if ".tmp." in p.name] == []
    assert set(json.loads(settings.read_text())["mcpServers"]) == {"svc", "svc2"}
