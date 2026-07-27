"""Drift-guard for the DELIBERATE self-source subset relationship.

``a2a_executor._ROUTINE_SELF_SOURCE_TYPES`` is a deliberately NARROW GOVERNANCE
subset — the ``self-*`` markers whose INCOMING ping drops-not-queues on the
non-blocking fast-path AND whose OUTPUT is subject to the autonomous-loop
replay guard. It is intentionally DISTINCT from (and must NOT be unified with)
the platform's broader self-source CLASSIFICATION set, whose authority is the
SDK contract:

    molecule-ai-sdk  contracts/workspace-comms/self-source-types.schema.json
    → molcontracts.SelfSourceTypes / canvas SELF_SOURCE_TYPES

The classification set answers a DIFFERENT question ("does this render as a
system notice instead of a user bubble?") and includes PLATFORM-FIRED inbound
wakes (self-warmup, self-restart-context, self-first-boot-greet, self-stall,
self-nudge) that the runtime must QUEUE (deliver), never drop — so widening
the governance tuple to "match" the classification set would break platform
wake delivery. See the SSOT NOTE comment block above
``_ROUTINE_SELF_SOURCE_TYPES`` in a2a_executor.py.

The runtime does NOT vendor a copy of the full classification set (the SDK
contract is the sole authority; the runtime's ``_SELF_SOURCE_TYPES`` is the
routine tuple plus a ``self-`` prefix fallback in ``_is_self_source_type``).
So per the RFC follow-up we cannot assert a subset against a vendored full
list. Instead this guards the invariant we CAN check locally, so a future
rename/removal that orphans a governance marker fails loudly:

  * every governance marker classifies as a self turn (``_is_self_source_type``)
    and carries the ``self-`` prefix the classification membership relies on;
  * the governance subset is exactly the intended constants (a silent
    add/remove trips the test, forcing a conscious review);
  * the classification-only PLATFORM-FIRED markers are NOT in the governance
    subset — encoding the comment's warning as an executable guard so a
    well-meaning "unify the sets" change fails here instead of in prod.

This test does NOT unify the sets — the divergence is by design; it only
guards it.
"""

from __future__ import annotations

from molecule_runtime.a2a_executor import (
    A2A_SOURCE_SELF_CRON,
    A2A_SOURCE_SELF_DELEGATION,
    A2A_SOURCE_SELF_GOAL_NUDGE,
    A2A_SOURCE_SELF_HARVESTER,
    A2A_SOURCE_SELF_IDLE,
    A2A_SOURCE_SELF_LIFECYCLE,
    A2A_SOURCE_SELF_SCHEDULER,
    _ROUTINE_SELF_SOURCE_TYPES,
    _SELF_SOURCE_TYPES,
    _is_self_source_type,
)

# The classification-only markers named in the a2a_executor SSOT NOTE: these
# are PLATFORM-FIRED inbound wakes the runtime must QUEUE (deliver), not drop.
# They belong to the broader SDK classification set but MUST stay OUT of the
# governance subset. Kept here as documentation mirrored from the comment —
# NOT an authoritative copy of the classification set (the SDK contract is the
# authority); a drift in the SDK set is caught upstream by the schema gate.
_PLATFORM_FIRED_CLASSIFICATION_ONLY = (
    "self-warmup",
    "self-restart-context",
    "self-first-boot-greet",
    "self-stall",
    "self-nudge",
)


def test_every_governance_marker_classifies_as_self():
    """Each governance marker must satisfy ``_is_self_source_type`` and carry
    the ``self-`` prefix. If a future rename dropped the prefix or a removal
    orphaned a marker from the classification predicate, this fails loudly —
    the whole point of the guard."""
    assert _ROUTINE_SELF_SOURCE_TYPES, "governance subset must not be empty"
    for marker in _ROUTINE_SELF_SOURCE_TYPES:
        assert isinstance(marker, str) and marker
        assert marker.startswith("self-"), marker
        assert _is_self_source_type(marker) is True, marker


def test_governance_subset_is_within_self_source_set():
    """Subset invariant: every governance marker is a member of the runtime's
    self-source set used by the rule-3 preemption predicate. Guards against a
    marker existing in the governance tuple while being invisible to the
    self-vs-user preemption decision."""
    assert set(_ROUTINE_SELF_SOURCE_TYPES).issubset(set(_SELF_SOURCE_TYPES))


def test_governance_subset_is_exactly_the_intended_markers():
    """Pin the governance subset to its intended members so a silent add or
    removal is caught and forces a conscious review of the SSOT NOTE. Adding
    a legitimate new governance marker SHOULD update this list (deliberately)."""
    expected = {
        A2A_SOURCE_SELF_CRON,
        A2A_SOURCE_SELF_HARVESTER,
        A2A_SOURCE_SELF_IDLE,
        A2A_SOURCE_SELF_SCHEDULER,
        A2A_SOURCE_SELF_GOAL_NUDGE,
        A2A_SOURCE_SELF_DELEGATION,
        A2A_SOURCE_SELF_LIFECYCLE,
    }
    assert set(_ROUTINE_SELF_SOURCE_TYPES) == expected


def test_platform_fired_classification_markers_are_not_governed():
    """The classification-only PLATFORM-FIRED wakes must NOT be in the
    governance subset — they must be QUEUED/delivered, not dropped. This
    encodes the a2a_executor SSOT-NOTE warning as an executable guard: a
    change that "unifies" the sets by adding these here fails LOUDLY.

    They still classify as self turns (via the ``self-`` prefix) — that is
    correct for preemption — but they are NOT routine-governance markers."""
    for marker in _PLATFORM_FIRED_CLASSIFICATION_ONLY:
        assert marker not in _ROUTINE_SELF_SOURCE_TYPES, (
            f"{marker} is a platform-fired classification-only wake that must "
            "be QUEUED, not dropped — it must never enter the governance subset"
        )
        # But it IS a self turn for the rule-3 preemption predicate.
        assert _is_self_source_type(marker) is True, marker
