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

Delta hygiene: counts and overdue delegation-id sets ARE the signal — they go
into ``item_ids`` so the digest re-fires when mail state actually changes and
stays silent otherwise. Ages appear ONLY as :class:`AgeBand` enums (assembler
rule: raw ages resurrect the steady-state nag loop).
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..contract import AgeBand, Band, Contribution, PullInstruction, Urgency


class CommsSummaryUnavailable(RuntimeError):
    """The comms source could not produce a summary THIS tick.

    Providers let this propagate: the ProviderRunner catches it, marks the
    section FAILED, and the assembler excludes failed sections from the delta
    hash SYMMETRICALLY (contract failed_sections_excluded_from_hash) — a
    transient outage neither fires the digest nor suppresses it, and the
    degraded marker renders. Returning an empty summary instead would conflate
    "comms down" with "no mail" and fire spuriously on the blip + recovery.
    """


INBOUND_PROVIDER_ID = "inbound-a2a"
INBOUND_TIER = 3  # contract providers[3].base_tier
SENT_PROVIDER_ID = "sent-folder"
SENT_TIER = 2  # contract providers[2].base_tier

#: Default no-reply age (seconds) beyond which a sent delegation is flagged
#: "target agent may have an issue" (matches the server default and the
#: delegations hard-deadline default).
DEFAULT_OVERDUE_AFTER_SECONDS = 21600

#: How many overdue targets the summary names inline (detail via pull).
_OVERDUE_NAMES_CAP = 3


@dataclass
class MailSummary:
    """The tenant mail-summary API response, typed."""

    received_unread: int = 0
    replies_unread: int = 0
    sent_awaiting_reply: int = 0
    overdue: tuple = ()  # of dict: delegation_id, target_workspace_id, age_seconds
    overdue_after_seconds: int = DEFAULT_OVERDUE_AFTER_SECONDS


class CommsSummarySource(Protocol):
    """The comms abstraction the mail providers depend on.

    THE plugin seam: today the one implementation reads the platform
    mail-summary API; a future communication-layer plugin provides its own
    implementation and is bound in ``build_default_providers`` — nothing else
    changes. ``fetch()`` returns a summary, or raises
    :class:`CommsSummaryUnavailable` when the source cannot answer THIS tick —
    never an empty summary for an outage (failed-exclusion symmetry).
    """

    async def fetch(self) -> MailSummary:  # pragma: no cover - protocol
        ...


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
            try:
                data = await asyncio.get_running_loop().run_in_executor(None, self._get)
            except Exception as exc:
                raise CommsSummaryUnavailable(str(exc)) from exc
            if data is None:
                raise CommsSummaryUnavailable("mail summary: non-object response")
            summary = MailSummary(
                received_unread=int(data.get("received_unread", 0) or 0),
                replies_unread=int(data.get("replies_unread", 0) or 0),
                sent_awaiting_reply=int(data.get("sent_awaiting_reply", 0) or 0),
                overdue=tuple(data.get("overdue") or ()),
                overdue_after_seconds=int(
                    data.get("overdue_after_seconds", DEFAULT_OVERDUE_AFTER_SECONDS)
                    or DEFAULT_OVERDUE_AFTER_SECONDS
                ),
            )
            self._cached, self._cached_at = summary, now
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


@dataclass
class InboundMailProvider:
    """``inbound-a2a`` (tier 3): unread received + unread replies, as counts."""

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
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.BASE,
                tier=INBOUND_TIER,
                urgency=Urgency.NORMAL,
                count=s.received_unread + s.replies_unread,
                summary=summary,
                age_band=AgeBand.NONE,
                # counts ARE the delta signal: a change re-fires the digest,
                # steady state stays silent.
                item_ids=(f"recv:{s.received_unread}", f"replies:{s.replies_unread}"),
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
        hours = max(1, s.overdue_after_seconds // 3600)
        summary = f"You have {s.sent_awaiting_reply} sent message(s) awaiting a reply."
        if overdue:
            names = ", ".join(
                str(o.get("target_workspace_id", "?"))[:8] for o in overdue[:_OVERDUE_NAMES_CAP]
            )
            more = "…" if len(overdue) > _OVERDUE_NAMES_CAP else ""
            summary += (
                f" ⚠ {len(overdue)} sent >{hours}h with no reply — the target agent"
                f" may have an issue ({names}{more})."
            )
        summary += " Use the Molecules AI workspace communication MCP to see detail."
        oldest_age = max((int(o.get("age_seconds", 0) or 0) for o in overdue), default=0)
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.URGENT if overdue else Band.BASE,
                tier=SENT_TIER,
                # D2: an overdue no-reply jumps the tier queue.
                urgency=Urgency.URGENT if overdue else Urgency.NORMAL,
                count=s.sent_awaiting_reply,
                summary=summary,
                age_band=_age_band_for_seconds(oldest_age) if overdue else AgeBand.NONE,
                # awaiting count + the overdue id SET are the delta signal; the
                # ids keep the digest firing when a DIFFERENT delegation goes
                # overdue even if the count happens to match.
                item_ids=tuple(
                    [f"awaiting:{s.sent_awaiting_reply}"]
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
