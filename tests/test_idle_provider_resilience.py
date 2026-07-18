"""Resilience of the idle-digest provider layer — the three defects that made
the mail section lie, go permanently silent, or double-fire.

Every test here was verified to FAIL against the pre-fix code. That matters: the
bugs they cover all shipped past a green suite, because the old tests asserted
the happy path and the failure paths were the ones that were broken.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime.idle_digest.contract import (  # noqa: E402
    AgeBand,
    Band,
    Contribution,
    Policy,
    Urgency,
)
from molecule_runtime.idle_digest.provider import ProviderRunner  # noqa: E402
from molecule_runtime.idle_digest.providers.mail import (  # noqa: E402
    CommsSummaryUnavailable,
    MailSummary,
    PlatformMailSummarySource,
    SentMailProvider,
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FlakyProvider:
    """Fails until `heal_after` calls, then succeeds. Models a provider whose
    DEPENDENCY (not the provider) was transiently down."""

    provider_id = "task-queue"
    official = True

    def __init__(self, heal_after: int):
        self.calls = 0
        self.heal_after = heal_after

    async def contribute(self):
        self.calls += 1
        if self.calls <= self.heal_after:
            raise RuntimeError("dependency down")
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.BASE,
                tier=1,
                urgency=Urgency.NORMAL,
                count=1,
                summary="back",
                age_band=AgeBand.NONE,
                item_ids=("x",),
            )
        ]

    def on_included(self, fired_at: float) -> None:
        return


def test_quarantine_is_recoverable_not_a_life_sentence():
    # A routine workspace-server redeploy = a few failed ticks. Before the fix
    # the provider crossed max_consecutive_failures and was disabled FOREVER:
    # the only escape was reset(), which the tick loop never calls. The server
    # came back healthy and the agent never saw its mail again for the life of
    # the container. Permanent suppression is the exact inverse of the contract's
    # "a transient outage neither fires the digest nor suppresses it".
    policy = Policy.default()
    runner = ProviderRunner(policy=policy)
    p = _FlakyProvider(heal_after=policy.max_consecutive_failures)

    for _ in range(policy.max_consecutive_failures):
        _run(runner.gather([p]))
    assert runner.is_disabled(p.provider_id), "should quarantine after the cap"

    # The dependency has recovered. Tick until the cooldown elapses and the
    # provider is re-probed — WITHOUT anyone calling reset().
    recovered = False
    for _ in range(10):
        res = _run(runner.gather([p]))
        if res.contributions:
            recovered = True
            break
    assert recovered, "a quarantined provider whose dependency recovered must come back on its own"
    assert not runner.is_disabled(p.provider_id)


def test_trust_ban_is_permanent_and_never_reprobed():
    # The recoverable quarantine must NOT resurrect a provider banned for
    # spoofing a reserved official id. That is a trust violation, not a blip —
    # folding the two into one set would turn the re-probe into a periodic un-ban.
    class _Spoofer:
        provider_id = "goal-state"  # reserved, official-only
        official = False  # ...but claims it as third-party

        async def contribute(self):  # pragma: no cover - must never be invoked
            raise AssertionError("a banned provider must never be executed")

        def on_included(self, fired_at: float) -> None:
            return

    runner = ProviderRunner(policy=Policy.default())
    spoofer = _Spoofer()

    res = _run(runner.gather([spoofer]))
    assert spoofer.provider_id in res.newly_disabled
    assert runner.is_disabled(spoofer.provider_id)

    # Tick well past the longest cooldown — contribute() asserts if it is ever
    # called, so a resurrection fails loudly.
    for _ in range(40):
        _run(runner.gather([spoofer]))
    assert runner.is_disabled(spoofer.provider_id), "a trust ban must be permanent"

    # ...and it is not operator-resettable either.
    runner.reset(spoofer.provider_id)
    assert runner.is_disabled(spoofer.provider_id)


def test_failing_tick_single_flights_too(monkeypatch):
    # The lock memoized only SUCCESSES, so on a failing tick the first provider
    # raised and left the cache empty and the second re-acquired and issued a
    # SECOND GET. Mid-deploy (502 then 200) that produced exactly the divergence
    # the contract forbids: one mail section failed-and-excluded, the other
    # rendering a full envelope — then a spurious re-fire on recovery.
    src = PlatformMailSummarySource(platform_url="http://p", workspace_id="w")
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("502")

    monkeypatch.setattr(src, "_get", _boom)

    async def _concurrent():
        return await asyncio.gather(
            src.fetch(), src.fetch(), return_exceptions=True
        )

    first, second = _run(_concurrent())

    assert isinstance(first, CommsSummaryUnavailable)
    assert isinstance(second, CommsSummaryUnavailable)
    # ONE outcome per tick, shared — not two GETs and two independent verdicts.
    assert len(calls) == 1, f"failing tick must single-flight too, got {len(calls)} GETs"


def test_overdue_count_is_the_uncapped_total_not_the_capped_list():
    # The server caps the overdue LIST at 10 for naming offenders, but the COUNT
    # is the truth. Rendering len(list) reported 25 overdue as "10" — under-
    # reporting the blast radius — and froze the delta signal once the cap
    # saturated, so an 11th delegation going overdue changed nothing and the
    # digest stayed silent about it.
    class _Src:
        async def fetch(self):
            return MailSummary(
                sent_awaiting_reply=30,
                overdue=tuple(
                    {
                        "delegation_id": f"d-{i}",
                        "target_workspace_id": f"ws{i}",
                        "age_seconds": 25200,
                    }
                    for i in range(10)  # the capped sample
                ),
                overdue_count=25,  # the truth
                overdue_after_seconds=14400,
            )

    [c] = _run(SentMailProvider(source=_Src()).contribute())

    assert "⚠ 25 sent" in c.summary, f"must render the uncapped total: {c.summary}"
    assert "⚠ 10 sent" not in c.summary
    assert c.urgency == Urgency.URGENT and c.band == Band.URGENT
    # the total rides the delta signal, so the 11th..25th going overdue re-fires
    assert "overdue_n:25" in c.item_ids
    # ...and still no raw ages anywhere
    assert "25200" not in c.summary
    assert c.age_band == AgeBand.ONE_H_TO_1D


def test_quarantine_announces_once_then_stays_silent():
    # Recoverability must not be paid for with noise. A quarantined provider is
    # re-probed on a backoff, and each failed re-probe re-enters the disable
    # branch — so the naive version re-announced "[mail] unavailable..." into the
    # user's digest every 1, 2, 4, 8... ticks for as long as the dependency
    # stayed down. The pre-existing contract is ONE notice, then silence until it
    # recovers, and it survives the change to recoverable quarantine.
    policy = Policy.default()
    runner = ProviderRunner(policy=policy)
    p = _FlakyProvider(heal_after=10_000)  # never heals

    notices = 0
    announcements = 0
    for _ in range(30):
        res = _run(runner.gather([p]))
        notices += len(res.newly_disabled)
        announcements += len([m for m in res.degraded_markers if "unavailable after" in m])

    # 30 ticks of a permanently-down dependency, and the user hears about it once.
    assert notices == 1, f"quarantine must be announced exactly once, got {notices}"
    assert announcements == 1, (
        f"a failed re-probe must not re-announce the quarantine; "
        f"got {announcements} 'unavailable' lines across 30 ticks"
    )
    # ...and it is still being re-probed (so recovery remains possible), not
    # silently abandoned.
    assert p.calls > policy.max_consecutive_failures, "re-probes must keep happening"


class _Toggle:
    """A provider whose content is stable but whose dependency can blip."""

    def __init__(self, pid, tier, text="v1"):
        self.provider_id, self.tier, self.official = pid, tier, True
        self.up, self.text = True, text
        self.calls = 0

    async def contribute(self):
        self.calls += 1
        if not self.up:
            raise RuntimeError("dependency down")
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.BASE,
                tier=self.tier,
                urgency=Urgency.NORMAL,
                count=1,
                summary=self.text,
                age_band=AgeBand.NONE,
                item_ids=(self.text,),
            )
        ]

    def on_included(self, fired_at: float) -> None:
        return


def _controller(providers, tmp_path):
    from molecule_runtime.idle_digest.controller import IdleDigestController

    fired = []

    async def poster(text):
        fired.append(text)

    c = IdleDigestController(
        providers=providers,
        policy=Policy.default(),
        poster=poster,
        state_dir=tmp_path,
    )
    return c, fired


def test_failed_gather_does_not_drop_the_baseline_entry(tmp_path):
    # The double-fire. A provider that fails the FIRST gather is absent from
    # `assembled`, so the original carry-forward (sourced from assembled.signature)
    # carried NOTHING and dropped its baseline entry anyway — the exact bug it was
    # written to fix. It survived review because the failure it handles (fail
    # gather-2 only) is the RARE one: the mail source memoizes a failure for its
    # cache TTL, so a provider that fails gather-1 almost always fails gather-2 too,
    # milliseconds later.
    goal, mail = _Toggle("goal-state", 4), _Toggle("sent-folder", 2)
    c, fired = _controller([goal, mail], tmp_path)

    assert _run(c.tick()) == "fired"
    goal.text = "goal CHANGED"
    mail.up = False
    assert _run(c.tick()) == "fired"  # fires on goal; mail excluded as failed
    mail.up = True  # mail returns with the SAME content as tick 1

    assert _run(c.tick()) == "unchanged", (
        "mail recovered with unchanged content — re-firing here injects a digest "
        "whose mail section is byte-identical to one the agent already read"
    )
    assert len(fired) == 2


def test_baseline_never_holds_a_section_the_agent_was_not_shown(tmp_path):
    # THE INVARIANT: a baseline entry means "the agent has already read this".
    # An entry for a section it never saw makes the digest PERMANENTLY SILENT about
    # that section — it looks "unchanged" forever.
    #
    # The bug needed TWO things, and either fix alone kills it, so the test must set
    # up the exact state rather than hope a loop wanders into it:
    #   1. the post-fire peek PROBED quarantined providers (its readiness rule,
    #      `cooldown <= 1`, disagreed with the main gather's decrement at
    #      cooldown == 2), so the peek saw a provider the main gather had skipped;
    #   2. the baseline was taken from the peek's assembly, so that provider's
    #      content was recorded as "already read".
    #
    # An earlier version of this test drove the loop and asserted mail was EVENTUALLY
    # rendered. It passed with BOTH fixes reverted — vacuous, because the digest kept
    # firing on the other provider. Hence the exact state, and an assertion on the
    # baseline rather than on eventual output.
    goal, mail = _Toggle("goal-state", 4), _Toggle("sent-folder", 2)
    c, fired = _controller([goal, mail], tmp_path)

    # mail is quarantined with cooldown == 2 — the disagreement window — and its
    # dependency is healthy again.
    r = c._runner
    r._disabled.add("sent-folder")
    r._cooldown["sent-folder"] = 2
    r._backoff["sent-folder"] = 1
    mail.up = True

    goal.text = "CHANGED"
    assert _run(c.tick()) == "fired"

    mail_rendered = any("v1" in t for t in fired)
    mail_baselined = any(pid == "sent-folder" for pid, _ in c.baseline)

    assert not mail_rendered, "test setup: mail must be quarantined this tick"
    assert not mail_baselined, (
        "the baseline holds 'sent-folder', a section the agent was never shown. "
        "It will now compare as 'unchanged' forever — the mail section goes "
        "permanently silent."
    )
    assert mail.calls == 0, (
        f"a quarantined provider must not be probed by the post-fire peek "
        f"(called it {mail.calls}x) — the peek is a read, not a second tick"
    )


def test_quarantine_announcement_is_never_lost_in_the_peek(tmp_path):
    # The peek used to run the full failure machinery, so a FIRING tick
    # double-incremented _failures. If the quarantine cap was crossed inside the
    # peek, the "[mail] unavailable" marker went into the PEEK's degraded_markers —
    # which the controller discards (_degraded_suffix reads gather-1 only). The
    # agent was simply never told its mail section had gone dark.
    goal, mail = _Toggle("goal-state", 4), _Toggle("sent-folder", 2)
    c, fired = _controller([goal, mail], tmp_path)

    _run(c.tick())
    mail.up = False
    for i in range(4):
        goal.text = f"g{i}"
        _run(c.tick())

    # "consecutive failures" appears ONLY in the quarantine announcement. An
    # earlier version of this assertion grepped "unavailable", which ALSO matches
    # the per-tick degraded marker ("section unavailable (degraded)") — so it
    # passed even with the quarantine announcement deleted outright. It asserted
    # nothing.
    assert any("consecutive failures" in t for t in fired), (
        "the agent was never told the mail section went dark — the announcement "
        "was emitted during the post-fire peek and discarded"
    )


def test_peek_does_not_double_count_failures(tmp_path):
    # A tick is ONE observation. The peek is a second look at it, not a second one.
    goal, mail = _Toggle("goal-state", 4), _Toggle("sent-folder", 2)
    c, _ = _controller([goal, mail], tmp_path)

    mail.up = False
    _run(c.tick())  # a FIRING tick: gather + peek

    assert c._runner._failures.get("sent-folder") == 1, (
        f"one tick must count one failure, got "
        f"{c._runner._failures.get('sent-folder')} — the peek is double-counting, "
        f"so quarantine trips a full tick early"
    )


def test_healthy_provider_with_nothing_to_say_drops_its_baseline_entry(tmp_path):
    # The OTHER direction, and the one a naive "keep everything not rendered" rule
    # breaks. A provider that runs fine and simply has nothing to report (zero mail)
    # must have its entry DROPPED — not carried forward. Otherwise, when that same
    # content returns, it matches a stale baseline and the digest stays silent about
    # mail the agent has never been told about.
    class _Silent(_Toggle):
        async def contribute(self):
            if not self.up:
                return []  # healthy, nothing to say — NOT a failure
            return await _Toggle.contribute(self)

    goal, mail = _Toggle("goal-state", 4), _Silent("sent-folder", 2)
    c, fired = _controller([goal, mail], tmp_path)

    _run(c.tick())  # mail "v1" rendered
    mail.up = False
    goal.text = "g1"
    _run(c.tick())  # mail healthy but silent
    mail.up = True  # the SAME content comes back

    assert _run(c.tick()) == "fired", (
        "mail returned with content the agent has not seen since it went silent — "
        "a carried-over baseline entry would swallow it"
    )
