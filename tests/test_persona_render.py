"""Generic persona-engine helpers (ADR-004) — the runtime-name-free surface the
shared engine KEEPS after the per-runtime materializers moved into the adapters.

ADR-004 (`docs/adr/ADR-004-sdk-owns-adapter-contract-and-registry.md`) deleted the
engine's per-runtime persona dispatch (``persona_render._RUNTIME_PERSONA`` /
``materialize_persona_for`` / ``persona_path_for`` / ``is_persona_supported`` and
every ``materialize_<runtime>_persona`` / ``_<runtime>_persona_path``). The
per-runtime native identity-file materialization (openclaw → SOUL.md + cleared
placeholders, codex → AGENTS.md, hermes → ~/.hermes/SOUL.md, claude-code →
system-prompt.md) now lives IN each adapter's template repo and is proven by the
SDK conformance suite's persona check (``molecule_plugin.adapter_conformance`` —
``test_executor_or_persona_carries_system_prompt``, run by each template's
``tests/test_conformance.py``).

What the ENGINE keeps — and this file covers — is the generic, runtime-name-free
surface every adapter (official or third-party) reuses:
  * :func:`read_canonical_persona` — the runtime-agnostic INPUT to every
    materializer (read the delivered ``prompt_files``, fall back to
    ``system-prompt.md``, join).
  * :func:`write_persona` — the byte-shape writer (parents created, trailing
    newline only when absent) so a materialized identity file is byte-identical
    regardless of who wrote it.
  * :func:`default_persona_path` + the BaseAdapter default (``adapter_base``) that
    uses it — the name-agnostic fallback for a not-yet-migrated / third-party
    adapter.
"""
from __future__ import annotations

from pathlib import Path

from molecule_runtime import persona_render

PERSONA = "# You are test7 — the Org Concierge\n\nYou orchestrate; you don't do the work yourself."


# ---------------------------------------------------------------------------
# read_canonical_persona — the runtime-agnostic INPUT (unchanged by ADR-004).
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


def test_read_persona_joins_multiple_prompt_files_in_order(tmp_path):
    """Multiple declared ``prompt_files`` are joined (ordered), mirroring how
    build_system_prompt sources the role."""
    (tmp_path / "a.md").write_text("# ALPHA", encoding="utf-8")
    (tmp_path / "b.md").write_text("# BETA", encoding="utf-8")
    got = persona_render.read_canonical_persona(tmp_path, ["a.md", "b.md"])
    assert got == "# ALPHA\n\n# BETA"


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


def test_read_persona_skips_missing_files(tmp_path):
    """A declared-but-missing prompt file is skipped (fail-soft), not fatal."""
    (tmp_path / "present.md").write_text("# HERE", encoding="utf-8")
    got = persona_render.read_canonical_persona(
        tmp_path, ["missing.md", "present.md"]
    )
    assert got == "# HERE"


# ---------------------------------------------------------------------------
# write_persona — the generic byte-shape writer (shared by every native writer).
# ---------------------------------------------------------------------------

def test_write_persona_creates_parents_and_appends_trailing_newline(tmp_path):
    target = tmp_path / "nested" / "dir" / "SOUL.md"
    persona_render.write_persona(target, "hello identity")
    assert target.exists()
    # trailing newline appended when absent
    assert target.read_text(encoding="utf-8") == "hello identity\n"


def test_write_persona_does_not_double_trailing_newline(tmp_path):
    target = tmp_path / "id.md"
    persona_render.write_persona(target, "already newline\n")
    assert target.read_text(encoding="utf-8") == "already newline\n"


def test_write_persona_is_idempotent(tmp_path):
    """Re-writing the SAME persona produces byte-identical content (no
    accumulation across reboots — the reason the RAW persona is the input)."""
    target = tmp_path / "id.md"
    persona_render.write_persona(target, PERSONA)
    first = target.read_bytes()
    persona_render.write_persona(target, PERSONA)
    assert target.read_bytes() == first


# ---------------------------------------------------------------------------
# default_persona_path + the BaseAdapter fallback (name-agnostic, no dispatch).
# ---------------------------------------------------------------------------

def test_default_persona_path_is_system_prompt_md(tmp_path):
    p = persona_render.default_persona_path(tmp_path)
    assert p == tmp_path / "system-prompt.md"


def test_base_adapter_materialize_persona_writes_default_file(tmp_path):
    """The BaseAdapter fallback (a not-yet-migrated / third-party adapter that does
    NOT override the persona seam) reads the canonical persona and writes it to the
    default identity file ``<config_path>/system-prompt.md`` — name-agnostic, no
    self.name() dispatch (ADR-004)."""
    from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter

    class _BareAdapter(BaseAdapter):
        @staticmethod
        def name():
            return "some-third-party-runtime"

        @staticmethod
        def display_name():
            return "Bare"

        @staticmethod
        def description():
            return "no persona override"

        async def setup(self, config):  # pragma: no cover
            return None

        async def create_executor(self, config):  # pragma: no cover
            return None

    (tmp_path / "system-prompt.md").write_text(PERSONA, encoding="utf-8")
    cfg = AdapterConfig(model="m", config_path=str(tmp_path), workspace_id="ws")
    written = _BareAdapter().materialize_persona(cfg)
    assert written == Path(tmp_path) / "system-prompt.md"
    assert "the Org Concierge" in written.read_text(encoding="utf-8")


def test_base_adapter_materialize_persona_noop_when_nothing_delivered(tmp_path):
    """No persona delivered -> the fallback returns None and writes nothing (never
    clobber a runtime's baked default with an empty identity)."""
    from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter

    class _BareAdapter(BaseAdapter):
        @staticmethod
        def name():
            return "some-third-party-runtime"

        @staticmethod
        def display_name():
            return "Bare"

        @staticmethod
        def description():
            return "no persona override"

        async def setup(self, config):  # pragma: no cover
            return None

        async def create_executor(self, config):  # pragma: no cover
            return None

    cfg = AdapterConfig(model="m", config_path=str(tmp_path), workspace_id="ws")
    assert _BareAdapter().materialize_persona(cfg) is None
    assert not (tmp_path / "system-prompt.md").exists()
