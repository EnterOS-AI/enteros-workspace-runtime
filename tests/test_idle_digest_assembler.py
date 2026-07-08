"""Unit tests for the idle-digest assembler engine (task #219, PR-2).

Pure-engine coverage — no runtime, no lease, no tenant. Exercises every
load-bearing rule the contract pins: the 7-field hash (band excluded), delta
firing with failed-section exclusion, the goal-cadence double-fire prevention,
band/tier sort, header-excluded emptiness, trust validation, limits +
deterministic budget truncation, and the failure-safe ProviderRunner.
"""
from __future__ import annotations

import asyncio

import pytest

from molecule_runtime.idle_digest import (
    AgeBand,
    Band,
    Contribution,
    ContributionError,
    Policy,
    PreviewItem,
    ProviderRunner,
    PullInstruction,
    Urgency,
    assemble,
    body_is_empty,
    check_reserved_id,
    content_hash,
    render,
    should_fire,
    signature,
    sort_contributions,
    validate,
)


def _c(
    provider_id="task-queue",
    band=Band.BASE,
    tier=1,
    urgency=Urgency.NORMAL,
    count=1,
    summary="s",
    age_band=AgeBand.NONE,
    item_ids=(),
    pull=None,
    preview_items=(),
):
    return Contribution(
        provider_id=provider_id,
        band=band,
        tier=tier,
        urgency=urgency,
        count=count,
        summary=summary,
        age_band=age_band,
        item_ids=tuple(item_ids),
        pull=pull,
        preview_items=tuple(preview_items),
    )


# ---------------------------------------------------------------------------
# hashing — the 7-field tuple, band excluded
# ---------------------------------------------------------------------------


def test_hash_is_sha256_prefixed():
    h = content_hash(_c())
    assert h.startswith("sha256:") and len(h) == len("sha256:") + 64


def test_band_is_excluded_from_hash():
    # same everything except band -> identical hash (band derived from urgency)
    a = _c(band=Band.BASE, urgency=Urgency.NORMAL)
    b = _c(band=Band.URGENT, urgency=Urgency.NORMAL)  # contrived: band differs only
    assert content_hash(a) == content_hash(b)


def test_urgency_is_hashed():
    a = _c(urgency=Urgency.NORMAL, band=Band.BASE)
    b = _c(urgency=Urgency.URGENT, band=Band.BASE)
    assert content_hash(a) != content_hash(b)


def test_status_change_via_item_ids_changes_hash():
    a = _c(item_ids=("task-1:queued",))
    b = _c(item_ids=("task-1:paused",))
    assert content_hash(a) != content_hash(b)


def test_age_band_change_changes_hash():
    a = _c(age_band=AgeBand.UNDER_1H)
    b = _c(age_band=AgeBand.ONE_H_TO_1D)
    assert content_hash(a) != content_hash(b)


def test_pull_and_previews_do_not_change_hash():
    a = _c(pull=None, preview_items=())
    b = _c(
        pull=PullInstruction("task_list", "pull it", 5),
        preview_items=(PreviewItem("i1", "queued", "x", AgeBand.UNDER_1H),),
    )
    assert content_hash(a) == content_hash(b)


# ---------------------------------------------------------------------------
# delta — signature + should_fire
# ---------------------------------------------------------------------------


def test_unchanged_digest_does_not_fire():
    cs = [_c()]
    base = signature(cs)
    assert should_fire(cs, base) is False


def test_new_item_fires():
    base = signature([_c(count=1)])
    assert should_fire([_c(count=2)], base) is True


def test_failed_provider_crash_does_not_refire():
    # baseline had task-queue + goal; goal fails this tick -> excluded both sides
    base = signature([_c(provider_id="task-queue"), _c(provider_id="goal-state")])
    current = [_c(provider_id="task-queue")]  # goal produced nothing (failed)
    assert should_fire(current, base, failed_provider_ids=frozenset({"goal-state"})) is False


def test_failed_provider_recover_same_content_does_not_refire():
    base = signature([_c(provider_id="task-queue"), _c(provider_id="goal-state")])
    current = [_c(provider_id="task-queue"), _c(provider_id="goal-state")]
    assert should_fire(current, base, failed_provider_ids=frozenset()) is False


def test_failed_provider_excluded_from_current_side_too():
    """Symmetric exclusion: even if a failed provider's (stale/partial)
    contribution appears in `current`, it is filtered from BOTH sides, so it
    cannot spuriously fire — matching the documented contract."""
    base = signature([_c(provider_id="task-queue")])
    # goal-state is reported failed but a stale contribution leaked into current
    current = [_c(provider_id="task-queue"), _c(provider_id="goal-state", count=99)]
    assert (
        should_fire(current, base, failed_provider_ids=frozenset({"goal-state"}))
        is False
    )


def test_goal_cadence_no_double_fire_with_post_included_baseline():
    """The load-bearing double-fire guard. At fire time the goal reports
    age_band=due; after on_included it reports just-included. If the baseline
    is recomputed POST-callback (just-included), the next tick sees no change."""
    goal_due = _c(provider_id="goal-state", tier=7, age_band=AgeBand.DUE)
    goal_just = _c(provider_id="goal-state", tier=7, age_band=AgeBand.JUST_INCLUDED)

    # tick 0: due -> would fire against an empty baseline
    assert should_fire([goal_due], ()) is True
    # CORRECT baseline: recomputed AFTER on_included -> just-included
    baseline = signature([goal_just])
    # tick 1 (300s later, still within cadence): provider still reports
    # just-included -> no change -> no double fire
    assert should_fire([goal_just], baseline) is False
    # cadence elapses: provider now reports due again -> fires
    assert should_fire([goal_due], baseline) is True


def test_fire_time_baseline_would_double_fire():
    """Proves the bug the post-on_included rule prevents: baselining at the
    fire-time 'due' value re-fires one tick later when the band resets."""
    goal_due = _c(provider_id="goal-state", age_band=AgeBand.DUE)
    goal_just = _c(provider_id="goal-state", age_band=AgeBand.JUST_INCLUDED)
    wrong_baseline = signature([goal_due])  # fire-time value (the bug)
    assert should_fire([goal_just], wrong_baseline) is True  # spurious refire


# ---------------------------------------------------------------------------
# sort + empty
# ---------------------------------------------------------------------------


def test_sort_pinned_urgent_base():
    pinned = _c(provider_id="identity-capabilities", band=Band.PINNED, tier=0)
    base1 = _c(provider_id="task-queue", band=Band.BASE, tier=1)
    base7 = _c(provider_id="goal-state", band=Band.BASE, tier=7)
    urgent = _c(provider_id="task-queue", band=Band.URGENT, tier=1, urgency=Urgency.URGENT)
    out = sort_contributions([base7, base1, urgent, pinned])
    assert [c.band for c in out] == [Band.PINNED, Band.URGENT, Band.BASE, Band.BASE]
    assert [c.tier for c in out[2:]] == [1, 7]


def test_urgent_outranks_lower_tier_base():
    urgent_hi_tier = _c(band=Band.URGENT, tier=7, urgency=Urgency.URGENT)
    base_lo_tier = _c(band=Band.BASE, tier=1)
    out = sort_contributions([base_lo_tier, urgent_hi_tier])
    assert out[0] is urgent_hi_tier  # urgent band beats a lower base tier


def test_header_only_is_empty():
    header = _c(provider_id="identity-capabilities", band=Band.PINNED, tier=0)
    assert body_is_empty([header]) is True


def test_body_present_not_empty():
    header = _c(provider_id="identity-capabilities", band=Band.PINNED, tier=0)
    assert body_is_empty([header, _c()]) is False


# ---------------------------------------------------------------------------
# trust / validation
# ---------------------------------------------------------------------------


def test_pinned_reserved_to_identity():
    with pytest.raises(ContributionError):
        validate(_c(provider_id="task-queue", band=Band.PINNED))


def test_identity_may_be_pinned():
    validate(_c(provider_id="identity-capabilities", band=Band.PINNED, tier=0))


def test_urgent_band_requires_urgent_urgency():
    with pytest.raises(ContributionError):
        validate(_c(band=Band.URGENT, urgency=Urgency.NORMAL))


# ---------------------------------------------------------------------------
# limits + budget
# ---------------------------------------------------------------------------


def test_summary_byte_cap_utf8_safe():
    policy = Policy(max_summary_bytes=20)
    long = "é" * 100  # 2 bytes each
    d = assemble([_c(summary=long)], policy)
    rendered = d.contributions[0].summary
    assert len(rendered.encode("utf-8")) <= 20
    assert rendered.endswith("…")


def test_preview_item_cap():
    policy = Policy(max_preview_items_per_envelope=2)
    items = tuple(PreviewItem(f"i{n}", "queued", f"x{n}", AgeBand.UNDER_1H) for n in range(5))
    d = assemble([_c(preview_items=items)], policy)
    assert len(d.contributions[0].preview_items) == 2


def test_envelope_cap_per_provider():
    policy = Policy(max_envelopes_per_provider=2)
    raw = [_c(provider_id="task-queue", tier=1, summary=f"e{n}") for n in range(5)]
    d = assemble(raw, policy)
    assert sum(1 for c in d.contributions if c.provider_id == "task-queue") == 2


def test_budget_truncation_drops_lowest_previews_keeps_counts():
    # budget chosen to sit between (both previews) and (high preview only), so
    # exactly the lowest-tier preview must be stripped.
    hi = _c(
        provider_id="task-queue", tier=1, summary="high tier",
        preview_items=(PreviewItem("a", "queued", "A" * 40, AgeBand.UNDER_1H),),
    )
    lo = _c(
        provider_id="goal-state", tier=7, summary="low tier",
        preview_items=(PreviewItem("b", "due", "B" * 40, AgeBand.DUE),),
    )
    full = render([hi, lo]).encode("utf-8")
    hi_only = render([hi, _c(provider_id="goal-state", tier=7, summary="low tier")]).encode("utf-8")
    assert len(hi_only) < len(full)  # sanity: budget window exists
    policy = Policy(max_digest_bytes=len(hi_only) + 1)
    d = assemble([hi, lo], policy)
    by = {c.provider_id: c for c in d.contributions}
    # both summaries survive (never dropped); lowest-tier previews go first
    assert "high tier" in d.text and "low tier" in d.text
    assert by["goal-state"].preview_items == ()
    assert by["task-queue"].preview_items != ()  # higher tier keeps its detail


# ---------------------------------------------------------------------------
# assemble end-to-end
# ---------------------------------------------------------------------------


def test_assemble_orders_and_signs():
    header = _c(provider_id="identity-capabilities", band=Band.PINNED, tier=0, summary="You are X")
    task = _c(provider_id="task-queue", band=Band.BASE, tier=1, summary="tasks")
    d = assemble([task, header], Policy())
    assert d.contributions[0].band is Band.PINNED
    assert d.is_empty is False
    assert d.signature == signature(d.contributions)
    assert "You are X" in d.text


def test_assemble_header_only_is_empty():
    header = _c(provider_id="identity-capabilities", band=Band.PINNED, tier=0)
    d = assemble([header], Policy())
    assert d.is_empty is True


# ---------------------------------------------------------------------------
# ProviderRunner — failure semantics
# ---------------------------------------------------------------------------


class _Provider:
    def __init__(self, provider_id, contribs=None, *, raises=None, hang=False, official=False):
        self.provider_id = provider_id
        self.official = official
        self._contribs = contribs or []
        self._raises = raises
        self._hang = hang
        self.included_at = None

    async def contribute(self):
        if self._hang:
            await asyncio.sleep(3600)
        if self._raises:
            raise self._raises
        return self._contribs

    def on_included(self, fired_at):
        self.included_at = fired_at


@pytest.mark.asyncio
async def test_runner_collects_success():
    p = _Provider("my-plugin", [_c(provider_id="my-plugin")])
    runner = ProviderRunner(Policy())
    res = await runner.gather([p])
    assert len(res.contributions) == 1 and not res.failed


@pytest.mark.asyncio
async def test_runner_skips_raising_provider():
    good = _Provider("my-plugin", [_c(provider_id="my-plugin")])
    bad = _Provider("flaky-plugin", raises=RuntimeError("boom"))
    runner = ProviderRunner(Policy())
    res = await runner.gather([good, bad])
    assert res.contributions and "flaky-plugin" in res.failed
    assert any("degraded" in m for m in res.degraded_markers)


@pytest.mark.asyncio
async def test_runner_times_out_hanging_provider():
    hang = _Provider("flaky-plugin", hang=True)
    runner = ProviderRunner(Policy(provider_timeout_seconds=1))
    res = await asyncio.wait_for(runner.gather([hang]), timeout=5)
    assert "flaky-plugin" in res.failed


@pytest.mark.asyncio
async def test_runner_rejects_spoofed_provider_id():
    spoof = _Provider("my-plugin", [_c(provider_id="identity-capabilities", band=Band.PINNED, tier=0)])
    runner = ProviderRunner(Policy())
    res = await runner.gather([spoof])
    assert "my-plugin" in res.failed  # envelope claimed a foreign id


@pytest.mark.asyncio
async def test_runner_disables_after_consecutive_failures():
    bad = _Provider("flaky-plugin", raises=RuntimeError("boom"))
    runner = ProviderRunner(Policy(max_consecutive_failures=3))
    for _ in range(2):
        res = await runner.gather([bad])
        assert not res.newly_disabled
    res = await runner.gather([bad])  # 3rd failure -> disable
    assert "flaky-plugin" in res.newly_disabled
    assert runner.is_disabled("flaky-plugin")
    # disabled provider is not invoked again
    res = await runner.gather([bad])
    assert not res.degraded_markers  # silent after the one disable notice


@pytest.mark.asyncio
async def test_runner_success_resets_failure_count():
    prov = _Provider("flaky-plugin", raises=RuntimeError("boom"))
    runner = ProviderRunner(Policy(max_consecutive_failures=3))
    await runner.gather([prov])
    await runner.gather([prov])  # 2 failures
    prov._raises = None
    prov._contribs = [_c(provider_id="goal-state")]
    await runner.gather([prov])  # success resets
    prov._raises = RuntimeError("boom again")
    res = await runner.gather([prov])
    assert not res.newly_disabled  # counter reset, not at cap


@pytest.mark.asyncio
async def test_runner_reset_reenables():
    bad = _Provider("flaky-plugin", raises=RuntimeError("boom"))
    runner = ProviderRunner(Policy(max_consecutive_failures=1))
    await runner.gather([bad])
    assert runner.is_disabled("flaky-plugin")
    runner.reset("flaky-plugin")
    assert not runner.is_disabled("flaky-plugin")


# ---------------------------------------------------------------------------
# reserved provider id trust enforcement
# ---------------------------------------------------------------------------


def test_check_reserved_id_rejects_third_party():
    with pytest.raises(ContributionError):
        check_reserved_id("goal-state", official=False)
    with pytest.raises(ContributionError):
        check_reserved_id("task-queue", official=False)


def test_check_reserved_id_allows_official():
    check_reserved_id("goal-state", official=True)  # no raise
    check_reserved_id("task-queue", official=True)


def test_check_reserved_id_allows_third_party_nonreserved():
    check_reserved_id("my-cool-plugin", official=False)  # not reserved -> fine


@pytest.mark.asyncio
async def test_runner_rejects_third_party_claiming_reserved_id():
    spoof = _Provider(
        "goal-state",
        [_c(provider_id="goal-state", tier=7)],
        official=False,  # a plugin masquerading as the official goal provider
    )
    runner = ProviderRunner(Policy())
    res = await runner.gather([spoof])
    assert not res.contributions  # never invoked
    assert "goal-state" in res.newly_disabled
    assert any("reserved" in m for m in res.degraded_markers)
    # stays rejected + silent on subsequent ticks
    res2 = await runner.gather([spoof])
    assert not res2.contributions and not res2.degraded_markers


@pytest.mark.asyncio
async def test_runner_allows_official_reserved_id():
    official = _Provider(
        "goal-state", [_c(provider_id="goal-state", tier=7)], official=True
    )
    runner = ProviderRunner(Policy())
    res = await runner.gather([official])
    assert len(res.contributions) == 1 and not res.newly_disabled


@pytest.mark.asyncio
async def test_runner_provider_missing_official_attr_is_third_party():
    """A provider that omits the ``official`` marker fails safe to third-party,
    so it cannot claim a reserved id (getattr default False)."""

    class _Bare:
        provider_id = "task-queue"

        async def contribute(self):
            return [_c(provider_id="task-queue")]

        def on_included(self, fired_at):
            pass

    runner = ProviderRunner(Policy())
    res = await runner.gather([_Bare()])
    assert "task-queue" in res.newly_disabled and not res.contributions


# ---------------------------------------------------------------------------
# Policy env overrides
# ---------------------------------------------------------------------------


def test_policy_env_override(monkeypatch):
    monkeypatch.setenv("MOLECULE_IDLE_FIRE_SECONDS", "600")
    monkeypatch.setenv("MOLECULE_IDLE_MAX_SUMMARY_BYTES", "128")
    p = Policy.default()
    assert p.idle_fire_after_seconds == 600
    assert p.max_summary_bytes == 128


def test_policy_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("MOLECULE_IDLE_FIRE_SECONDS", "not-a-number")
    p = Policy.default()
    assert p.idle_fire_after_seconds == 300  # contract default
