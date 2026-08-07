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

from molecule_runtime import prompt
from molecule_runtime.branding import product_display_name
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

# Sentence unique to BASE_PLATFORM_PROMPT. Deliberately product-NAME-free: the
# product display name now comes from the vendored branding SSOT
# (molecule_runtime/branding.py), so asserting a literal name here would just be
# the stale-literal defect wearing a test's clothes. It also must NOT be read
# back off the module, or the monkeypatch-to-"" regression leg below would go
# vacuous ("" is in every string).
BASE_FRAME_MARKER = "You are an AI agent running as a *workspace* inside an organization on"


def test_selftest_g2_base_frame_always_present(tmp_path, monkeypatch):
    # (1) PASSES: base frame present with no template at all, and it names the
    # product from the branding SSOT.
    out = build_system_prompt(str(tmp_path), "ws", [], [], prompt_files=None, a2a_mcp=False)
    assert BASE_FRAME_MARKER in out
    assert product_display_name() in out

    # (2) REGRESSION: empty out the BASE_PLATFORM_PROMPT (someone "simplifies" the
    # base frame away). The base-frame invariant now FAILS (RED).
    monkeypatch.setattr(prompt, "BASE_PLATFORM_PROMPT", "")
    regressed = prompt.build_system_prompt(
        str(tmp_path), "ws", [], [], prompt_files=None, a2a_mcp=False
    )
    with pytest.raises(AssertionError):
        assert BASE_FRAME_MARKER in regressed


# ── G5 / G6 (ADR-004): SUPERSEDED — the engine-side per-runtime guardrails are
# gone with the engine dispatch tables they policed.
# ------------------------------------------------------------------------------
# The old G5 (openclaw renderer must not fall back to claude) and G6
# (renderer-completeness for the kind=platform allowlist) self-tests monkeypatched
# ``mcp_render._RUNTIME_SPECS`` to prove the guardrail went RED when a runtime lost
# its concrete engine entry. ADR-004 DELETED ``_RUNTIME_SPECS`` and moved the
# per-runtime render/read/present INTO each adapter, so there is no engine entry to
# drop and no by-name ``management_mcp_present_for`` fallback to mis-attribute.
#
# The invariant those guards protected is now enforced in two places:
#   * PER-ADAPTER: the SDK conformance suite (``molecule_plugin.adapter_conformance``)
#     asserts each adapter's own render→read→present round-trips on ITS native
#     config and FAILS CLOSED when unmapped (an unmapped adapter never false-greens
#     against claude's settings.json — the #3159 guard, now ``test_unmapped_runtime_*``
#     in the SDK suite, run by every template's ``tests/test_conformance.py``).
#   * DRIFT-DOWN RATCHET: ``tests/test_engine_no_runtime_dispatch_ratchet.py`` fails
#     any change that re-introduces a ``_RUNTIME_*`` table or a runtime-name literal
#     into ``mcp_render`` / ``persona_render`` (the drift can only shrink → 0).
#
# G0/G1/G2 above remain here (they police the prompt SSOT, not the engine's
# per-runtime dispatch, so they are unaffected by ADR-004).
