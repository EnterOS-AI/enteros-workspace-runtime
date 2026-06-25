"""GUARDRAIL SELF-TEST (runtime side) — proves each guardrail goes RED on its bug.

Task #80. "How do we test the guardrails actually work?" — by injecting each
guardrail's KNOWN regression into a throwaway fixture/patched module and asserting
the guardrail's own check FAILS, then letting monkeypatch auto-revert. A guardrail
that can't fail isn't a guardrail; this file is the meta-proof for the runtime
half (G0 fallback, G1 channel, G2 base-frame, G5 openclaw fail-closed, G6
renderer-completeness). The core half is proved by the Go meta-test
TestGuardrailSelfTest_* in molecule-core.

Each case is structured: (1) confirm the guardrail PASSES on pristine code, then
(2) inject the regression and confirm it RAISES (RED). monkeypatch reverts the
injection at teardown — no source is permanently mutated.
"""
from __future__ import annotations

import pytest

from molecule_runtime import mcp_render, prompt
from molecule_runtime.prompt import build_system_prompt


def _write(base, rel, text):
    f = base / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


# ── G0: filename SSOT fallback = system-prompt.md ──────────────────────────────

def test_selftest_g0_fallback_filename(tmp_path, monkeypatch):
    from tests.test_prompt_filename_ssot_g0 import (
        CANONICAL_PROMPT_FILENAME,
        test_runtime_fallback_filename_is_canonical,
    )

    # (1) PASSES on pristine code.
    test_runtime_fallback_filename_is_canonical(tmp_path)

    # (2) REGRESSION: re-point the build's fallback at a WRONG filename, exactly
    # the filename-split bug. Re-run the SAME guardrail body → it must go RED.
    real_build = prompt.build_system_prompt

    def _split_build(config_path, *a, prompt_files=None, **k):
        # Simulate someone changing the fallback to a different filename.
        if not prompt_files:
            prompt_files = ["concierge.md"]  # the WRONG (split) filename
        return real_build(config_path, *a, prompt_files=prompt_files, **k)

    monkeypatch.setattr("molecule_runtime.prompt.build_system_prompt", _split_build)
    # The guardrail imports build_system_prompt by name; patch that binding too.
    import tests.test_prompt_filename_ssot_g0 as g0mod
    monkeypatch.setattr(g0mod, "build_system_prompt", _split_build)

    fresh = tmp_path / "regress"
    with pytest.raises(AssertionError):
        g0mod.test_runtime_fallback_filename_is_canonical(fresh)


# ── G1: single prompt-delivery channel = config.system_prompt ──────────────────

def test_selftest_g1_channel_vs_file_reread(tmp_path):
    from molecule_runtime.adapter_base import AdapterConfig
    from molecule_runtime.executor_helpers import get_system_prompt

    marker_a = "G1-CHANNEL-A"
    marker_b = "G1-FILE-B"
    _write(tmp_path, "system-prompt.md", marker_b)
    config = AdapterConfig(model="m", config_path=str(tmp_path), workspace_id="ws")
    config.system_prompt = marker_a

    # The guardrail's invariant: an executor reads the CHANNEL (== A).
    assert config.system_prompt == marker_a

    # REGRESSION: an executor wired to re-read the file (the per-runtime vector)
    # gets a DIFFERENT prompt (== B). Asserting equality to the channel — the
    # property the guardrail demands of every executor — now FAILS (RED).
    regressed_executor_prompt = get_system_prompt(config.config_path)
    with pytest.raises(AssertionError):
        assert regressed_executor_prompt == config.system_prompt, (
            "an executor that re-reads system-prompt.md diverges from the channel"
        )


# ── G2: base platform frame ALWAYS present + first ─────────────────────────────

def test_selftest_g2_base_frame_always_present(tmp_path, monkeypatch):
    # (1) PASSES: base frame present with no template at all.
    out = build_system_prompt(str(tmp_path), "ws", [], [], prompt_files=None, a2a_mcp=False)
    assert "Molecule AI platform" in out

    # (2) REGRESSION: empty out the BASE_PLATFORM_PROMPT (someone "simplifies" the
    # base frame away). The base-frame invariant now FAILS (RED).
    monkeypatch.setattr(prompt, "BASE_PLATFORM_PROMPT", "")
    regressed = prompt.build_system_prompt(
        str(tmp_path), "ws", [], [], prompt_files=None, a2a_mcp=False
    )
    with pytest.raises(AssertionError):
        assert "Molecule AI platform" in regressed


# ── G5: openclaw renderer fail-closed (native config, not claude fallback) ─────

def test_selftest_g5_openclaw_not_claude_fallback(tmp_path, monkeypatch):
    from tests.test_mcp_render_openclaw_failclosed import (
        MANAGEMENT_MCP_NAME,
        test_openclaw_present_does_not_probe_claude_settings,
    )

    # (1) PASSES on pristine code.
    test_openclaw_present_does_not_probe_claude_settings(tmp_path, monkeypatch)

    # (2) REGRESSION: drop openclaw's concrete entry so it falls through to the
    # claude_code default (the #3159 mis-attribution). The present-reader would
    # then read .claude/settings.json — and the guardrail's assertion that
    # present stays False despite the claude file now FAILS (RED).
    regressed_specs = dict(mcp_render._RUNTIME_SPECS)
    regressed_specs.pop("openclaw", None)  # force the claude fallback
    monkeypatch.setattr(mcp_render, "_RUNTIME_SPECS", regressed_specs)

    import json

    fresh = tmp_path / "g5regress"
    fresh.mkdir()
    monkeypatch.setenv("HOME", str(fresh))
    claude_settings = fresh / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(
        json.dumps({"mcpServers": {MANAGEMENT_MCP_NAME: {"command": "npx"}}})
    )
    with pytest.raises(AssertionError):
        # Regressed openclaw now resolves to claude's present-reader → True
        # against the claude file → the fail-closed contract is violated.
        assert (
            mcp_render.management_mcp_present_for("openclaw", fresh, MANAGEMENT_MCP_NAME)
            is False
        )


# ── G6: renderer-completeness for the kind=platform allowlist ──────────────────

def test_selftest_g6_renderer_completeness(monkeypatch):
    from tests.test_mcp_render_completeness_g6 import (
        test_platform_runtime_has_concrete_render_spec,
    )

    # (1) PASSES: codex has a concrete entry.
    test_platform_runtime_has_concrete_render_spec("codex")

    # (2) REGRESSION: remove codex's concrete entry so it would silently fall
    # through to _DEFAULT_RUNTIME. The completeness guardrail now FAILS (RED).
    regressed_specs = dict(mcp_render._RUNTIME_SPECS)
    regressed_specs.pop("codex", None)
    monkeypatch.setattr(mcp_render, "_RUNTIME_SPECS", regressed_specs)
    with pytest.raises(AssertionError):
        test_platform_runtime_has_concrete_render_spec("codex")
