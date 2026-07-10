"""Generic, runtime-name-free helpers for the CANONICAL PERSONA.

ADR-004 (`docs/adr/ADR-004-sdk-owns-adapter-contract-and-registry.md`) moved the
**per-runtime shape** of the persona seam — the native identity-file materializers
and their path resolvers — OUT of this shared engine and INTO each adapter (the
sibling of the same move for the MCP seam in :mod:`molecule_runtime.mcp_render`).
Official adapters ship their own native identity-file writer in their template
repos (claude-code → ``system-prompt.md``, openclaw → ``SOUL.md`` + cleared
placeholders, codex → ``AGENTS.md``, hermes → ``~/.hermes/SOUL.md``); third-party
adapters ship theirs wherever their author wants. **This module holds ZERO
per-runtime dispatch** — no ``_RUNTIME_PERSONA``, no ``materialize_persona_for``,
no runtime-name literals. It never spells a runtime name.

What remains here is the small set of GENERIC, name-free helpers ADR-004 §6 says
the engine keeps — reusable by any adapter (official or third-party):

  * :func:`read_canonical_persona` — the runtime-agnostic INPUT every materializer
    consumes: the workspace's intended identity, read from the delivered
    ``prompt_files`` (falling back to ``system-prompt.md``), joined. Mirrors how
    ``prompt.build_system_prompt`` sources the role, so materializing is stable
    across boots (idempotent, never accumulates the base frame).
  * :func:`write_persona` — the byte-shape writer (parents created, trailing
    newline appended only when absent) every native writer uses, so a materialized
    identity file is byte-identical regardless of which adapter wrote it.
  * :func:`default_persona_path` — the default identity file the BaseAdapter
    fallback (``adapter_base``) writes when an adapter does not override the
    persona seam. Not a per-runtime resolver — just the base/reference default.

DO NOT re-add a per-runtime dispatch table here. A red-on-regression ratchet
(``tests/test_engine_no_runtime_dispatch_ratchet.py``) fails any change that
re-introduces a ``_RUNTIME_*`` table or a runtime-name literal into this module —
per ADR-004 the drift can only shrink (→ 0).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# The default identity-file basename the BaseAdapter fallback writes, and the
# fallback persona SOURCE when a workspace declares no ``prompt_files`` — the same
# file ``build_system_prompt`` falls back to. A descriptor/format constant (the
# base/reference default), NOT a per-runtime name: an adapter whose runtime reads
# a different native identity file (openclaw SOUL.md, codex AGENTS.md, …) owns its
# own writer in its template repo.
DEFAULT_PERSONA_FILE = "system-prompt.md"

# Kept name for backward-compat with the fallback source used by
# read_canonical_persona (same value as DEFAULT_PERSONA_FILE).
_DEFAULT_PERSONA_SOURCE = DEFAULT_PERSONA_FILE


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
# Generic native-identity writer + default path — the BaseAdapter fallback.
# ---------------------------------------------------------------------------

def write_persona(target: Path, persona: str) -> None:
    """Write ``persona`` to ``target`` (parents created), trailing newline.

    The byte-shape every native identity writer uses (base default and each
    adapter's own writer) so a materialized identity file is byte-identical
    regardless of who wrote it: the trailing newline is appended only when
    absent."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = persona if persona.endswith("\n") else persona + "\n"
    target.write_text(body, encoding="utf-8")


def default_persona_path(config_path: str | os.PathLike) -> Path:
    """The default identity file the BaseAdapter fallback writes
    (``<config_path>/system-prompt.md``).

    The base/reference default identity-file location (the file the base
    executor reads as its system-prompt fallback). NOT a per-runtime resolver — an
    adapter whose runtime reads a different native identity file resolves its OWN
    path in its template repo. Kept only so the base default has a concrete file
    to write when an adapter does not override the seam."""
    return Path(config_path) / DEFAULT_PERSONA_FILE
