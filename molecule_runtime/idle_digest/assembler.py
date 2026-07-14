"""The idle-digest assembler engine — pure functions, no I/O.

This is the canonical assembler *implementation* the contract's
``ownership.assembler_owner`` names (molecule_runtime). Everything here is a
pure function of its inputs so it is exhaustively unit-testable without a
runtime, a lease, or a live tenant (the seam study's rule: build the assembler
as testable module-level functions). The idle *controller*
(controller.py) wires these into the boot loop — native default-ON since
2026-07-13 (``MOLECULE_MAILBOX_KERNEL=0`` opts out); this module
never touches the loop, the network, or the filesystem.

Responsibilities encoded here (all per the design SSOT + contract):
  * hashing — SHA-256 over the fixed 7-field tuple (band excluded);
  * delta   — signature + fire decision with failed-section exclusion so a
              crash/recover cannot spuriously re-fire;
  * sort    — pinned band, then urgent band above all base tiers, then base
              tiers ascending (NOT lexicographic (tier, urgency));
  * empty   — the pinned header never counts toward emptiness;
  * limits  — per-provider envelope cap, summary byte cap, preview-item cap,
              and deterministic digest-budget truncation (drop lowest
              band/tier preview detail first; never drop counts or pulls);
  * trust   — band=pinned is rejected for any provider but the reserved id.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from .contract import (
    PINNED_PROVIDER_ID,
    RESERVED_PROVIDER_IDS,
    Band,
    Contribution,
    Policy,
    Urgency,
)

# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def _hash_payload(c: Contribution) -> str:
    """Canonical serialization of the 7 hashed fields, in the pinned order.
    Deterministic JSON (sorted keys off — we control key order via the list)
    so the same contribution always serializes identically across processes."""
    payload = [
        c.provider_id,
        c.tier,
        c.urgency.value,
        c.count,
        c.summary,
        c.age_band.value,
        list(c.item_ids),
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def content_hash(c: Contribution) -> str:
    """The ``sha256:<hex>`` content hash of a contribution. ``band``, ``pull``,
    ``preview_items`` and ``rearm_signals`` are deliberately NOT hashed — band
    is derived from provider_id + urgency, and the rest are render/plumbing
    detail that must not by itself re-fire the digest."""
    digest = hashlib.sha256(_hash_payload(c).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def signature(contributions: list[Contribution]) -> tuple[tuple[str, str], ...]:
    """The baseline representation persisted at each fire: a sorted tuple of
    ``(provider_id, content_hash)`` pairs over the given (successfully
    assembled) contributions. Sorted so ordering never affects equality."""
    return tuple(sorted((c.provider_id, content_hash(c)) for c in contributions))


def should_fire(
    current: list[Contribution],
    baseline: tuple[tuple[str, str], ...],
    failed_provider_ids: frozenset[str] = frozenset(),
) -> bool:
    """True iff the digest content changed since the last fire, restricting to
    providers that did NOT fail this tick. A provider that errored this tick is
    excluded from BOTH sides of the comparison so a crash (its hash vanishes)
    and the subsequent recover (its hash returns unchanged) neither re-fire —
    ``failed_sections_excluded_from_hash`` in the contract. Both sides are
    filtered by ``failed_provider_ids`` so the exclusion is symmetric: a failed
    provider normally produces no ``current`` contribution, but filtering
    ``current`` too keeps the comparison correct even if a caller passes a
    stale/partial contribution for a provider it also reports as failed."""
    cur = tuple(p for p in signature(current) if p[0] not in failed_provider_ids)
    base = tuple(p for p in baseline if p[0] not in failed_provider_ids)
    return tuple(sorted(cur)) != tuple(sorted(base))


# ---------------------------------------------------------------------------
# sort + empty
# ---------------------------------------------------------------------------

_BAND_RANK = {Band.PINNED: 0, Band.URGENT: 1, Band.BASE: 2}


def sort_contributions(contributions: list[Contribution]) -> list[Contribution]:
    """Render order: pinned band, then urgent band above ALL base tiers, then
    base tiers ascending; tier-ascending within each band. Python's sort is
    stable, so equal (band, tier) keeps registration order."""
    return sorted(contributions, key=lambda c: (_BAND_RANK[c.band], c.tier))


def body_is_empty(contributions: list[Contribution]) -> bool:
    """True iff there is no non-pinned (body) contribution. The pinned header
    never counts toward emptiness — a header-only digest is empty and must not
    fire (the controller sleeps)."""
    return not any(c.band is not Band.PINNED for c in contributions)


# ---------------------------------------------------------------------------
# trust / validation
# ---------------------------------------------------------------------------


class ContributionError(ValueError):
    """A contribution violates a load-bearing assembler invariant."""


def validate(c: Contribution) -> None:
    """Structural + per-envelope trust checks. NOTE: the *reserved provider id*
    rule (``trust.reserved_provider_ids``) cannot be enforced here — a
    contribution alone does not reveal whether its registrant is an official
    platform provider or a third-party plugin. That check is
    :func:`check_reserved_id`, applied at registration where trust is known
    (the :class:`ProviderRunner` calls it before invoking a provider)."""
    if c.band is Band.PINNED and c.provider_id != PINNED_PROVIDER_ID:
        raise ContributionError(
            f"band=pinned is reserved to {PINNED_PROVIDER_ID!r}, "
            f"not {c.provider_id!r}"
        )
    if c.band is Band.URGENT and c.urgency is not Urgency.URGENT:
        raise ContributionError(
            f"{c.provider_id!r}: band=urgent requires urgency=urgent"
        )
    if c.count < 0:
        raise ContributionError(f"{c.provider_id!r}: count must be >= 0")
    if c.tier < 0:
        raise ContributionError(f"{c.provider_id!r}: tier must be >= 0")


def check_reserved_id(provider_id: str, *, official: bool) -> None:
    """Enforce ``trust.reserved_provider_ids``: an official provider id
    (``identity-capabilities``, ``task-queue``, ``goal-state``, …) may be used
    ONLY by an official platform-owned provider. A third-party (plugin)
    provider claiming a reserved id is rejected so it cannot spoof a
    system-owned digest section. Applied at registration; providers are treated
    as third-party (``official=False``) unless they explicitly declare
    otherwise, so the default fails safe toward untrusted."""
    if not official and provider_id in RESERVED_PROVIDER_IDS:
        raise ContributionError(
            f"provider id {provider_id!r} is reserved to an official provider; "
            f"a third-party provider may not register it"
        )


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` so its UTF-8 encoding is <= ``max_bytes``, never
    splitting a multi-byte codepoint. Appends a single-char ellipsis when it
    actually cut something (the ellipsis is counted in the budget)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    ell = "…"  # …
    budget = max(0, max_bytes - len(ell.encode("utf-8")))
    cut = encoded[:budget]
    # back off to a codepoint boundary
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore") + ell


def enforce_envelope_limits(c: Contribution, policy: Policy) -> Contribution:
    """Cap a single envelope's summary bytes and preview-item count. Counts and
    the pull instruction are never dropped."""
    summary = _truncate_utf8(c.summary, policy.max_summary_bytes)
    previews = c.preview_items[: policy.max_preview_items_per_envelope]
    if summary == c.summary and previews == c.preview_items:
        return c
    return replace(c, summary=summary, preview_items=previews)


def cap_envelopes_per_provider(
    contributions: list[Contribution], policy: Policy
) -> list[Contribution]:
    """Keep at most ``max_envelopes_per_provider`` envelopes per provider,
    preserving the assembler's sort order and dropping the lowest-priority
    (last, after sort) overflow envelopes."""
    seen: dict[str, int] = {}
    kept: list[Contribution] = []
    for c in contributions:
        n = seen.get(c.provider_id, 0)
        if n < policy.max_envelopes_per_provider:
            kept.append(c)
            seen[c.provider_id] = n + 1
    return kept


# ---------------------------------------------------------------------------
# render + budget truncation
# ---------------------------------------------------------------------------

_BAND_HEADING = {Band.URGENT: "⚡ URGENT", Band.BASE: None}


def render(contributions: list[Contribution]) -> str:
    """Render the ordered contributions to the plain-text digest body. Pinned
    header first (no heading), then the urgent band under a heading, then base
    tiers. Ages render as bands, never raw durations."""
    lines: list[str] = []
    last_band: Band | None = None
    for c in contributions:
        if c.band is not last_band:
            heading = _BAND_HEADING.get(c.band) if c.band is not Band.PINNED else None
            if heading:
                lines.append("")
                lines.append(heading)
            elif c.band is Band.BASE and last_band is not None:
                lines.append("")
            last_band = c.band
        lines.append(f"• {c.summary}")
        for item in c.preview_items:
            suffix = f" → {item.next_action}" if item.next_action else ""
            lines.append(f"    - {item.summary} [{item.age_band.value}]{suffix}")
        if c.pull is not None:
            lines.append(f"    pull: {c.pull.tool}")
    return "\n".join(lines).strip("\n")


def _digest_bytes(contributions: list[Contribution]) -> int:
    return len(render(contributions).encode("utf-8"))


def truncate_to_budget(
    contributions: list[Contribution], policy: Policy
) -> list[Contribution]:
    """Deterministic digest-budget truncation: while the rendered digest
    exceeds ``max_digest_bytes``, strip preview_items from the LOWEST band/tier
    envelope upward. Counts, summaries and pull instructions are never dropped —
    only the optional preview detail. ``contributions`` must already be sorted.

    If stripping all previews still overflows, the digest is returned as-is
    (counts + summaries + pulls are the irreducible floor); the caller/renderer
    surfaces that the digest hit its floor."""
    work = list(contributions)
    if _digest_bytes(work) <= policy.max_digest_bytes:
        return work
    # Walk lowest-priority first (end of the sorted list), stripping previews.
    for i in range(len(work) - 1, -1, -1):
        if work[i].preview_items:
            work[i] = replace(work[i], preview_items=())
            if _digest_bytes(work) <= policy.max_digest_bytes:
                return work
    return work


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssembledDigest:
    """The result of assembling a set of raw contributions."""

    contributions: list[Contribution]
    text: str
    signature: tuple[tuple[str, str], ...]
    is_empty: bool


def assemble(raw: list[Contribution], policy: Policy) -> AssembledDigest:
    """Validate, limit, sort, and budget-truncate a set of raw contributions
    into a rendered digest + its delta signature. Pure — the fire decision and
    the on_included/baseline recompute are the controller's job (see
    :func:`should_fire` / :func:`signature`)."""
    for c in raw:
        validate(c)
    limited = [enforce_envelope_limits(c, policy) for c in raw]
    ordered = sort_contributions(limited)
    ordered = cap_envelopes_per_provider(ordered, policy)
    ordered = truncate_to_budget(ordered, policy)
    return AssembledDigest(
        contributions=ordered,
        text=render(ordered),
        signature=signature(ordered),
        is_empty=body_is_empty(ordered),
    )
