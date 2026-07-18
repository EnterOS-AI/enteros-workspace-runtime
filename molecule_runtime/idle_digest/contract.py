"""Idle-digest contract vocabulary + policy — the runtime mirror of the SDK
``contracts/idle-prompt`` layer (molecule-ai-sdk#57, task #219).

This module holds the *shape* the assembler works with: the contribution
envelope, its enum vocabularies, and the assembler ``Policy`` (wake / sort /
delta / empty / limits / failure knobs). Values default to the operator-ruled
contract instance; the test-overridable knobs (idle-fire threshold, limits,
provider timeout) read an env override so tests and staged tenants can retune
without editing code — exactly the "contract values are production defaults"
framing the contract pins.

:func:`Policy.default` loads the production values from the **vendored contract
instance** (``molecule_runtime/contracts/idle-prompt.contract.json`` — a
byte-for-byte mirror of molecule-ai-sdk's, kept honest by the
``scripts/check-schemas-in-sync.sh`` drift gate; same offline-vendoring pattern
``manifest_ssot.py`` uses for plugin-manifest). The ``Policy`` field defaults
below remain a hardcoded MIRROR of those SSOT values — the fail-safe fallback if
the vendored instance is ever missing/unreadable (loud-degrade, never crash),
and a fixture the tests assert stays equal to the instance so the mirror can't
silently drift. :data:`CONTRACT_SCHEMA_VERSION` pins the contract revision.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from enum import Enum
from importlib import resources
from typing import Optional

logger = logging.getLogger(__name__)

CONTRACT_SCHEMA_VERSION = "idle-prompt/v1"

# The vendored contract INSTANCE (byte-for-byte mirror of molecule-ai-sdk's
# contracts/idle-prompt/idle-prompt.contract.json — see contracts/PROVENANCE.md
# and the check-schemas-in-sync.sh drift gate). Policy.default() loads the
# operator-ruled production values from it; the resource path is relative to the
# molecule_runtime package root and ships in the wheel via package-data.
_CONTRACT_INSTANCE_RESOURCE = "contracts/idle-prompt.contract.json"
_contract_load_failed_logged = False


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
    def from_contract_instance() -> Optional["Policy"]:
        """Build a Policy from the vendored contract instance (the SSOT for the
        production values). Returns None — logging once — on any missing/malformed
        instance, so :func:`default` falls back to the hardcoded mirror rather
        than crashing boot (the manifest_ssot loud-degrade posture)."""
        global _contract_load_failed_logged
        try:
            raw = (
                resources.files("molecule_runtime")
                .joinpath(_CONTRACT_INSTANCE_RESOURCE)
                .read_text(encoding="utf-8")
            )
            data = json.loads(raw)
            wake, delta = data["wake"], data["delta"]
            limits, failure, trust = data["limits"], data["failure"], data["trust"]
            return Policy(
                idle_fire_after_seconds=int(wake["idle_fire_after_seconds"]),
                stale_escalation_thresholds_seconds=tuple(
                    int(x) for x in delta["stale_escalation_thresholds_seconds"]
                ),
                max_envelopes_per_provider=int(limits["max_envelopes_per_provider"]),
                max_summary_bytes=int(limits["max_summary_bytes"]),
                max_digest_bytes=int(limits["max_digest_bytes"]),
                max_preview_items_per_envelope=int(
                    limits["max_preview_items_per_envelope"]
                ),
                provider_timeout_seconds=int(failure["provider_timeout_seconds"]),
                max_consecutive_failures=int(failure["max_consecutive_failures"]),
                third_party_default_tier=int(trust["third_party_default_tier"]),
            )
        except Exception:  # noqa: BLE001 — never crash boot on a bad/missing mirror
            if not _contract_load_failed_logged:
                logger.error(
                    "idle-digest: could not load policy from the vendored contract "
                    "instance %s — falling back to the hardcoded mirror. Re-vendor "
                    "per molecule_runtime/contracts/PROVENANCE.md.",
                    _CONTRACT_INSTANCE_RESOURCE,
                )
                _contract_load_failed_logged = True
            return None

    @staticmethod
    def default() -> "Policy":
        """The production policy: values from the vendored contract instance
        (SSOT), the hardcoded mirror as fail-safe fallback, then env overrides
        applied to the test-overridable knobs."""
        base = Policy.from_contract_instance() or Policy()
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


# ---------------------------------------------------------------------------
# self-prompt framing
# ---------------------------------------------------------------------------

# Title line prepended to every self-initiated idle/digest prompt at the
# POSTER (transport) layer — it frames the message in the agent's context and
# in traces as a system-generated consolidation, not user input. Deliberately
# NOT part of any Contribution: contributions are hashed for the fire/delta
# decision and a static banner must not affect it.
SYSTEM_IDLE_HEADER = "<SYSTEM IDLE PROMPT>"


def frame_idle_prompt(text: str) -> str:
    """Prepend the system-idle title to a rendered digest / idle prompt.

    Idempotent (a text already carrying the header is returned unchanged) so
    a retried or double-framed post can never stack banners.
    """
    body = text or ""
    if body.lstrip().startswith(SYSTEM_IDLE_HEADER):
        return body
    return f"{SYSTEM_IDLE_HEADER}\n{body}"
