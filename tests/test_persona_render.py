"""Persona-materialization PORT tests — the per-runtime render matrix + the
concierge-runtime allowlist completeness guardrail.

Sibling of ``test_mcp_render_completeness_g6`` / the #3159 MCP render-matrix.
Where those pin that each runtime's *tools* land in the file THAT runtime reads,
these pin that each runtime's *identity* (the canonical persona) is materialized
into the file THAT runtime reads for identity — and, critically, that openclaw
does NOT get its persona written to claude's ``system-prompt.md`` (the persona
analogue of the #3159 cross-runtime mis-attribution), and that materialization is
idempotent (no accumulation across reboots).
"""
from __future__ import annotations

import pytest

from molecule_runtime import persona_render

# The kind=platform runtime allowlist (mirrors test_mcp_render_completeness_g6):
# the runtimes a concierge is allowed to run on AND that have a concrete, verified
# native identity convention. hermes is EXCLUDED (fail-loud stub).
PLATFORM_RUNTIME_ALLOWLIST = ("claude-code", "codex", "openclaw", "google-adk")

# runtime -> the basename of the native identity file it must materialize into.
EXPECTED_NATIVE_FILE = {
    "claude-code": "system-prompt.md",
    "openclaw": "SOUL.md",
    "codex": "AGENTS.md",
    "google-adk": "GEMINI.md",
    "gemini": "GEMINI.md",
}

PERSONA = "# You are test7 — the Org Concierge\n\nYou orchestrate; you don't do the work yourself."


# ---------------------------------------------------------------------------
# read_canonical_persona — the runtime-agnostic INPUT.
# ---------------------------------------------------------------------------

def test_read_persona_from_prompt_files_subdir(tmp_path):
    """The persona is read from the delivered ``prompt_files`` — including a
    ``prompts/concierge.md`` under a SUBDIR (the exact #3418 delivery shape that
    openclaw's top-level-only copy silently skipped)."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "concierge.md").write_text(PERSONA, encoding="utf-8")
    got = persona_render.read_canonical_persona(tmp_path, ["prompts/concierge.md"])
    assert "the Org Concierge" in got
    assert "orchestrate" in got


def test_read_persona_falls_back_to_system_prompt_md(tmp_path):
    """No ``prompt_files`` declared -> fall back to system-prompt.md (mirrors
    build_system_prompt's own fallback)."""
    (tmp_path / "system-prompt.md").write_text(PERSONA, encoding="utf-8")
    got = persona_render.read_canonical_persona(tmp_path, [])
    assert "the Org Concierge" in got


def test_read_persona_empty_when_nothing_delivered(tmp_path):
    """Nothing delivered -> empty string, so the caller no-ops instead of
    clobbering a runtime's baked default."""
    assert persona_render.read_canonical_persona(tmp_path, []) == ""


# ---------------------------------------------------------------------------
# Per-runtime materialize matrix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime,expected_file", sorted(EXPECTED_NATIVE_FILE.items()))
def test_materialize_writes_the_runtime_native_file(runtime, expected_file, tmp_path):
    target = persona_render.materialize_persona_for(runtime, tmp_path, PERSONA)
    assert target is not None
    assert target.name == expected_file
    assert target.exists()
    assert "the Org Concierge" in target.read_text(encoding="utf-8")


def test_openclaw_materializes_soul_and_clears_placeholders(tmp_path):
    """openclaw -> SOUL.md carries the persona; BOOTSTRAP.md / AGENTS.md are
    cleared (the validated mechanism)."""
    target = persona_render.materialize_persona_for("openclaw", tmp_path, PERSONA)
    assert target.name == "SOUL.md"
    assert "the Org Concierge" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    for cleared in ("BOOTSTRAP.md", "AGENTS.md"):
        body = (tmp_path / cleared).read_text(encoding="utf-8")
        assert "Cleared by persona materialization" in body
        # The persona/identity itself must NOT live in the cleared files.
        assert "You orchestrate" not in body


def test_openclaw_does_not_write_claude_system_prompt(tmp_path):
    """Cross-runtime isolation (the #3159 analogue): an openclaw persona must NOT
    land in claude's system-prompt.md, and a claude persona must NOT land in
    openclaw's SOUL.md."""
    persona_render.materialize_persona_for("openclaw", tmp_path, PERSONA)
    assert not (tmp_path / "system-prompt.md").exists()

    other = tmp_path / "claude"
    other.mkdir()
    persona_render.materialize_persona_for("claude-code", other, PERSONA)
    assert (other / "system-prompt.md").exists()
    assert not (other / "SOUL.md").exists()


def test_materialize_empty_persona_is_a_noop(tmp_path):
    """Empty/whitespace persona -> None, nothing written (never clobber a baked
    default with an empty identity)."""
    assert persona_render.materialize_persona_for("openclaw", tmp_path, "   \n") is None
    assert not (tmp_path / "SOUL.md").exists()


def test_materialize_is_idempotent(tmp_path):
    """Re-materializing the SAME persona produces byte-identical content — it can
    never accumulate across reboots (the reason the RAW persona, not the assembled
    system prompt, is the materialization input)."""
    t1 = persona_render.materialize_persona_for("openclaw", tmp_path, PERSONA)
    first = t1.read_text(encoding="utf-8")
    t2 = persona_render.materialize_persona_for("openclaw", tmp_path, PERSONA)
    assert t1 == t2
    assert t2.read_text(encoding="utf-8") == first


# ---------------------------------------------------------------------------
# Concierge-runtime allowlist completeness (the G6 analogue).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", PLATFORM_RUNTIME_ALLOWLIST)
def test_allowlisted_runtime_has_concrete_materializer(runtime):
    assert persona_render.is_persona_supported(runtime) is True
    assert persona_render.normalize_runtime(runtime) not in persona_render._UNVERIFIED_RUNTIMES


@pytest.mark.parametrize("runtime", PLATFORM_RUNTIME_ALLOWLIST)
def test_allowlisted_runtime_native_path_is_runtime_specific(runtime, tmp_path):
    path = str(persona_render.persona_path_for(runtime, tmp_path))
    assert path.endswith("/" + EXPECTED_NATIVE_FILE[runtime]) or path.endswith(
        "\\" + EXPECTED_NATIVE_FILE[runtime]
    )
    if persona_render.normalize_runtime(runtime) != "claude_code":
        assert not path.endswith("system-prompt.md")


def test_hermes_persona_writes_soul_md(tmp_path, monkeypatch):
    """hermes graduated OUT of the fail-loud stubs: its identity convention is now
    pinned to hermes-agent's Layer-1 ``~/.hermes/SOUL.md`` (verified live). The
    materializer writes the persona there, resolving HERMES_HOME (config_path is
    unused — the identity file is HOME-based)."""
    assert persona_render.is_persona_supported("hermes") is True
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    target = persona_render.materialize_hermes_persona(tmp_path / "configs", PERSONA)
    assert target == hermes_home / "SOUL.md"
    assert target.read_text().strip().startswith(PERSONA.strip()[:20])
    # the dispatch path-resolver agrees with the materializer's target
    assert str(persona_render._spec_for("hermes")[0](tmp_path)).endswith("/.hermes/SOUL.md")
