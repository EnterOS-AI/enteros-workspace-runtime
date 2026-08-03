"""DB-backed mail providers — ``inbound-a2a`` + ``sent-folder`` (task #219 phase-2, D5).

D5 ruling (CTO 2026-07-14): since the mailbox kernel is native, SENT and
RECEIVED are **workspace-DB state**. These providers are thin READERS of the
tenant mail-summary API (core#4308: ``GET /workspaces/:id/mail/summary``)
rendering COUNTS + a pull instruction — never message bodies, never a second
file-based store (the no-duplicate-platform-ledger-SSOT rule). The digest line
is the CTO-specified shape:

    You have {n} received, {n} replies not yet read, {n} sent awaiting reply.
    ⚠ {k} sent >6h with no reply — the target agent may have an issue.
    Use the Molecules AI workspace communication MCP to see detail.

PLUGIN-SHAPED SEAM (CTO 2026-07-14): the communication layer will later move
behind the plugin boundary (not now). Both providers therefore depend ONLY on
the :class:`CommsSummarySource` protocol; :class:`PlatformMailSummarySource`
is the default BINDING, injected at assembly (``build_default_providers``).
When the comms layer becomes a plugin, its implementation replaces the binding
in one place — the providers, their contract envelopes (tiers 2/3, reserved
ids), and their tests do not move.

Delta hygiene: counts AND per-item identity sets are the signal — both go into
``item_ids`` so the digest re-fires when mail state actually changes and stays
silent otherwise. Ages appear ONLY as :class:`AgeBand` enums (assembler rule:
raw ages resurrect the steady-state nag loop).

A COUNT IS NOT AN IDENTITY (core#5028). ``item_ids`` feeds the assembler's
content hash, so whatever is in that tuple is what the digest can tell apart.
The inbound provider used to put ONLY counts there, which made the change
detector a hash of a number: if the agent reads one message and one new one
arrives between two ticks, the count is unchanged, the hash is unchanged, and a
brand-new inbound is NEVER surfaced. The sent provider already carried real
per-delegation ids beside its count for exactly this reason; both halves now do.
Counts stay in the tuple — they catch a change the server-capped id sample can
miss — but they are no longer the whole identity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from ..contract import AgeBand, Band, Contribution, PullInstruction, Urgency

logger = logging.getLogger(__name__)

# D3 mail-plugin seam precursor: the stable comms contract now lives in
# ``molecule_runtime.idle_digest.comms`` so the mail plugin (which owns the
# provider render source at v0.2.0) can import it from a path that survives the
# eventual delete of this module's render half. Imported + re-exported here so
# every existing importer of ``providers.mail`` (controller.build_default_providers,
# tests/test_idle_provider_mail.py, tests/test_idle_provider_plugin_loader.py)
# and ``PlatformMailSummarySource`` below stay byte-behaviour-identical.
from ..comms import (  # noqa: F401  (re-exported for backwards-compatible import path)
    DEFAULT_OVERDUE_AFTER_SECONDS,
    CommsSummarySource,
    CommsSummaryUnavailable,
    MailSummary,
)

__all__ = [
    "CommsSummaryUnavailable",
    "CommsSummarySource",
    "MailSummary",
    "DEFAULT_OVERDUE_AFTER_SECONDS",
    "PlatformMailSummarySource",
    "InboundMailProvider",
    "SentMailProvider",
    "INBOUND_PROVIDER_ID",
    "INBOUND_TIER",
    "SENT_PROVIDER_ID",
    "SENT_TIER",
]

INBOUND_PROVIDER_ID = "inbound-a2a"
INBOUND_TIER = 3  # contract providers[3].base_tier
SENT_PROVIDER_ID = "sent-folder"
SENT_TIER = 2  # contract providers[2].base_tier

#: How many overdue targets the summary names inline (detail via pull).
_OVERDUE_NAMES_CAP = 3


@dataclass
class PlatformMailSummarySource:
    """Default binding: the tenant mail-summary read API (core#4308).

    One GET per digest tick shared by BOTH providers (tiny TTL cache) — the
    ProviderRunner invokes each provider separately but mail state must not be
    fetched twice per tick (or worse, diverge between the two envelopes).
    """

    platform_url: str
    workspace_id: str
    timeout_seconds: float = 4.0
    cache_ttl_seconds: float = 5.0
    # injected seams (tests)
    now_fn: callable = time.time
    _cached: Optional[MailSummary] = field(default=None, init=False)
    #: the failure memoized for THIS tick (see fetch) — so both providers see
    #: one outcome, not one success and one failure.
    _error: Optional[str] = field(default=None, init=False)
    _cached_at: float = field(default=0.0, init=False)
    _lock: Optional[asyncio.Lock] = field(default=None, init=False)

    async def fetch(self) -> MailSummary:
        # SINGLE-FLIGHT: ProviderRunner gathers providers CONCURRENTLY, so both
        # mail providers race a cold cache — without the lock that is two HTTP
        # GETs per tick and possibly DIVERGENT envelopes. The lock is created
        # lazily on the running loop (the dataclass is built outside any loop).
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = self.now_fn()
            if self._cached is not None and (now - self._cached_at) < self.cache_ttl_seconds:
                return self._cached
            # FAILURES ARE MEMOIZED TOO, and that is the whole point of the lock.
            #
            # Memoizing only successes made the lock half a fix: on a FAILING
            # tick the first provider raised and left the cache empty, so the
            # second provider re-acquired and issued a SECOND GET. During a
            # rolling deploy (502 then 200) that produced exactly the divergence
            # the contract forbids — sent-folder marked failed and excluded from
            # the delta hash while inbound-a2a rendered a full envelope — and
            # then a spurious re-fire on the next tick when sent-folder's hash
            # reappeared unchanged. It also doubled the GETs (2 x 4s timeouts)
            # inside the runner's 5s per-provider budget on a hard outage.
            #
            # One outcome per tick, shared by both providers — success or
            # failure, symmetrically.
            if self._error is not None and (now - self._cached_at) < self.cache_ttl_seconds:
                raise CommsSummaryUnavailable(self._error)
            try:
                data = await asyncio.get_running_loop().run_in_executor(None, self._get)
                if data is None:
                    raise CommsSummaryUnavailable("mail summary: non-object response")
                summary = MailSummary(
                    received_unread=int(data.get("received_unread", 0) or 0),
                    replies_unread=int(data.get("replies_unread", 0) or 0),
                    # core#5028: ABSENT (old server) must stay None; PRESENT but
                    # empty must become (). `data.get("received") or ()` would
                    # collapse both to () and make the skew undetectable — which
                    # is precisely the shape of mistake that let the
                    # overdue_count fallback run silently forever.
                    received=(
                        tuple(data["received"] or ())
                        if isinstance(data.get("received"), list)
                        else None
                    ),
                    sent_awaiting_reply=int(data.get("sent_awaiting_reply", 0) or 0),
                    overdue=tuple(data.get("overdue") or ()),
                    overdue_count=int(
                        # The TRUE uncapped total. Falls back to the list length
                        # only for a server that predates the field — rendering
                        # len(list) is what under-reported 25 overdue as "10".
                        # The server DOES emit it as of core#5028; before that
                        # this fallback was permanent, not vestigial.
                        data.get("overdue_count", len(data.get("overdue") or ()))
                        or 0
                    ),
                    overdue_after_seconds=int(
                        data.get("overdue_after_seconds", DEFAULT_OVERDUE_AFTER_SECONDS)
                        or DEFAULT_OVERDUE_AFTER_SECONDS
                    ),
                )
            except Exception as exc:
                self._cached, self._error, self._cached_at = None, str(exc), now
                raise CommsSummaryUnavailable(str(exc)) from exc
            self._cached, self._error, self._cached_at = summary, None, now
            return summary

    def _get(self) -> Optional[dict]:
        from molecule_runtime.platform_auth import auth_headers

        url = f"{self.platform_url}/workspaces/{self.workspace_id}/mail/summary"
        req = urllib.request.Request(url, headers=auth_headers())
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read()
        loaded = json.loads(body)
        return loaded if isinstance(loaded, dict) else None


def _age_band_for_seconds(age: int) -> AgeBand:
    if age < 3600:
        return AgeBand.UNDER_1H
    if age < 86400:
        return AgeBand.ONE_H_TO_1D
    return AgeBand.OVER_1D


#: The item_ids marker emitted when the server did not project per-message
#: identities. It rides the delta hash ON PURPOSE: it is a real change in what
#: the digest can see, and when the server is finally upgraded the marker
#: disappearing re-fires the digest once — which is correct, the section's
#: information content just changed.
_RECV_IDENTITY_UNAVAILABLE = "recv-identity:UNAVAILABLE"


def _received_item_ids(received: tuple) -> list[str]:
    """The per-message identity half of the inbound delta signal (core#5028).

    Prefers the a2a ``message_id`` (the id a tenant-side reconciler correlates
    against its own ledger) and falls back to the server row ``id``, which is
    ALWAYS present. Falling back to the count for an id-less message would
    reintroduce the defect for exactly those messages. SORTED, so the server's
    newest-first ordering is not itself a change signal.
    """
    ids = []
    for m in received:
        if not isinstance(m, dict):
            continue
        ident = str(m.get("message_id") or m.get("id") or "").strip()
        if ident:
            ids.append(f"msg:{ident}")
    return sorted(set(ids))


@dataclass
class InboundMailProvider:
    """``inbound-a2a`` (tier 3): unread received + unread replies.

    The digest LINE is counts (D5, unchanged). The digest's change-detection
    IDENTITY is per-message ids (core#5028) — those are different jobs and
    conflating them is the bug this provider used to have.
    """

    source: CommsSummarySource
    provider_id: str = field(default=INBOUND_PROVIDER_ID, init=False)
    official: bool = field(default=True, init=False)

    async def contribute(self) -> list[Contribution]:
        # An unavailable source RAISES (CommsSummaryUnavailable) — the runner
        # marks the section failed and the hash excludes it symmetrically.
        s = await self.source.fetch()
        if s is None:
            raise CommsSummaryUnavailable("comms source returned None")
        if s.received_unread == 0 and s.replies_unread == 0:
            return []
        parts = []
        if s.received_unread:
            parts.append(f"{s.received_unread} received message(s) unread")
        if s.replies_unread:
            parts.append(f"{s.replies_unread} repl{'y' if s.replies_unread == 1 else 'ies'} not yet read")
        summary = (
            "You have " + " and ".join(parts) + ". "
            "Use the Molecules AI workspace communication MCP to see detail."
        )
        # THE core#5028 FIX. The counts stay in item_ids — they catch a change
        # the server-capped id sample can miss — but they are no longer the
        # WHOLE identity. A count alone made the digest's change-detection hash
        # a hash of a number: read one message, receive one new one between two
        # ticks, and the count (so the hash) is unchanged and a brand-new
        # inbound is NEVER surfaced. This mirrors the sent side below, which has
        # carried real per-item ids next to its count for exactly this reason.
        count_ids = [f"recv:{s.received_unread}", f"replies:{s.replies_unread}"]
        received = getattr(s, "received", None)
        if received is None:
            # VERSION SKEW: the server did not project identities at all. Fall
            # back to today's count-only behaviour rather than crashing — but
            # LOUDLY. `overdue_count` is the cautionary tale: the runtime has
            # carried a "only for a server that predates the field" fallback
            # since #4308, the server never emitted the field, and the silent
            # permanent fallback let everyone believe the bug was fixed while
            # it was still live. A degraded mode nobody can see is not a
            # degraded mode, it is an undetected outage.
            logger.warning(
                "idle-digest inbound-a2a: mail summary carries NO per-message "
                "identities (`received` absent) — falling back to COUNT-ONLY "
                "change detection. A read + a new arrival between ticks will "
                "not re-fire the digest (core#5028). Upgrade workspace-server."
            )
            item_ids = tuple(count_ids + [_RECV_IDENTITY_UNAVAILABLE])
        else:
            item_ids = tuple(count_ids + _received_item_ids(received))
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.BASE,
                tier=INBOUND_TIER,
                urgency=Urgency.NORMAL,
                count=s.received_unread + s.replies_unread,
                summary=summary,
                age_band=AgeBand.NONE,
                item_ids=item_ids,
                pull=PullInstruction(
                    tool="inbox_peek",
                    instruction="Peek the unread inbox (received messages + delegation replies) before acting.",
                    max_items=10,
                ),
            )
        ]

    def on_included(self, fired_at: float) -> None:  # pragma: no cover - no state
        return


@dataclass
class SentMailProvider:
    """``sent-folder`` (tier 2): sends awaiting reply, with the D2 urgency jump
    when one is overdue (default >6h) — "the target agent may have an issue"."""

    source: CommsSummarySource
    provider_id: str = field(default=SENT_PROVIDER_ID, init=False)
    official: bool = field(default=True, init=False)

    async def contribute(self) -> list[Contribution]:
        s = await self.source.fetch()
        if s is None:
            raise CommsSummaryUnavailable("comms source returned None")
        if s.sent_awaiting_reply == 0:
            return []
        overdue = list(s.overdue)
        # The COUNT is the uncapped total; `overdue` is only a capped SAMPLE used
        # to name a few offenders. Rendering len(overdue) under-reports the blast
        # radius (25 overdue shown as "10") and freezes the delta signal once the
        # cap saturates, so an 11th delegation going overdue changes nothing.
        overdue_total = s.overdue_count or len(overdue)
        hours = max(1, s.overdue_after_seconds // 3600)
        summary = f"You have {s.sent_awaiting_reply} sent message(s) awaiting a reply."
        if overdue_total:
            names = ", ".join(
                str(o.get("target_workspace_id", "?"))[:8] for o in overdue[:_OVERDUE_NAMES_CAP]
            )
            more = "…" if overdue_total > len(overdue[:_OVERDUE_NAMES_CAP]) else ""
            summary += (
                f" ⚠ {overdue_total} sent >{hours}h with no reply — the target agent"
                f" may have an issue ({names}{more})."
            )
        summary += " Use the Molecules AI workspace communication MCP to see detail."
        oldest_age = max((int(o.get("age_seconds", 0) or 0) for o in overdue), default=0)
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.URGENT if overdue_total else Band.BASE,
                tier=SENT_TIER,
                # D2: an overdue no-reply jumps the tier queue.
                urgency=Urgency.URGENT if overdue_total else Urgency.NORMAL,
                count=s.sent_awaiting_reply,
                summary=summary,
                age_band=_age_band_for_seconds(oldest_age) if overdue else AgeBand.NONE,
                # awaiting count + the overdue id SET are the delta signal; the
                # ids keep the digest firing when a DIFFERENT delegation goes
                # overdue even if the count happens to match.
                # The uncapped total rides the delta signal alongside the id
                # sample, so an 11th delegation going overdue still re-fires the
                # digest even though the capped id set is unchanged.
                item_ids=tuple(
                    [f"awaiting:{s.sent_awaiting_reply}", f"overdue_n:{overdue_total}"]
                    + sorted(f"overdue:{o.get('delegation_id','?')}" for o in overdue)
                ),
                pull=PullInstruction(
                    tool="check_task_status",
                    instruction="Check the overdue delegation(s) by id; consider re-delegating or escalating if the target is wedged.",
                    max_items=10,
                ),
            )
        ]

    def on_included(self, fired_at: float) -> None:  # pragma: no cover - no state
        return
