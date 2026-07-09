"""Guard A — CROSS-RUNTIME CONTRACT META-GATE.

The wall that stops the "claude-code only" pattern (design SSOT:
``regression-prevention-guards.md`` §Guard A). Tonight's audit found the #1
recurring regression is a cross-runtime contract shipped for ONE runtime (the
skills-bridge / persona-materialization were claude-code-only at birth) while
every other runtime silently fell back to claude-code's renderer — a capability
loss no test caught.

This meta-gate makes that IMPOSSIBLE to merge. It is GENERIC:

  * The runtime axis is the SSOT ``live_runtimes.LIVE_RUNTIMES`` (openclaw is the
    operator default). Add a runtime there → this matrix demands an adapter or a
    documented exemption for it in EVERY contract.
  * The contract axis is AUTO-DISCOVERED: every ``molecule_runtime/*_render.py``
    module that registers a per-runtime dispatch table (a ``_RUNTIME_*`` dict) is
    a cross-runtime contract. Add a NEW contract module → it auto-joins the
    matrix and trips RED for any live runtime it doesn't cover. (So when
    runtime#235's ``skills_render`` lands, skills join the guarded set with no
    edit here.)

For every (contract × live runtime) cell the runtime must have ONE stance:

  * CONCRETE      — its own entry in the contract's registry (a real adapter).
  * FAIL_LOUD     — a documented stub in ``_UNVERIFIED_RUNTIMES`` (raises loudly)
                    — never a silent no-op.
  * DOC_SKIP      — a documented not-on-disk skip in ``_PROMPT_EMBEDDED_RUNTIMES``
                    (e.g. google-adk skills ride the prompt, not a file).

A runtime that is NONE of these is UNGUARDED: it would silently resolve through
``_spec_for`` to the default runtime's renderer — the exact reference-runtime
creep this guard forbids. That cell trips the matrix RED.

FAIL-BEFORE proof is baked into CI (no manual step): ``test_metagate_selftest_*``
build a SYNTHETIC claude-code-only contract (openclaw missing, no exemption) and
assert the SAME gap-finder that powers the real matrix reports it RED, then a
satisfied synthetic reports GREEN.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

import molecule_runtime
from molecule_runtime import mcp_render, persona_render
from molecule_runtime.live_runtimes import DEFAULT_RUNTIME, LIVE_RUNTIMES

_norm = mcp_render.normalize_runtime

# A module-level dict is a per-runtime dispatch registry iff its name matches
# this (``_RUNTIME_SPECS`` / ``_RUNTIME_PERSONA`` / ``_RUNTIME_SKILLS`` /
# ``_RUNTIME_READERS`` …). We UNION the keys of every such dict in a module, so a
# runtime registered in ANY of a contract's tables counts as registered.
_REGISTRY_ATTR = re.compile(r"^_RUNTIME_[A-Z_]+$")


@dataclass(frozen=True)
class Contract:
    """A cross-runtime SDK contract, reduced to what the meta-gate checks."""

    name: str                 # short module name, e.g. "mcp_render"
    registered: frozenset     # normalized keys with a concrete adapter entry
    fail_loud: frozenset      # normalized keys documented as fail-loud stubs
    doc_skip: frozenset       # normalized keys documented as not-on-disk skips
    default_runtime: str | None   # module ``_DEFAULT_RUNTIME`` (if declared)


def _as_contract(module) -> Contract | None:
    """Reduce a module to a ``Contract`` iff it registers a per-runtime table."""
    registry_dicts = [
        v
        for k, v in vars(module).items()
        if _REGISTRY_ATTR.match(k) and isinstance(v, dict) and v
    ]
    if not registry_dicts:
        return None
    registered = {
        _norm(k) for d in registry_dicts for k in d.keys() if isinstance(k, str)
    }
    fail_loud = {_norm(k) for k in getattr(module, "_UNVERIFIED_RUNTIMES", ()) or ()}
    doc_skip = {_norm(k) for k in getattr(module, "_PROMPT_EMBEDDED_RUNTIMES", ()) or ()}
    default = getattr(module, "_DEFAULT_RUNTIME", None)
    return Contract(
        name=module.__name__.rsplit(".", 1)[-1],
        registered=frozenset(registered),
        fail_loud=frozenset(fail_loud),
        doc_skip=frozenset(doc_skip),
        default_runtime=_norm(default) if isinstance(default, str) else None,
    )


def discover_contracts() -> list[Contract]:
    """Auto-discover every cross-runtime contract module in the SDK.

    Scans ``molecule_runtime/*_render.py`` (the naming convention for the
    per-runtime dispatch ports) and keeps those that register a ``_RUNTIME_*``
    table. A NEW ``*_render.py`` contract auto-joins the matrix — no edit here."""
    pkg_dir = Path(molecule_runtime.__file__).parent
    contracts: list[Contract] = []
    for path in sorted(pkg_dir.glob("*_render.py")):
        mod = importlib.import_module(f"molecule_runtime.{path.stem}")
        c = _as_contract(mod)
        if c is not None:
            contracts.append(c)
    return contracts


# ── stance / gap finder (pure — powers BOTH the real matrix and the self-test) ─

CONCRETE, FAIL_LOUD, DOC_SKIP, UNGUARDED = (
    "concrete", "fail_loud_exempt", "documented_skip", "UNGUARDED",
)


def stance_of(contract: Contract, runtime: str) -> str:
    """One-of stance for a (contract, runtime) cell (see module doc)."""
    key = _norm(runtime)
    if key in contract.fail_loud:
        return FAIL_LOUD
    if key in contract.doc_skip:
        return DOC_SKIP
    if key in contract.registered:
        return CONCRETE
    return UNGUARDED


def contract_gaps(contract: Contract, runtimes=LIVE_RUNTIMES) -> list[str]:
    """Live runtimes with NO stance for this contract (would silently fall back
    to the default runtime's renderer). Empty == the contract is fully guarded."""
    return [r for r in runtimes if stance_of(contract, r) == UNGUARDED]


# ── discovery sanity: the matrix must never be vacuous ─────────────────────────

def test_metagate_discovers_the_known_contracts():
    names = {c.name for c in discover_contracts()}
    assert {"mcp_render", "persona_render"} <= names, (
        f"cross-runtime contract discovery is degraded — expected at least "
        f"mcp_render + persona_render, found {sorted(names)}. A vacuous matrix "
        f"would pass every cell trivially."
    )


# ── the matrix: every contract × every LIVE runtime must be guarded ────────────

_CONTRACTS = discover_contracts()
_MATRIX = [(c, r) for c in _CONTRACTS for r in LIVE_RUNTIMES]


@pytest.mark.parametrize(
    "contract,runtime",
    _MATRIX,
    ids=[f"{c.name}|{r}" for c, r in _MATRIX],
)
def test_every_contract_covers_every_live_runtime(contract, runtime):
    """The WALL: a live runtime with no concrete adapter AND no documented
    exemption for this contract trips RED — a claude-code-only contract (or a
    newly-added runtime/contract left uncovered) cannot merge."""
    stance = stance_of(contract, runtime)
    assert stance != UNGUARDED, (
        f"UNGUARDED cell: runtime {runtime!r} has no adapter for the "
        f"{contract.name!r} contract and is not a documented exemption. It would "
        f"silently fall back to the default runtime's renderer (reference-runtime "
        f"creep — the claude-code-only bug this guard forbids). Fix: add a "
        f"concrete _RUNTIME_* entry for {runtime!r}, OR document it as a fail-loud "
        f"stub in _UNVERIFIED_RUNTIMES / a skip in _PROMPT_EMBEDDED_RUNTIMES."
    )


# ── kill reference-runtime creep: default is openclaw, never claude_code ───────

@pytest.mark.parametrize("contract", _CONTRACTS, ids=[c.name for c in _CONTRACTS])
def test_contract_default_runtime_is_not_claude_code(contract):
    """Every contract's unmapped-runtime fallback is the operator default
    (openclaw), never claude_code."""
    if contract.default_runtime is None:
        pytest.skip(f"{contract.name} declares no _DEFAULT_RUNTIME")
    assert contract.default_runtime != _norm("claude_code"), (
        f"{contract.name}._DEFAULT_RUNTIME is claude_code — reference-runtime "
        f"creep. It must be the operator default ({DEFAULT_RUNTIME})."
    )
    assert contract.default_runtime == _norm(DEFAULT_RUNTIME)


def test_unknown_runtime_does_not_silently_resolve_to_claude_code():
    """A genuinely-unknown runtime must NOT silently resolve to the claude_code
    renderer — it resolves to the operator default (openclaw). This is the
    concrete anti-creep assertion for both live ports."""
    unknown = "totally-unknown-runtime-xyz"

    mcp_spec = mcp_render._spec_for(unknown)
    assert mcp_spec is not mcp_render._RUNTIME_SPECS["claude_code"], (
        "mcp_render silently resolves an unknown runtime to the claude_code spec"
    )
    assert mcp_spec is mcp_render._RUNTIME_SPECS[_norm(DEFAULT_RUNTIME)]
    assert mcp_render._DEFAULT_RUNTIME != "claude_code"

    persona_spec = persona_render._spec_for(unknown)
    assert persona_spec is not persona_render._RUNTIME_PERSONA["claude_code"], (
        "persona_render silently resolves an unknown runtime to claude_code"
    )
    assert persona_spec is persona_render._RUNTIME_PERSONA[_norm(DEFAULT_RUNTIME)]
    assert persona_render._DEFAULT_RUNTIME != "claude_code"


# ── FAIL-BEFORE proof, baked into CI (deterministic, no manual step) ───────────

def test_metagate_selftest_red_on_claude_code_only_contract():
    """(fail-before) A SYNTHETIC claude-code-only contract — only claude_code +
    codex, openclaw absent, no exemption — must be reported RED by the SAME
    gap-finder the real matrix uses. This proves the wall goes RED on the exact
    'claude-code-only / missing openclaw adapter' violation."""
    synthetic = Contract(
        name="synthetic_claude_only",
        registered=frozenset({"claude_code", "codex"}),
        fail_loud=frozenset(),
        doc_skip=frozenset(),
        default_runtime="claude_code",
    )
    gaps = contract_gaps(synthetic)
    assert "openclaw" in gaps, (
        "meta-gate FAILED to flag a claude-code-only contract — openclaw has no "
        f"adapter yet was not reported as a gap (gaps={gaps})."
    )
    # The stance for openclaw is explicitly UNGUARDED (the RED condition).
    assert stance_of(synthetic, "openclaw") == UNGUARDED


def test_metagate_selftest_green_when_satisfied():
    """(fail-before, other half) The identical gap-finder reports GREEN (no gaps)
    once every live runtime has a concrete adapter or a documented exemption."""
    satisfied = Contract(
        name="synthetic_satisfied",
        registered=frozenset(_norm(r) for r in LIVE_RUNTIMES if r != "google_adk"),
        fail_loud=frozenset({"google_adk"}),   # documented fail-loud exemption
        doc_skip=frozenset(),
        default_runtime=_norm(DEFAULT_RUNTIME),
    )
    assert contract_gaps(satisfied) == [], (
        "a fully-adapted contract was wrongly reported RED"
    )


def test_metagate_selftest_red_when_new_runtime_added_without_adapter():
    """(fail-before) Adding a NEW live runtime that no contract adapts trips RED —
    the same gap-finder, a runtime axis extended by one unmapped runtime."""
    contract = Contract(
        name="synthetic_full_live",
        registered=frozenset(_norm(r) for r in LIVE_RUNTIMES),
        fail_loud=frozenset(),
        doc_skip=frozenset(),
        default_runtime=_norm(DEFAULT_RUNTIME),
    )
    extended = tuple(LIVE_RUNTIMES) + ("brand_new_runtime",)
    gaps = contract_gaps(contract, runtimes=extended)
    assert gaps == ["brand_new_runtime"], (
        f"a new unmapped runtime should be the only gap, got {gaps}"
    )
