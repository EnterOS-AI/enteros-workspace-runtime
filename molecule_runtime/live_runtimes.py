"""SSOT for the set of LIVE runtimes and the operator-default runtime.

This module is the *single list* every cross-runtime contract derives from, so
the runtime axis is declared ONCE and never hand-copied into each renderer /
test. Guard A (the cross-runtime contract meta-gate) parametrizes its matrix over
``LIVE_RUNTIMES`` × every registered SDK contract, and the per-contract renderers
import ``DEFAULT_RUNTIME`` from here for their unmapped-runtime fallback — so the
"reference runtime" can never silently drift back to ``claude_code``.

Why a dedicated SSOT
--------------------
Before this module the runtime set lived implicitly and INCONSISTENTLY across the
contract registries (``mcp_render._RUNTIME_SPECS`` lacked ``google_adk`` while
``persona_render._RUNTIME_PERSONA`` had it) and the unmapped fallback was
hand-set to ``claude_code`` in two places. That divergence is exactly how a
"claude-code only" contract slips in: a new runtime or a new contract is added
without every cell being covered, and the silent ``claude_code`` fallback hides
the gap at runtime. Deriving the axis + the default from one list makes the gap a
RED test instead of a latent capability loss.

Membership policy
-----------------
``LIVE_RUNTIMES`` is the set of runtimes a workspace can actually boot on today
(the design SSOT ``regression-prevention-guards.md`` §Guard A). It is a curated
subset of the broader manifest ``runtimes`` enum
(``contracts/plugin-manifest.schema.json`` — which also carries not-yet-live /
external values like ``crewai`` / ``external``). Adding a runtime here forces the
Guard A meta-gate to demand a concrete adapter OR a documented fail-loud
exemption for it in EVERY registered contract.
"""
from __future__ import annotations

# Underscore dispatch-key spelling (matches ``mcp_render.normalize_runtime`` /
# the ``_RUNTIME_*`` registry keys). ``openclaw`` is the operator DEFAULT.
#
# NOTE: keep this a plain tuple of normalized keys — every consumer normalizes
# its own input via ``normalize_runtime`` before comparing, so callers may pass
# either hyphen or underscore spelling.
LIVE_RUNTIMES: tuple[str, ...] = (
    "openclaw",     # operator DEFAULT (see DEFAULT_RUNTIME)
    "claude_code",
    "codex",
    "google_adk",
    "hermes",
)

# The operator-default runtime. This is the ONLY value the unmapped-runtime
# fallback in each contract renderer may resolve to — never ``claude_code``.
# Changing this changes every renderer's fallback in lockstep (single edit).
DEFAULT_RUNTIME: str = "openclaw"

assert DEFAULT_RUNTIME in LIVE_RUNTIMES, "DEFAULT_RUNTIME must be a live runtime"
