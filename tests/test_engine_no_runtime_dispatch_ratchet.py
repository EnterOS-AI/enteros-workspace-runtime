"""DRIFT-DOWN RATCHET (ADR-004) — the engine holds ZERO per-runtime dispatch.

ADR-004 (`docs/adr/ADR-004-sdk-owns-adapter-contract-and-registry.md`) moved the
per-runtime *shape* (MCP-config renderers/readers/present-probes + persona
materializers) OUT of the shared runtime engine and INTO each adapter, and made the
SDK own the adapter *socket* contract + the official registry. ADR-004 §Consequences
replaces the OLD engine-side guardrails — G6 (``test_mcp_render_completeness_g6``:
"every runtime has a concrete ``_RUNTIME_SPECS`` entry in the engine") and the
cross-runtime contract meta-gate (``test_cross_runtime_contract_metagate``: "every
LIVE runtime × every ``_RUNTIME_*`` contract is covered") — with:

  1. PER-ADAPTER conformance: the SDK conformance suite
     (``molecule_plugin.adapter_conformance.AdapterConformance``) asserts each
     adapter satisfies the socket (render→read→present lockstep on its OWN native
     config, tri-state enumerate, persona, fail-closed when unmapped). Every
     official template runs it (``molecule-ai-workspace-template-<rt>/tests/
     test_conformance.py``); first-party support is proven by running it across the
     official registry.

  2. THIS RATCHET: a red-on-regression test that FAILS if ``mcp_render`` /
     ``persona_render`` ever REGAIN a per-runtime dispatch table (``_RUNTIME_*``) or
     a runtime-name literal in CODE. Per ADR-004 the drift can only SHRINK — the
     target is ZERO, and this asserts it, so no one can re-extend the engine's
     per-runtime knowledge (the exact 2026-07-09 regression that added
     ``_RUNTIME_READERS['hermes'] = {}`` to the shared dict).

The old G6 / meta-gate proved "every runtime IS in the engine table"; this proves
the inverse — "NO runtime is in the engine at all". They are mutually exclusive by
construction, which is precisely the ADR-004 inversion.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import molecule_runtime
from molecule_runtime import mcp_render, persona_render

# The two engine modules that MUST hold zero per-runtime dispatch after ADR-004.
_ENGINE_MODULES = (mcp_render, persona_render)

# Runtime-name tokens that must NOT appear as CODE literals/identifiers in the
# engine modules (they may appear in comments/docstrings, which explain the ADR-004
# history — AST-based scanning below naturally exempts those). Both hyphen and
# underscore spellings, plus the ``render_<rt>`` / ``materialize_<rt>`` /
# ``_<rt>_`` fragments the deleted per-runtime functions used.
_RUNTIME_NAME_TOKENS = (
    "claude_code", "claude-code",
    "codex",
    "hermes",
    "openclaw",
    "gemini",
    "google_adk", "google-adk",
)

# The exact per-runtime dispatch symbols ADR-004 deleted — assert they stay gone
# (a rename that re-adds any of these is a regression).
_DELETED_MCP_SYMBOLS = (
    "_RUNTIME_SPECS", "_RUNTIME_READERS", "_spec_for", "_DEFAULT_RUNTIME",
    "_UNVERIFIED_RUNTIMES", "is_runtime_supported", "mcp_settings_path_for",
    "render_for_runtime", "management_mcp_present_for", "read_mcp_servers_for",
    "render_claude_settings", "render_codex_config", "render_hermes_config",
    "render_openclaw_config", "render_gemini_config",
    "_claude_path", "_codex_path", "_openclaw_path", "_hermes_path", "_gemini_path",
    "_json_settings_has", "_codex_config_has", "_openclaw_config_has",
    "_hermes_config_has",
    "_read_json_mcp_servers", "_read_codex_mcp_servers",
    "_read_openclaw_mcp_servers", "_read_hermes_mcp_servers",
)
_DELETED_PERSONA_SYMBOLS = (
    "_RUNTIME_PERSONA", "_spec_for", "_DEFAULT_RUNTIME", "_UNVERIFIED_RUNTIMES",
    "is_persona_supported", "persona_path_for", "materialize_persona_for",
    "materialize_claude_persona", "materialize_openclaw_persona",
    "materialize_codex_persona", "materialize_gemini_persona",
    "materialize_hermes_persona",
    "_claude_persona_path", "_openclaw_persona_path", "_codex_persona_path",
    "_gemini_persona_path", "_hermes_persona_path",
)

# The generic, runtime-name-free symbols the engine KEEPS (ADR-004 §6). Present-ness
# is asserted so the ratchet also catches an over-eager deletion of the surface the
# BaseAdapter default + adapters depend on.
_KEEP_MCP = (
    "normalize_runtime", "render_json_mcp_servers", "read_json_mcp_servers",
    "json_mcp_servers_has", "default_json_settings_path",
)
_KEEP_PERSONA = (
    "read_canonical_persona", "write_persona", "default_persona_path",
)


def _module_src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


# ── 1. No ``_RUNTIME_*`` dispatch table on either engine module ────────────────

@pytest.mark.parametrize("module", _ENGINE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_engine_has_no_runtime_dispatch_table(module):
    """ZERO ``_RUNTIME_*`` module attributes (dict or otherwise). This is the
    inverse of the deleted G6/meta-gate: the engine must key NOTHING on runtime."""
    offenders = [
        name for name in vars(module)
        if re.match(r"^_RUNTIME_[A-Z_]+$", name)
    ]
    assert offenders == [], (
        f"{module.__name__} re-grew per-runtime dispatch table(s) {offenders} — "
        "ADR-004 forbids ANY _RUNTIME_* table in the shared engine (the per-runtime "
        "shape lives in the adapter). Move it into the adapter + its SDK conformance "
        "suite; the engine keeps only generic, runtime-name-free helpers."
    )


# ── 2. The deleted per-runtime symbols stay deleted (no rename re-adds them) ───

def test_deleted_mcp_dispatch_symbols_stay_gone():
    present = [s for s in _DELETED_MCP_SYMBOLS if hasattr(mcp_render, s)]
    assert present == [], (
        f"mcp_render re-introduced deleted per-runtime symbol(s) {present} — "
        "ADR-004 relocated these into the adapters. Do not re-add them to the engine."
    )


def test_deleted_persona_dispatch_symbols_stay_gone():
    present = [s for s in _DELETED_PERSONA_SYMBOLS if hasattr(persona_render, s)]
    assert present == [], (
        f"persona_render re-introduced deleted per-runtime symbol(s) {present} — "
        "ADR-004 relocated these into the adapters. Do not re-add them to the engine."
    )


# ── 3. No runtime-name literal in CODE (comments/docstrings exempt) ────────────

def _code_string_and_name_tokens(source: str) -> list[str]:
    """Every string-constant value and identifier that appears in CODE (NOT in a
    module/class/function docstring; comments are already absent from the AST).

    Docstrings are the first statement of a module/class/function body and are
    excluded — they legitimately narrate the ADR-004 history. Everything else (a
    string literal used as a value, a dict key, an identifier, an attribute name,
    a keyword-arg name) is CODE and must carry no runtime name.
    """
    tree = ast.parse(source)
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ) and isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))

    tokens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_nodes:
                continue
            tokens.append(node.value)
        elif isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            tokens.append(node.arg)
    return tokens


@pytest.mark.parametrize("module", _ENGINE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_engine_code_has_no_runtime_name_literal(module):
    """Not one runtime-name token (``claude``/``codex``/``hermes``/``openclaw``/
    ``gemini``/``google-adk``) appears in CODE — only in comments/docstrings that
    narrate the ADR-004 move. A runtime-name literal in code means the engine
    started keying on a runtime again."""
    tokens = _code_string_and_name_tokens(_module_src(module))
    offenders = sorted({
        f"{tok!r} contains {name!r}"
        for tok in tokens
        for name in _RUNTIME_NAME_TOKENS
        if name in tok.lower()
    })
    assert offenders == [], (
        f"{module.__name__} has runtime-name literal(s) in CODE: {offenders}. "
        "ADR-004: the engine spells NO runtime name. Put per-runtime shape in the "
        "adapter (template repo); the engine keeps only generic helpers. (Runtime "
        "names are allowed in comments/docstrings that explain the history.)"
    )


# ── 4. The generic surface the engine KEEPS is still present ───────────────────

def test_generic_mcp_surface_is_kept():
    missing = [s for s in _KEEP_MCP if not hasattr(mcp_render, s)]
    assert missing == [], (
        f"mcp_render lost generic helper(s) {missing} the BaseAdapter default + "
        "adapters depend on (ADR-004 §6 keep-list)."
    )


def test_generic_persona_surface_is_kept():
    missing = [s for s in _KEEP_PERSONA if not hasattr(persona_render, s)]
    assert missing == [], (
        f"persona_render lost generic helper(s) {missing} the BaseAdapter default + "
        "adapters depend on (ADR-004 §6 keep-list)."
    )


# ── 5. FAIL-BEFORE proof (baked into CI, no manual step) — the ratchet CAN fail ─

def test_ratchet_self_test_detects_reintroduced_dispatch_table():
    """(fail-before) Prove the ratchet's table-detector goes RED on the exact
    2026-07-09 regression (a ``_RUNTIME_*`` dict re-added to the engine). Uses a
    throwaway module object so no source is mutated."""
    import types

    synthetic = types.ModuleType("synthetic_regressed_engine")
    synthetic.__file__ = mcp_render.__file__  # any real path; unused by this check
    synthetic._RUNTIME_READERS = {"hermes": {}}  # the exact regressed shape

    offenders = [n for n in vars(synthetic) if re.match(r"^_RUNTIME_[A-Z_]+$", n)]
    assert offenders == ["_RUNTIME_READERS"], (
        "the ratchet's _RUNTIME_* detector failed to flag a re-added dispatch table"
    )


def test_ratchet_self_test_detects_runtime_name_literal_in_code():
    """(fail-before, other half) Prove the code-literal scanner flags a runtime name
    used as a CODE literal, while a docstring mention is exempt."""
    regressed_src = (
        '"""Module docstring may say hermes freely — narration is exempt."""\n'
        "def render(runtime):\n"
        "    table = {'hermes': 1}\n"          # runtime name as a CODE dict key
        "    return table.get(runtime)\n"
    )
    tokens = _code_string_and_name_tokens(regressed_src)
    hit = [t for t in tokens if "hermes" in t.lower()]
    assert hit == ["hermes"], (
        f"the code-literal scanner should flag the CODE 'hermes' key only (got {hit})"
    )

    clean_src = (
        '"""Docstring: hermes / codex / openclaw narrated here are fine."""\n'
        "def render(runtime, spec):\n"
        "    return {runtime: spec}\n"
    )
    clean_tokens = _code_string_and_name_tokens(clean_src)
    assert not any(
        name in t.lower() for t in clean_tokens for name in _RUNTIME_NAME_TOKENS
    ), "a docstring-only runtime mention must NOT be flagged as a code literal"


# ── 6. Signpost: the SDK conformance suite is the per-adapter guarantor ────────

def test_signpost_sdk_conformance_suite_is_the_guarantor():
    """The replacement for the deleted G6/meta-gate is the SDK conformance suite
    (ADR-004 §Decision-4). This asserts the module the engine tests point to exists
    and is importable, so the pointer never rots. (The suite itself runs in the SDK
    repo + each template repo, against real adapters.)"""
    conformance = pytest.importorskip(
        "molecule_plugin.adapter_conformance",
        reason="SDK (molecule-ai-sdk) not installed in this env — the per-adapter "
        "conformance guarantor lives there and in each template repo",
    )
    assert hasattr(conformance, "AdapterConformance"), (
        "molecule_plugin.adapter_conformance must expose AdapterConformance — the "
        "inheritable per-adapter socket battery that replaces the engine's deleted "
        "G6/meta-gate (ADR-004 §Decision-4)."
    )
