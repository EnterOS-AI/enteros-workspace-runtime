"""Guardrail: the system prompt's SINGLE SOURCE (build_system_prompt) honors
config.yaml `prompt_files`, with `system-prompt.md` as the legacy fallback.

This locks in the SSOT that broke the org concierge: prompt-file resolution
lives in ONE place (build_system_prompt) and honors the declared `prompt_files`,
so NO runtime executor needs to — or should — re-read /configs/system-prompt.md
itself. _common_setup publishes this single build onto AdapterConfig.system_prompt
(adapter_base.py), the one delivery channel every executor consumes.

If anyone reintroduces a second prompt-file source (e.g. an executor that reads
only system-prompt.md and ignores prompt_files), these assertions stay green here
but the cross-runtime contract test catches it — this file is the base-source
half of that guardrail.
"""
from __future__ import annotations

from molecule_runtime.branding import product_display_name
from molecule_runtime.prompt import build_system_prompt


def _write(base, rel, text):
    f = base / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def test_honors_prompt_files_over_system_prompt_md(tmp_path):
    # The concierge layout: identity at prompts/concierge.md, declared via
    # prompt_files. A stale root system-prompt.md must NOT shadow it, and the
    # declared file MUST be loaded (the bug was prompt_files being ignored).
    _write(tmp_path, "prompts/concierge.md", "ORG-ORCHESTRATOR-IDENTITY")
    _write(tmp_path, "system-prompt.md", "GENERIC-FALLBACK")
    out = build_system_prompt(
        str(tmp_path), "ws-1", [], [], prompt_files=["prompts/concierge.md"], a2a_mcp=False
    )
    assert "ORG-ORCHESTRATOR-IDENTITY" in out
    # prompt_files is non-empty → the legacy single file is NOT also loaded.
    assert "GENERIC-FALLBACK" not in out


def test_loads_multiple_prompt_files_in_declared_order(tmp_path):
    _write(tmp_path, "prompts/a.md", "PROMPT-A-MARK")
    _write(tmp_path, "prompts/b.md", "PROMPT-B-MARK")
    out = build_system_prompt(
        str(tmp_path), "ws-1", [], [], prompt_files=["prompts/a.md", "prompts/b.md"], a2a_mcp=False
    )
    assert "PROMPT-A-MARK" in out and "PROMPT-B-MARK" in out
    assert out.index("PROMPT-A-MARK") < out.index("PROMPT-B-MARK")


def test_falls_back_to_system_prompt_md_when_no_prompt_files(tmp_path):
    # Legacy templates ship only system-prompt.md and declare no prompt_files;
    # the single source keeps loading it so existing workspaces don't regress.
    _write(tmp_path, "system-prompt.md", "LEGACY-SINGLE-FILE")
    for pf in (None, []):
        out = build_system_prompt(str(tmp_path), "ws-1", [], [], prompt_files=pf, a2a_mcp=False)
        assert "LEGACY-SINGLE-FILE" in out, f"fallback failed for prompt_files={pf!r}"


# Sentence unique to BASE_PLATFORM_PROMPT, carrying no product name — the name
# itself comes from the vendored branding SSOT and is asserted separately, so a
# rename can never make this guard stale (or, worse, quietly wrong).
BASE_FRAME_MARKER = "You are an AI agent running as a *workspace* inside an organization on"


def test_base_platform_identity_always_present(tmp_path):
    # EVERY workspace carries the shared platform frame, even with no template
    # and no prompt files — the role layers on top of it, never replaces it. This
    # is the always-on base prompt, single-sourced in the base builder so all
    # agents present a consistent platform identity, and it names the product
    # from the branding SSOT (molecule_runtime/branding.py) rather than a literal.
    out = build_system_prompt(str(tmp_path), "ws-1", [], [], prompt_files=None, a2a_mcp=False)
    assert BASE_FRAME_MARKER in out
    assert product_display_name() in out
    # Still present when a role prompt IS supplied, and layered before it.
    _write(tmp_path, "prompts/concierge.md", "ROLE-IDENTITY-MARK")
    out2 = build_system_prompt(
        str(tmp_path), "ws-1", [], [], prompt_files=["prompts/concierge.md"], a2a_mcp=False
    )
    assert BASE_FRAME_MARKER in out2 and "ROLE-IDENTITY-MARK" in out2
    assert product_display_name() in out2
    assert out2.index(BASE_FRAME_MARKER) < out2.index("ROLE-IDENTITY-MARK")
