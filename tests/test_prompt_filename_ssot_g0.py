"""G0 guardrail (runtime half) — the prompt-file FILENAME SSOT is
``system-prompt.md``.

Task #80 (de-bake guardrails).

The concierge filename split: the template declared its identity under one name
while core's substitute/probe and the runtime's fallback assumed another, so the
per-instance identity never reached the agent. The fix consolidated EVERY layer
on ``system-prompt.md``:

  * runtime ``build_system_prompt`` fallback  → ``system-prompt.md``  (THIS half)
  * core ``IsCPTemplateAssetPath`` allowlist   → ``system-prompt.md``
  * core ``substituteConciergeName`` target    → ``system-prompt.md``
  * core ``conciergeIdentityPresent`` probe    → ``/configs/system-prompt.md``
  * default ``config.yaml`` ``prompt_files``    → ``- system-prompt.md``

The core half (allowlist + subst + probe + default config.yaml) is pinned by the
companion Go guardrail ``TestConciergePromptFilenameSSOT`` in molecule-core. This
file pins the runtime end of the convergence: the no-``prompt_files`` fallback
filename, AND the negative fixture — a ``prompt_files`` pointing at a file the
template doesn't ship must NOT silently substitute a different file; the declared
identity is simply absent (a RED a real concierge would surface as a missing
identity, not a wrong one).
"""
from __future__ import annotations

import inspect

from molecule_runtime.prompt import build_system_prompt

# The ONE canonical prompt filename every layer converges on. If this constant
# ever needs to change, it must change in lockstep across runtime + core (and the
# core guardrail asserts the same literal), which is the whole point of the SSOT.
CANONICAL_PROMPT_FILENAME = "system-prompt.md"


def _write(base, rel, text):
    f = base / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def test_runtime_fallback_filename_is_canonical(tmp_path):
    """With NO prompt_files declared, build_system_prompt falls back to exactly
    the canonical filename — the same literal core substitutes/probes."""
    _write(tmp_path, CANONICAL_PROMPT_FILENAME, "CANONICAL-FALLBACK-IDENTITY")
    # A decoy under a DIFFERENT name must NOT be loaded by the fallback.
    _write(tmp_path, "concierge.md", "DECOY-WRONG-FILENAME")
    out = build_system_prompt(tmp_path.as_posix(), "ws-g0", [], [], prompt_files=None, a2a_mcp=False)
    assert "CANONICAL-FALLBACK-IDENTITY" in out
    assert "DECOY-WRONG-FILENAME" not in out


def test_fallback_literal_present_in_source():
    """Belt-and-suspenders: the canonical literal IS the fallback in the source.

    If someone renames the fallback filename in build_system_prompt without
    updating the SSOT (and the core side), this trips — even before a behavioral
    test would, since it reads the function source directly."""
    src = inspect.getsource(build_system_prompt)
    assert f'"{CANONICAL_PROMPT_FILENAME}"' in src or f"'{CANONICAL_PROMPT_FILENAME}'" in src, (
        f"build_system_prompt must fall back to {CANONICAL_PROMPT_FILENAME!r} "
        "(the filename SSOT shared with core's subst/probe/allowlist)."
    )


def test_negative_prompt_files_points_at_unshipped_file(tmp_path):
    """NEGATIVE fixture: a config.yaml prompt_files that names a file the template
    does NOT ship → the declared identity is absent (no silent substitution of a
    different file). The agent boots WITHOUT the role identity rather than with a
    wrong one — the exact regression the filename SSOT prevents.

    This proves convergence MATTERS: if prompt_files and the shipped filename
    diverge, the identity is lost. (Mirrors the core negative fixture.)"""
    # Template SHIPS its identity at the canonical filename…
    _write(tmp_path, CANONICAL_PROMPT_FILENAME, "ROLE-IDENTITY-THAT-SHOULD-LOAD")
    # …but config.yaml declares a NON-shipped prompt file (the split).
    out = build_system_prompt(
        tmp_path.as_posix(), "ws-g0", [], [],
        prompt_files=["prompts/does-not-exist.md"], a2a_mcp=False,
    )
    # Declared file is missing → its identity never loads.
    assert "ROLE-IDENTITY-THAT-SHOULD-LOAD" not in out
    # And because prompt_files was non-empty, the canonical fallback is NOT used
    # either — divergence => identity LOST (not wrong). The base platform frame
    # still anchors the prompt so the agent boots (G2), just identity-less here.
    assert "Molecule AI platform" in out


def test_positive_aligned_prompt_files_loads_identity(tmp_path):
    """The aligned case: prompt_files names the file the template ships → identity
    loads. The contrast with the negative fixture above is what makes the
    convergence load-bearing."""
    _write(tmp_path, "prompts/concierge.md", "ALIGNED-ROLE-IDENTITY")
    out = build_system_prompt(
        tmp_path.as_posix(), "ws-g0", [], [],
        prompt_files=["prompts/concierge.md"], a2a_mcp=False,
    )
    assert "ALIGNED-ROLE-IDENTITY" in out
