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


def _inbound(received_unread=0, replies_unread=0, received=None):
    return MailSummary(
        received_unread=received_unread,
        replies_unread=replies_unread,
        received=received,
    )


def _msg(mid, sender="peer-1", method="message/send", row_id=None):
    return {
        "id": row_id or f"row-{mid}",
        "message_id": mid,
        "sender_id": sender,
        "method": method,
    }


def test_inbound_counts_render_and_delta_ids():
    # core#5028: the counts still RENDER (the digest line is unchanged), but
    # the identity tuple is now built from real per-message ids. The old
    # assertion here — item_ids == ("recv:3", "replies:1") — PINNED THE DEFECT
    # AS THE SPEC: it asserted that a hash of two numbers was the change
    # signal, which is precisely what makes a compensating churn invisible.
    s = _inbound(
        received_unread=3,
        replies_unread=1,
        received=(_msg("m-1"), _msg("m-2"), _msg("m-3"), _msg("r-1", method="delegate_result")),
    )
    [c] = _run(InboundMailProvider(source=_FakeSource(s)).contribute())
    assert c.provider_id == "inbound-a2a" and c.tier == 3
    assert "3 received message(s) unread" in c.summary
    assert "1 reply not yet read" in c.summary
    assert "workspace communication MCP" in c.summary
    assert c.count == 4 and c.urgency == Urgency.NORMAL
    # counts still ride the delta signal (they catch a change the capped id
    # sample can miss), but the per-message IDENTITIES are what make a
    # same-count churn visible.
    assert "recv:3" in c.item_ids and "replies:1" in c.item_ids
    for mid in ("m-1", "m-2", "m-3", "r-1"):
        assert f"msg:{mid}" in c.item_ids, c.item_ids
    # no raw ages anywhere
    assert c.age_band == AgeBand.NONE
    assert c.pull.tool == "inbox_peek"


def test_inbound_equal_counts_different_messages_refires():
    """THE core#5028 REGRESSION.

    Two ticks, EQUAL counts, DIFFERENT messages: the agent read one message
    and one new one arrived in between. A digest whose change-detection hash
    is a hash of a number cannot tell these apart, so the brand-new inbound is
    never surfaced. The contributions' content hashes must differ.
    """
    from molecule_runtime.idle_digest.assembler import content_hash, should_fire

    tick1 = _inbound(received_unread=2, received=(_msg("A"), _msg("B")))
    tick2 = _inbound(received_unread=2, received=(_msg("B"), _msg("C")))

    [c1] = _run(InboundMailProvider(source=_FakeSource(tick1)).contribute())
    [c2] = _run(InboundMailProvider(source=_FakeSource(tick2)).contribute())

    # Precondition: this test only proves anything while the COUNTS MATCH.
    assert c1.count == c2.count == 2
    assert c1.summary == c2.summary, "counts identical => the rendered line is identical"

    assert content_hash(c1) != content_hash(c2), (
        "equal counts + different messages hash IDENTICALLY — a brand-new "
        f"inbound is invisible to the digest. item_ids: {c1.item_ids} vs {c2.item_ids}"
    )
    # and end-to-end through the fire decision the controller actually calls
    baseline = tuple(sorted((c1.provider_id, content_hash(c1)) for _ in (0,)))
    assert should_fire([c2], baseline) is True
    # steady state (same messages) must still stay SILENT — the fix must not
    # turn the digest into a nag loop.
    [c1b] = _run(InboundMailProvider(source=_FakeSource(tick1)).contribute())
    assert should_fire([c1b], baseline) is False


def test_inbound_identity_survives_a_missing_message_id():
    # A sender that supplied no a2a messageId still gets a stable identity:
    # the server always projects a row `id`. Falling back to the count here
    # would reintroduce the defect for exactly those messages.
    s = _inbound(
        received_unread=2,
        received=(
            {"id": "row-1", "message_id": "", "sender_id": "p"},
            {"id": "row-2", "sender_id": "p"},
        ),
    )
    [c] = _run(InboundMailProvider(source=_FakeSource(s)).contribute())
    assert "msg:row-1" in c.item_ids and "msg:row-2" in c.item_ids, c.item_ids


def test_inbound_identity_tuple_is_order_independent():
    # The server orders newest-first; a re-order alone must NOT re-fire.
    a = _inbound(received_unread=2, received=(_msg("A"), _msg("B")))
    b = _inbound(received_unread=2, received=(_msg("B"), _msg("A")))
    [ca] = _run(InboundMailProvider(source=_FakeSource(a)).contribute())
    [cb] = _run(InboundMailProvider(source=_FakeSource(b)).contribute())
    assert ca.item_ids == cb.item_ids


def test_inbound_degrades_LOUDLY_against_a_server_without_identities(caplog):
    """Version skew must degrade, not crash — and must be OBSERVABLE.

    `received is None` means the server did not project the field at all (an
    old server), which is NOT the same as "present but empty". The provider
    falls back to today's count-only identity, but says so: a WARNING plus a
    marker inside item_ids. The `overdue_count` mistake — a silent permanent
    fallback that made everyone believe the bug was fixed — is exactly what
    this assertion exists to prevent.
    """
    import logging

    s = _inbound(received_unread=3, replies_unread=1, received=None)
    with caplog.at_level(logging.WARNING):
        [c] = _run(InboundMailProvider(source=_FakeSource(s)).contribute())

    assert "recv-identity:UNAVAILABLE" in c.item_ids, c.item_ids
    assert "recv:3" in c.item_ids and "replies:1" in c.item_ids
    assert any(
        "5028" in r.getMessage() or "identity" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    # The rendered digest line is UNCHANGED by the fallback — degradation is a
    # plumbing fact for operators, not noise for the agent.
    assert "3 received message(s) unread" in c.summary
    assert "UNAVAILABLE" not in c.summary


def test_inbound_present_but_empty_is_not_treated_as_skew():
    # An upgraded server with nothing unread sends `received: []`. That is a
    # FACT, not a skew — and the provider contributes nothing anyway.
    assert _run(InboundMailProvider(source=_FakeSource(_inbound(received=()))).contribute()) == []


def test_platform_source_distinguishes_absent_from_empty_received(monkeypatch):
    # The wire-level half of the skew handshake: a key the old server never
    # writes must arrive as None, not (). Getting this wrong is what makes a
    # fallback silent.
    old = PlatformMailSummarySource(platform_url="http://p", workspace_id="w")
    monkeypatch.setattr(old, "_get", lambda: {"received_unread": 2})
    assert _run(old.fetch()).received is None

    new = PlatformMailSummarySource(platform_url="http://p", workspace_id="w")
    monkeypatch.setattr(new, "_get", lambda: {"received_unread": 0, "received": []})
    assert _run(new.fetch()).received == ()

    full = PlatformMailSummarySource(platform_url="http://p", workspace_id="w")
    monkeypatch.setattr(
        full, "_get", lambda: {"received_unread": 1, "received": [{"id": "r", "message_id": "m"}]}
    )
    assert _run(full.fetch()).received == ({"id": "r", "message_id": "m"},)


def test_platform_source_reads_overdue_count_from_the_server(monkeypatch):
    # core#5028 second bug: the server now EMITS overdue_count, so the capped
    # len(overdue) fallback must stop being the value in play.
    src = PlatformMailSummarySource(platform_url="http://p", workspace_id="w")
    monkeypatch.setattr(
        src,
        "_get",
        lambda: {
            "sent_awaiting_reply": 25,
            "overdue": [{"delegation_id": f"d-{i}"} for i in range(10)],
            "overdue_count": 25,
        },
    )
    s = _run(src.fetch())
    assert s.overdue_count == 25, "the UNCAPPED server total, not len(overdue)"
    [c] = _run(SentMailProvider(source=_FakeSource(s)).contribute())
    assert "⚠ 25 sent >6h" in c.summary


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
