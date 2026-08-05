"""The comparison that would have caught the 54-tools-zero-callable incident.

`loaded_mcp_tools` is produced by asking the MCP SERVER what it advertises, so
it can only ever prove the SERVER is healthy. On a live concierge it reported 54
management tools loaded while the model could call ZERO of them: hermes'
tool_search had deferred the whole MCP surface behind three bridge tools. The
one signal that looked like it covered that case is the one that hid it.

These tests pin the fix AND pin the two ways it could be quietly re-broken:

  * a comparison that never fires (constant subset=True) — the original bug in
    a new costume;
  * a comparison that ALWAYS fires (constant subset=False) — which is what a
    RAW string comparison does on hermes, because the probe spells the server
    `molecule-platform` and hermes registers `molecule_platform`. A gate that
    is red on every healthy beat gets muted, and then it is not a gate.

Live measurements these tests encode (2026-08-05, one concierge, same servers,
only tools.tool_search.enabled differing):

    probe reports                : 60 ids (54 management)   [BOTH configs]
    tool_search=off  model-facing: 120 (92 mcp__*)  loaded ⊄ facing:  0/60
    tool_search=on   model-facing:  28 ( 0 mcp__*)  loaded ⊄ facing: 60/60
                                   activated=True deferred=95 tier=1
"""
from __future__ import annotations

import asyncio

import pytest

from molecule_runtime import platform_agent_identity as pai
from molecule_runtime.loaded_mcp_tools_probe import (
    canonical_tool_id,
    capture_model_facing_tools,
    loaded_not_model_facing,
)


@pytest.fixture(autouse=True)
def _clear_producers():
    pai.set_loaded_mcp_tools(None)
    pai.set_model_facing_tools(None)
    yield
    pai.set_loaded_mcp_tools(None)
    pai.set_model_facing_tools(None)


# The real ids, verbatim from the live box.
PROBE_IDS = [
    "mcp__molecule-platform__create_workspace",
    "mcp__molecule-platform__list_workspaces",
    "mcp__molecule-platform__delete_workspace",
]
HERMES_REGISTERED = [
    "mcp__molecule_platform__create_workspace",
    "mcp__molecule_platform__list_workspaces",
    "mcp__molecule_platform__delete_workspace",
]
BRIDGE_ONLY = ["tool_search", "tool_describe", "tool_call", "bash", "read_file"]


# ── the spelling split is real, and it is why we canonicalise ──────────────
def test_probe_and_hermes_spell_the_same_tool_differently():
    """If this ever starts passing as equal, delete canonical_tool_id."""
    assert PROBE_IDS[0] != HERMES_REGISTERED[0]
    assert canonical_tool_id(PROBE_IDS[0]) == canonical_tool_id(HERMES_REGISTERED[0])


def test_raw_comparison_would_be_a_constant_false_alarm():
    """A gate that fires on a HEALTHY box is not a gate — pin that we avoid it.

    Every probe id is absent from the hermes-registered set BY SPELLING ALONE,
    so a naive raw diff reports full divergence on a perfectly healthy
    concierge. This test documents the trap the fix sidesteps.
    """
    raw_missing = [t for t in PROBE_IDS if t not in set(HERMES_REGISTERED)]
    assert raw_missing == PROBE_IDS  # constant false alarm
    assert loaded_not_model_facing(PROBE_IDS, HERMES_REGISTERED) == []


# ── the incident, and its converging control ───────────────────────────────
def test_deferred_surface_is_flagged_degraded():
    """tool_search deferred the MCP tools: loaded ⊄ model-facing -> DEGRADED."""
    orphaned = loaded_not_model_facing(PROBE_IDS, BRIDGE_ONLY)
    assert orphaned == sorted(PROBE_IDS)
    assert orphaned, "a deferred management surface MUST report degraded"


def test_not_deferred_converges():
    """Same probe output, tool_search off: the sets converge -> healthy."""
    assert loaded_not_model_facing(PROBE_IDS, HERMES_REGISTERED + BRIDGE_ONLY) == []


def test_partial_deferral_is_flagged():
    """One tool short is still a hole — the check is subset, not intersection."""
    facing = HERMES_REGISTERED[:-1] + BRIDGE_ONLY
    assert loaded_not_model_facing(PROBE_IDS, facing) == [
        "mcp__molecule-platform__delete_workspace"
    ]


# ── nonsense negative control ──────────────────────────────────────────────
def test_nonsense_id_is_in_neither_set():
    """If a fabricated id ever shows up on either side the comparison is fake."""
    bogus = "mcp__not-a-real-server__definitely_not_a_tool"
    assert bogus not in PROBE_IDS
    assert bogus not in HERMES_REGISTERED
    # and injecting it into the loaded set is reported as orphaned, not ignored
    assert bogus in loaded_not_model_facing(PROBE_IDS + [bogus], HERMES_REGISTERED)


# ── tri-state: absence must never read as health ───────────────────────────
@pytest.mark.parametrize(
    "loaded,facing",
    [(None, BRIDGE_ONLY), (PROBE_IDS, None), (None, None)],
)
def test_unobserved_side_yields_no_verdict(loaded, facing):
    assert loaded_not_model_facing(loaded, facing) is None


def test_empty_model_facing_is_a_real_verdict_not_unknown():
    """[] is 'the model got nothing' — the worst case, and it must be LOUD."""
    assert loaded_not_model_facing(PROBE_IDS, []) == sorted(PROBE_IDS)


# ── heartbeat payload wiring ───────────────────────────────────────────────
def test_payload_omits_all_three_fields_when_unobserved():
    payload = pai.identity_gate_payload()
    assert "model_facing_tools" not in payload
    assert "loaded_not_model_facing" not in payload


def test_payload_carries_the_degraded_verdict():
    pai.set_loaded_mcp_tools(PROBE_IDS)
    pai.set_model_facing_tools(BRIDGE_ONLY)
    payload = pai.identity_gate_payload()
    assert payload["loaded_mcp_tools"] == PROBE_IDS
    assert payload["model_facing_tools"] == BRIDGE_ONLY
    assert payload["loaded_not_model_facing"] == sorted(PROBE_IDS)


def test_payload_verdict_is_empty_on_a_healthy_box():
    pai.set_loaded_mcp_tools(PROBE_IDS)
    pai.set_model_facing_tools(HERMES_REGISTERED)
    payload = pai.identity_gate_payload()
    assert payload["loaded_not_model_facing"] == []


def test_payload_has_no_verdict_when_only_the_old_signal_exists():
    """The pre-fix state must stay EXACTLY as it was — no manufactured verdict."""
    pai.set_loaded_mcp_tools(PROBE_IDS)
    payload = pai.identity_gate_payload()
    assert payload["loaded_mcp_tools"] == PROBE_IDS
    assert "loaded_not_model_facing" not in payload


# ── the adapter port ───────────────────────────────────────────────────────
class _Adapter:
    def __init__(self, result=None, boom=False):
        self._result, self._boom = result, boom

    async def enumerate_model_facing_tools(self, config):
        if self._boom:
            raise RuntimeError("adapter exploded")
        return self._result


def test_base_adapter_default_is_none_not_a_pass():
    """An unimplemented port must NOT manufacture a healthy verdict."""
    from molecule_runtime.adapter_base import BaseAdapter

    assert asyncio.run(
        BaseAdapter.enumerate_model_facing_tools(object(), None)
    ) is None


def test_capture_publishes_and_survives_a_throwing_adapter():
    assert asyncio.run(capture_model_facing_tools(_Adapter(BRIDGE_ONLY), None)) == BRIDGE_ONLY
    assert pai.model_facing_tools() == BRIDGE_ONLY

    pai.set_model_facing_tools(None)
    assert asyncio.run(capture_model_facing_tools(_Adapter(boom=True), None)) is None
    assert pai.model_facing_tools() is None
