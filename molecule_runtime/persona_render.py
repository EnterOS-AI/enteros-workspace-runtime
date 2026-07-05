"""Per-runtime materializers for the workspace's CANONICAL PERSONA.

This module is the *persona-materialization PORT* — the sibling of
:mod:`molecule_runtime.mcp_render` (which renders an MCP-server descriptor into
each runtime's native MCP-config file). Where ``mcp_render`` gives a runtime its
*tools*, this module gives a runtime its *identity*: it renders the
runtime-agnostic **canonical persona** — the workspace's intended identity,
delivered as the ``prompt_files`` content (e.g. a concierge's
``prompts/concierge.md``) — into each runtime's OWN native identity file, so the
model ACTUALLY BOOTS with that identity regardless of whether the runtime
consumes the base-assembled ``config.system_prompt``.

Why a materializer is needed
----------------------------
Claude Code's executor consumes the base-assembled ``config.system_prompt``
directly, so its persona already reaches the model. But other runtimes read
identity from a NATIVE on-disk file their own gateway/CLI loads and IGNORE
``config.system_prompt`` entirely. Each runtime has a different native identity
convention:

  * Claude Code → ``<configs>/system-prompt.md`` — the file its adapter reads
                  as the system-prompt fallback.
  * OpenClaw    → ``<configs>/SOUL.md`` — copied into the gateway workspace at
                  setup; the ``BOOTSTRAP.md`` / ``AGENTS.md`` placeholders are
                  cleared so the baked generic-identity boilerplate can't compete
                  with the materialized identity.
  * Codex       → ``<configs>/AGENTS.md`` — the AAIF / AGENTS.md convention codex
                  reads from its project directory.
  * Gemini / google-adk → ``<configs>/GEMINI.md`` — the Gemini context-file
                  convention (``gemini`` / ``google-adk`` map here).
  * Hermes      → native convention unverified — deliberate fail-loud stub.

The bug this closes
-------------------
core #3418 (the *provision* half) made the concierge's ``/configs`` runtime-native
and delivered the persona to ``/configs/prompts/concierge.md`` for every
non-claude-code runtime. But that never reached an OpenClaw concierge's model:
OpenClaw's setup copies only TOP-LEVEL ``/configs/*.md`` into its gateway
workspace (so a persona under ``prompts/`` is skipped) AND its executor never
reads ``config.system_prompt`` — so a concierge on the DEFAULT openclaw runtime
booted with the baked placeholder SOUL.md and no concierge identity. This module
is the *runtime* half #3418 was missing: at boot the active adapter reads the
canonical persona and materializes it into ITS native identity file (SOUL.md for
openclaw, cleared placeholders included), so ``prompts/concierge.md`` becomes the
model's actual on-disk identity.

Design
------
NO runtime is the reference / special-case: every runtime declares its own
``(path_resolver, materializer)`` pair in ``_RUNTIME_PERSONA`` and the boot path
dispatches on ``adapter.name()`` — exactly like ``mcp_render._RUNTIME_SPECS``.
The materializers are PURE filesystem renderers (take a configs dir + persona
string, write the native file), idempotent (last-write-wins with identical
content), and testable without any runtime binary. Adding a new runtime is one
dict entry + one small writer. An unverified runtime's materializer raises
``NotImplementedError`` (fail-loud), which the boot-path caller downgrades to a
non-fatal warning — a persona is not a privileged capability like the management
MCP, so a missing native convention must not brick the boot.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# SSOT for the underscore dispatch-key canonicalization — shared with mcp_render
# so ``claude-code`` -> ``claude_code`` normalizes identically in both ports.
from molecule_runtime.mcp_render import normalize_runtime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Native identity-file names, per runtime convention.
# ---------------------------------------------------------------------------
# Claude Code's system-prompt fallback file (the file its adapter's
# create_executor reads when config.system_prompt is unset). Tied to the
# claude-code adapter: ``os.path.join(config.config_path, "system-prompt.md")``.
CLAUDE_PERSONA_FILE = "system-prompt.md"

# OpenClaw reads identity from SOUL.md in its gateway workspace (populated by
# copying top-level ``/configs/*.md`` at setup). BOOTSTRAP.md / AGENTS.md are the
# baked generic placeholders that dilute a strong SOUL identity — cleared below.
OPENCLAW_PERSONA_FILE = "SOUL.md"
OPENCLAW_CLEARED_FILES = ("BOOTSTRAP.md", "AGENTS.md")

# Codex reads the AAIF-standard AGENTS.md from its project directory.
CODEX_PERSONA_FILE = "AGENTS.md"

# Gemini / google-adk read GEMINI.md as their durable context / identity file.
GEMINI_PERSONA_FILE = "GEMINI.md"

# Fallback persona source when a workspace declares no ``prompt_files`` — the same
# file build_system_prompt() falls back to. Keeps read_canonical_persona a
# no-op-preserving mirror of the prompt builder's own fallback.
_DEFAULT_PERSONA_SOURCE = "system-prompt.md"


# ---------------------------------------------------------------------------
# Canonical persona reader — the runtime-agnostic INPUT to every materializer.
# ---------------------------------------------------------------------------

def read_canonical_persona(config_path: str | os.PathLike, prompt_files) -> str:
    """Read the workspace's canonical persona from its delivered ``prompt_files``.

    The canonical persona is the workspace's runtime-agnostic *intended identity*
    — the delivered role content (a concierge's ``prompts/concierge.md``; a
    member's role prompt). This mirrors how ``prompt.build_system_prompt`` sources
    the role: the ordered ``prompt_files`` (relative to ``config_path``), joined;
    falling back to ``system-prompt.md`` when no ``prompt_files`` are declared.

    We deliberately return the RAW delivered role content (NOT the fully-assembled
    ``config.system_prompt``): it is stable across boots (so materializing is
    idempotent and can never accumulate the base frame / guardrail across
    restarts), and it is exactly what every runtime's native identity file should
    hold — the delivered persona, wrapped by each runtime's own loader.

    Returns the joined persona text (``""`` when nothing is delivered, so the
    caller can no-op rather than clobber a runtime's baked default).
    """
    base = Path(config_path)
    names = [str(n) for n in (prompt_files or []) if str(n).strip()]
    if not names:
        names = [_DEFAULT_PERSONA_SOURCE]

    parts: list[str] = []
    for name in names:
        fpath = base / name
        try:
            text = fpath.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Per-runtime materializers — PURE filesystem renderers.
# ---------------------------------------------------------------------------

def _write_persona_file(target: Path, persona: str) -> None:
    """Write ``persona`` to ``target`` (parents created), trailing newline."""
    target.parent.mkdir(parents=True, exist_ok=True)
    body = persona if persona.endswith("\n") else persona + "\n"
    target.write_text(body, encoding="utf-8")


def materialize_claude_persona(config_path: Path, persona: str) -> Path:
    """Claude Code — write the persona to ``<configs>/system-prompt.md``.

    This is the file the claude-code adapter reads as its system-prompt fallback.
    The claude-code executor prefers the base-assembled ``config.system_prompt``,
    so this write is a no-regression native mirror: it guarantees the persona is
    present in claude-code's own convention even if the assembled prompt is ever
    empty, and keeps claude-code non-special (it declares a native file like
    every other runtime)."""
    target = Path(config_path) / CLAUDE_PERSONA_FILE
    _write_persona_file(target, persona)
    return target


def materialize_openclaw_persona(config_path: Path, persona: str) -> Path:
    """OpenClaw — write the persona to ``<configs>/SOUL.md`` and CLEAR the
    ``BOOTSTRAP.md`` / ``AGENTS.md`` placeholders.

    OpenClaw's gateway reads identity from SOUL.md in its workspace, populated by
    copying top-level ``/configs/*.md`` at setup — so writing ``/configs/SOUL.md``
    (top-level) makes the canonical persona the model's actual identity, overlaying
    the baked placeholder SOUL.md. The baked BOOTSTRAP.md ("read SOUL.md for your
    identity …") and generic AGENTS.md are cleared (overwritten with a one-line
    pointer) so their placeholder boilerplate can't compete with the strong
    materialized identity. This is the mechanism validated live (concierge
    self-identified as the Org Concierge and kept orchestrating)."""
    target = Path(config_path) / OPENCLAW_PERSONA_FILE
    _write_persona_file(target, persona)
    for cleared in OPENCLAW_CLEARED_FILES:
        stub = Path(config_path) / cleared
        # Overwrite the baked generic placeholder with a neutral one-liner so the
        # gateway loads no competing identity/boilerplate (identity is SOUL.md).
        _write_persona_file(
            stub,
            f"# {cleared[:-3]}\n\n"
            "(Cleared by persona materialization — this workspace's identity and "
            "role are defined in SOUL.md; discover and delegate to peers via the "
            "`molecule` MCP tools.)",
        )
    return target


def materialize_codex_persona(config_path: Path, persona: str) -> Path:
    """Codex — write the persona to ``<configs>/AGENTS.md`` (the AAIF convention
    codex reads from its project directory)."""
    target = Path(config_path) / CODEX_PERSONA_FILE
    _write_persona_file(target, persona)
    return target


def materialize_gemini_persona(config_path: Path, persona: str) -> Path:
    """Gemini / google-adk — write the persona to ``<configs>/GEMINI.md``
    (the Gemini context-file convention)."""
    target = Path(config_path) / GEMINI_PERSONA_FILE
    _write_persona_file(target, persona)
    return target


def materialize_hermes_persona(config_path: Path, persona: str) -> Path:
    """TODO: Hermes' native identity-file convention is unverified.

    Hermes wires identity through its own agent descriptor rather than a plain
    markdown identity file, and the concrete location/format is not confirmed in
    this repo. Rather than guess and write a file a hermes agent silently never
    reads (the persona analogue of the #3159 MCP mis-attribution), this is a
    marked fail-loud stub. The boot-path caller downgrades the NotImplementedError
    to a non-fatal warning (a persona is not a privileged capability). Implement
    concretely once the hermes adapter's identity convention is pinned against a
    live runtime — one dict entry + one writer, no other change."""
    raise NotImplementedError(
        "hermes persona materialization not implemented — native identity "
        "convention unverified"
    )


# ---------------------------------------------------------------------------
# Native identity-file path resolvers (uniform (config_path) -> Path signature).
# ---------------------------------------------------------------------------

def _claude_persona_path(config_path: str | os.PathLike) -> Path:
    return Path(config_path) / CLAUDE_PERSONA_FILE


def _openclaw_persona_path(config_path: str | os.PathLike) -> Path:
    return Path(config_path) / OPENCLAW_PERSONA_FILE


def _codex_persona_path(config_path: str | os.PathLike) -> Path:
    return Path(config_path) / CODEX_PERSONA_FILE


def _gemini_persona_path(config_path: str | os.PathLike) -> Path:
    return Path(config_path) / GEMINI_PERSONA_FILE


# ===========================================================================
# Per-runtime dispatch — the production wiring.
# ===========================================================================
# runtime -> (path_resolver, materializer). Mirrors mcp_render._RUNTIME_SPECS.
# claude_code is ALSO the default for any unmapped runtime, so a new runtime that
# hasn't been mapped yet still materializes into the base convention rather than
# crashing — except the deliberate fail-loud stubs (hermes), whose materializer
# raises and whose caller warns rather than bricking boot.
_RUNTIME_PERSONA: dict[str, tuple] = {
    "claude_code": (_claude_persona_path, materialize_claude_persona),
    "openclaw": (_openclaw_persona_path, materialize_openclaw_persona),
    "codex": (_codex_persona_path, materialize_codex_persona),
    # Gemini and google-adk share the GEMINI.md context-file convention.
    "gemini": (_gemini_persona_path, materialize_gemini_persona),
    "google_adk": (_gemini_persona_path, materialize_gemini_persona),
    # hermes: native identity convention unverified — fail-loud stub.
    "hermes": (_claude_persona_path, materialize_hermes_persona),
}

# The runtime used when the active runtime isn't mapped above. Claude Code is the
# base runtime, so an unmapped runtime keeps base behavior (materialize into
# system-prompt.md) rather than failing.
_DEFAULT_RUNTIME = "claude_code"

# Runtimes whose materializer is a deliberate fail-loud stub (convention unverified).
_UNVERIFIED_RUNTIMES = frozenset({"hermes"})


def _spec_for(runtime: str) -> tuple:
    return _RUNTIME_PERSONA.get(normalize_runtime(runtime), _RUNTIME_PERSONA[_DEFAULT_RUNTIME])


def is_persona_supported(runtime: str) -> bool:
    """True when this runtime has a CONCRETE (non-stub) persona materializer.

    Unmapped runtimes fall back to the claude materializer (supported); the hermes
    stub is mapped but its materializer raises, so it is reported unsupported."""
    return normalize_runtime(runtime) not in _UNVERIFIED_RUNTIMES


def persona_path_for(runtime: str, config_path: str | os.PathLike) -> Path:
    """Absolute native identity file the given runtime reads its persona from."""
    return _spec_for(runtime)[0](config_path)


def materialize_persona_for(
    runtime: str, config_path: str | os.PathLike, persona: str
) -> Path | None:
    """Materialize ``persona`` into ``runtime``'s native identity file.

    Returns the path written, or ``None`` when ``persona`` is empty/whitespace
    (no-op — never clobber a runtime's baked default with an empty identity).
    Raises ``NotImplementedError`` for an unverified runtime (hermes); the caller
    decides whether that is fatal (it is NOT for a persona)."""
    if not (persona or "").strip():
        return None
    _, materialize_fn = _spec_for(runtime)
    target = materialize_fn(Path(config_path), persona)
    return target
