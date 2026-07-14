"""Mail providers (task #219 phase-2, D5) — inbound-a2a + sent-folder over the
CommsSummarySource plugin seam.

Pins the D5 contract: counts + pull instruction (never bodies), the D2 urgency
jump on an overdue no-reply, count/id-set delta hygiene (no raw ages), the
source-unavailable skip, and the seam itself (a fake source drops in with zero
provider changes — the property the future comms-layer plugin relies on).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime.idle_digest.contract import AgeBand, Band, Urgency  # noqa: E402
from molecule_runtime.idle_digest.controller import build_default_providers  # noqa: E402
from molecule_runtime.idle_digest.providers.mail import (  # noqa: E402
    CommsSummaryUnavailable,
    InboundMailProvider,
    MailSummary,
    PlatformMailSummarySource,
    SentMailProvider,
)


class _FakeSource:
    """A stand-in comms plugin — the seam contract is just `fetch()`."""

    def __init__(self, summary):
        self.summary = summary
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        return self.summary


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_inbound_counts_render_and_delta_ids():
    s = MailSummary(received_unread=3, replies_unread=1)
    [c] = _run(InboundMailProvider(source=_FakeSource(s)).contribute())
    assert c.provider_id == "inbound-a2a" and c.tier == 3
    assert "3 received message(s) unread" in c.summary
    assert "1 reply not yet read" in c.summary
    assert "workspace communication MCP" in c.summary
    assert c.count == 4 and c.urgency == Urgency.NORMAL
    # counts are the delta signal — and no raw ages anywhere
    assert c.item_ids == ("recv:3", "replies:1")
    assert c.age_band == AgeBand.NONE
    assert c.pull.tool == "inbox_peek"


def test_inbound_empty_contributes_nothing():
    assert _run(InboundMailProvider(source=_FakeSource(MailSummary())).contribute()) == []


def test_sent_awaiting_normal_until_overdue():
    s = MailSummary(sent_awaiting_reply=2)
    [c] = _run(SentMailProvider(source=_FakeSource(s)).contribute())
    assert c.provider_id == "sent-folder" and c.tier == 2
    assert "2 sent message(s) awaiting a reply" in c.summary
    assert "may have an issue" not in c.summary
    assert c.urgency == Urgency.NORMAL and c.band == Band.BASE


def test_sent_overdue_jumps_urgency_and_names_target():
    s = MailSummary(
        sent_awaiting_reply=2,
        overdue=(
            {"delegation_id": "d-1", "target_workspace_id": "aaaabbbb-cccc", "age_seconds": 25200},
        ),
    )
    [c] = _run(SentMailProvider(source=_FakeSource(s)).contribute())
    # D2: an overdue no-reply jumps the tier queue
    assert c.urgency == Urgency.URGENT and c.band == Band.URGENT
    assert "⚠ 1 sent >6h with no reply" in c.summary
    assert "target agent" in c.summary and "aaaabbbb" in c.summary
    # age appears ONLY as a band (7h → 1h-1d), never raw
    assert c.age_band == AgeBand.ONE_H_TO_1D
    assert "25200" not in c.summary
    # the overdue id SET rides the delta hash: a different delegation going
    # overdue re-fires even at the same counts
    assert "overdue:d-1" in c.item_ids and "awaiting:2" in c.item_ids


def test_source_unavailable_raises_for_failed_exclusion():
    # "Comms down" must be a RUNNER-VISIBLE failure (hash-excluded
    # symmetrically per the contract), never an empty "no mail" section —
    # otherwise a transient 500 fires the digest on the blip AND the recovery.
    class _DownSource:
        async def fetch(self):
            raise CommsSummaryUnavailable("boom")

    with pytest.raises(CommsSummaryUnavailable):
        _run(InboundMailProvider(source=_DownSource()).contribute())
    with pytest.raises(CommsSummaryUnavailable):
        _run(SentMailProvider(source=_DownSource()).contribute())

    # A legacy/plugin source that returns None is treated the same way.
    class _NoneSource:
        async def fetch(self):
            return None

    with pytest.raises(CommsSummaryUnavailable):
        _run(InboundMailProvider(source=_NoneSource()).contribute())


def test_platform_source_single_flights_concurrent_fetches(monkeypatch):
    # ProviderRunner gathers providers CONCURRENTLY — a cold cache must not
    # double-GET (or diverge between the two mail envelopes). The lock makes
    # the second concurrent fetch a cache hit.
    src = PlatformMailSummarySource(platform_url="http://p", workspace_id="w")
    calls = []

    def _slow_get():
        calls.append(1)
        return {"received_unread": 1, "sent_awaiting_reply": 1}

    monkeypatch.setattr(src, "_get", _slow_get)

    async def _concurrent():
        return await asyncio.gather(src.fetch(), src.fetch())

    first, second = _run(_concurrent())
    assert first == second and len(calls) == 1

    # and sequential re-fetch within the TTL is also a cache hit
    third = _run(src.fetch())
    assert third == first and len(calls) == 1


def test_roster_binds_mail_providers_only_with_a_source():
    # No platform coords, no injected source => phase-1 roster unchanged.
    ids = [p.provider_id for p in build_default_providers()]
    assert "sent-folder" not in ids and "inbound-a2a" not in ids
    # Platform coords => default binding assembles both, in tier order.
    ids = [
        p.provider_id
        for p in build_default_providers(platform_url="http://p", workspace_id="w")
    ]
    assert ids.index("sent-folder") < ids.index("inbound-a2a")
    # THE PLUGIN SEAM: an injected source (the future comms plugin) binds the
    # same providers with zero provider changes.
    fake = _FakeSource(MailSummary())
    provs = build_default_providers(comms_source=fake)
    mail = [p for p in provs if p.provider_id in ("sent-folder", "inbound-a2a")]
    assert len(mail) == 2 and all(p.source is fake for p in mail)
