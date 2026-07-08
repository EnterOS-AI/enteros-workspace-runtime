"""Idle-digest contract vocabulary + policy — the runtime mirror of the SDK
``contracts/idle-prompt`` layer (molecule-ai-sdk#57, task #219).

This module holds the *shape* the assembler works with: the contribution
envelope, its enum vocabularies, and the assembler ``Policy`` (wake / sort /
delta / empty / limits / failure knobs). Values default to the operator-ruled
contract instance; the test-overridable knobs (idle-fire threshold, limits,
provider timeout) read an env override so tests and staged tenants can retune
without editing code — exactly the "contract values are production defaults"
framing the contract pins.

SEQUENCING NOTE: this is a hand-maintained mirror of the contract constants,
NOT yet loaded from the vendored SDK instance. The follow-up (gated on
molecule-ai-sdk#57 landing on sdk main) vendors ``idle-prompt.schema.json`` +
``idle-prompt.contract.json`` into ``molecule_runtime/contracts/`` and switches
:func:`Policy.default` to load from the vendored instance, with the
``scripts/check-schemas-in-sync.sh`` drift gate keeping the mirror honest (the
same pattern ``manifest_ssot.py`` uses for plugin-manifest). Until then the
values here are the SSOT mirror; :data:`CONTRACT_SCHEMA_VERSION` pins which
contract revision they mirror.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

CONTRACT_SCHEMA_VERSION = "idle-prompt/v1"


class Band(str, Enum):
    """Render band. ``pinned`` is a reserved capability grantable only to the
    official ``identity-capabilities`` provider (enforced by the assembler)."""

    PINNED = "pinned"
    URGENT = "urgent"
    BASE = "base"


class Urgency(str, Enum):
    NORMAL = "normal"
    URGENT = "urgent"


class AgeBand(str, Enum):
    """The ONLY time-derived field allowed in hashed or rendered content — raw
    timestamps/durations are forbidden (a raw age changes every tick and
    resurrects the steady-state nag loop). Item ages use the under-1h / 1h-1d /
    over-1d bands; the goal cadence rides this same slot with just-included /
    due; ``none`` for time-free contributions such as the header."""

    NONE = "none"
    JUST_INCLUDED = "just-included"
    DUE = "due"
    UNDER_1H = "under-1h"
    ONE_H_TO_1D = "1h-1d"
    OVER_1D = "over-1d"


# The one reserved id that may declare band=pinned.
PINNED_PROVIDER_ID = "identity-capabilities"

# Official ids the assembler rejects collisions/spoofing of.
RESERVED_PROVIDER_IDS = frozenset(
    {
        "identity-capabilities",
        "task-queue",
        "sent-folder",
        "inbound-a2a",
        "delegation-results",
        "scheduler",
        "goal-state",
    }
)


@dataclass(frozen=True)
class PullInstruction:
    """How the agent fetches bounded detail. ``tool`` must be a whitelisted MCP
    tool (assembler trust rule; enforcement lives at registration, not here)."""

    tool: str
    instruction: str
    max_items: int


@dataclass(frozen=True)
class PreviewItem:
    """A bounded preview row. NO raw time fields — ``age_band`` only."""

    item_id: str
    status: str
    summary: str
    age_band: AgeBand = AgeBand.NONE
    next_action: Optional[str] = None


@dataclass(frozen=True)
class Contribution:
    """The canonical contribution envelope a provider returns. A provider MAY
    return multiple envelopes (item-level urgency per D2). The ASSEMBLER
    computes the delta hash over the fixed tuple in :data:`HASH_FIELDS`;
    ``band`` is carried for rendering but excluded from the hash (it is fully
    determined by ``provider_id`` + ``urgency``)."""

    provider_id: str
    band: Band
    tier: int
    urgency: Urgency
    count: int
    summary: str
    age_band: AgeBand = AgeBand.NONE
    item_ids: tuple[str, ...] = ()
    pull: Optional[PullInstruction] = None
    rearm_signals: tuple[str, ...] = ()
    preview_items: tuple[PreviewItem, ...] = ()


# The canonical per-envelope hash serialization (7 fields — 'band' excluded).
# Order is load-bearing: the assembler serializes exactly these, in order.
HASH_FIELDS = (
    "provider_id",
    "tier",
    "urgency",
    "count",
    "summary",
    "age_band",
    "item_ids",
)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive-int env override, falling back (loudly-defaulting, never
    crashing) to the contract default on absent/garbage/too-small input."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


@dataclass(frozen=True)
class Policy:
    """Assembler policy — production defaults mirror the contract instance;
    the marked knobs are env-overridable (test/staging retune)."""

    # wake — the idle-fire threshold is a SECOND consumer of the turn lease,
    # never the stall TTL. See the design SSOT §3 / contract wake policy.
    idle_fire_after_seconds: int = 300

    # delta
    stale_escalation_thresholds_seconds: tuple[int, ...] = (3600, 86400)

    # limits (deterministic truncation: drop lowest band/tier preview detail
    # first; counts + pull instructions are never dropped)
    max_envelopes_per_provider: int = 8
    max_summary_bytes: int = 512
    max_digest_bytes: int = 16384
    max_preview_items_per_envelope: int = 5

    # failure
    provider_timeout_seconds: int = 5
    max_consecutive_failures: int = 3

    # trust
    third_party_default_tier: int = 6

    @staticmethod
    def default() -> "Policy":
        """The production policy with env overrides applied to the
        test-overridable knobs."""
        base = Policy()
        return replace(
            base,
            idle_fire_after_seconds=_env_int(
                "MOLECULE_IDLE_FIRE_SECONDS", base.idle_fire_after_seconds, minimum=1
            ),
            max_envelopes_per_provider=_env_int(
                "MOLECULE_IDLE_MAX_ENVELOPES_PER_PROVIDER",
                base.max_envelopes_per_provider,
            ),
            max_summary_bytes=_env_int(
                "MOLECULE_IDLE_MAX_SUMMARY_BYTES", base.max_summary_bytes, minimum=16
            ),
            max_digest_bytes=_env_int(
                "MOLECULE_IDLE_MAX_DIGEST_BYTES", base.max_digest_bytes, minimum=256
            ),
            max_preview_items_per_envelope=_env_int(
                "MOLECULE_IDLE_MAX_PREVIEW_ITEMS",
                base.max_preview_items_per_envelope,
            ),
            provider_timeout_seconds=_env_int(
                "MOLECULE_IDLE_PROVIDER_TIMEOUT_SECONDS",
                base.provider_timeout_seconds,
            ),
        )
