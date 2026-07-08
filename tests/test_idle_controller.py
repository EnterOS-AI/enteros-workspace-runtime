"""Unit tests for the idle-digest controller (task #219, PR-7).

Injected poster/state-dir/clock — the full fire cycle is exercised without a
runtime or lease. Covers the delta gate (fire on change / no re-fire unchanged),
empty→sleep, the atomic-fire recheck + #381 skip, on_included callbacks, the
POST-callback baseline recompute (goal double-fire guard, end-to-end through a
real GoalStateProvider), digest_state persistence, and failure degradation.
"""
from __future__ import annotations

import pytest

from molecule_runtime.idle_digest import (
    FIRED,
    SKIPPED,
    SLEEP,
    UNCHANGED,
    AgeBand,
    Band,
    Contribution,
    IdleDigestController,
    Policy,
    PreviewItem,
    Urgency,
)
from molecule_runtime.idle_digest.providers.goal import GoalStateProvider


class _Poster:
    def __init__(self, fail=False):
        self.posts: list[str] = []
        self.fail = fail

    async def __call__(self, text):
        if self.fail:
            raise RuntimeError("post failed")
        self.posts.append(text)


class _Fake:
    def __init__(self, pid, contribs, *, official=True):
        self.provider_id = pid
        self.official = official
        self._contribs = contribs
        self.included: list[float] = []

    async def contribute(self):
        return list(self._contribs)

    def on_included(self, fired_at):
        self.included.append(fired_at)


def _header():
    return Contribution(
        provider_id="identity-capabilities", band=Band.PINNED, tier=0,
        urgency=Urgency.NORMAL, count=1, summary="You are X", age_band=AgeBand.NONE,
    )


def _body(summary="1 queued", provider_id="task-queue", item_ids=("t1:queued",)):
    return Contribution(
        provider_id=provider_id, band=Band.BASE, tier=1, urgency=Urgency.NORMAL,
        count=1, summary=summary, age_band=AgeBand.NONE, item_ids=item_ids,
    )


def _ctrl(providers, poster, tmp_path, **kw):
    return IdleDigestController(
        providers=providers, policy=Policy(), poster=poster,
        state_dir=tmp_path, **kw,
    )


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ---------------------------------------------------------------------------
# delta gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fires_when_body_present_and_changed(tmp_path):
    poster = _Poster()
    c = _ctrl([_Fake("identity-capabilities", [_header()]), _Fake("task-queue", [_body()])], poster, tmp_path)
    assert await c.tick() == FIRED
    assert len(poster.posts) == 1 and "You are X" in poster.posts[0]


@pytest.mark.asyncio
async def test_unchanged_does_not_refire(tmp_path):
    poster = _Poster()
    c = _ctrl([_Fake("identity-capabilities", [_header()]), _Fake("task-queue", [_body()])], poster, tmp_path)
    assert await c.tick() == FIRED
    assert await c.tick() == UNCHANGED  # nothing changed -> no second fire
    assert len(poster.posts) == 1


@pytest.mark.asyncio
async def test_change_refires(tmp_path):
    poster = _Poster()
    body = _Fake("task-queue", [_body(summary="1 queued")])
    c = _ctrl([_Fake("identity-capabilities", [_header()]), body], poster, tmp_path)
    await c.tick()
    body._contribs = [_body(summary="2 queued", item_ids=("t1:queued", "t2:queued"))]
    assert await c.tick() == FIRED  # content changed
    assert len(poster.posts) == 2


@pytest.mark.asyncio
async def test_header_only_sleeps(tmp_path):
    poster = _Poster()
    c = _ctrl([_Fake("identity-capabilities", [_header()])], poster, tmp_path)
    assert await c.tick() == SLEEP
    assert not poster.posts


@pytest.mark.asyncio
async def test_no_providers_sleeps(tmp_path):
    assert await _ctrl([], _Poster(), tmp_path).tick() == SLEEP


# ---------------------------------------------------------------------------
# skip / atomic recheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_check_suppresses(tmp_path):
    poster = _Poster()
    c = _ctrl([_Fake("task-queue", [_body()])], poster, tmp_path, skip_check=lambda: True)
    assert await c.tick() == SKIPPED
    assert not poster.posts


@pytest.mark.asyncio
async def test_pre_fire_check_aborts(tmp_path):
    poster = _Poster()
    c = _ctrl([_Fake("task-queue", [_body()])], poster, tmp_path, pre_fire_check=lambda: False)
    assert await c.tick() == SKIPPED
    assert not poster.posts  # a turn started mid-assembly -> abort the fire


# ---------------------------------------------------------------------------
# on_included + baseline recompute (the double-fire guard, end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_included_called_on_included_providers(tmp_path):
    poster = _Poster()
    body = _Fake("task-queue", [_body()])
    c = _ctrl([_Fake("identity-capabilities", [_header()]), body], poster, tmp_path)
    await c.tick()
    assert len(body.included) == 1


@pytest.mark.asyncio
async def test_goal_cadence_no_double_fire_end_to_end(tmp_path):
    """The full guard: a goal fires, on_included stamps it, the controller
    recomputes the baseline POST-callback (goal now just-included), so the next
    tick does not double-fire — and it re-fires once the cadence elapses."""
    clock = _Clock()
    poster = _Poster()
    goal = GoalStateProvider(state_dir=tmp_path / "goal", now_fn=clock)
    goal.set("keep the backlog moving", cadence_seconds=3600)
    c = IdleDigestController(
        providers=[_Fake("identity-capabilities", [_header()]), goal],
        policy=Policy(), poster=poster, state_dir=tmp_path, now_fn=clock,
    )
    assert await c.tick() == FIRED  # goal due
    assert await c.tick() == UNCHANGED  # just-included -> no double fire
    clock.advance(300)
    assert await c.tick() == UNCHANGED  # still within cadence
    clock.advance(3600)
    assert await c.tick() == FIRED  # cadence elapsed -> re-fires
    assert len(poster.posts) == 2


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_persists_across_instances(tmp_path):
    providers = [_Fake("identity-capabilities", [_header()]), _Fake("task-queue", [_body()])]
    c1 = _ctrl(providers, _Poster(), tmp_path)
    await c1.tick()  # fires, persists baseline
    assert (tmp_path / "idle-prompt" / "digest_state.json").exists() or (tmp_path / "digest_state.json").exists()
    # a fresh controller (restart) loads the baseline -> same digest doesn't re-fire
    c2 = _ctrl(providers, _Poster(), tmp_path)
    assert await c2.tick() == UNCHANGED


# ---------------------------------------------------------------------------
# failure degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_provider_degrades_marker(tmp_path):
    poster = _Poster()

    class _Bad(_Fake):
        async def contribute(self):
            raise RuntimeError("boom")

    c = _ctrl(
        [_Fake("identity-capabilities", [_header()]), _Fake("task-queue", [_body()]),
         _Bad("goal-state", [])], poster, tmp_path,
    )
    assert await c.tick() == FIRED  # good providers still fire
    assert "degraded" in poster.posts[0]


@pytest.mark.asyncio
async def test_post_failure_does_not_advance_baseline(tmp_path):
    poster = _Poster(fail=True)
    good = _Poster()
    providers = [_Fake("identity-capabilities", [_header()]), _Fake("task-queue", [_body()])]
    c = _ctrl(providers, poster, tmp_path)
    assert await c.tick() == SKIPPED  # post failed
    # baseline not advanced -> a controller with a working poster still fires
    c2 = _ctrl(providers, good, tmp_path)
    assert await c2.tick() == FIRED


@pytest.mark.asyncio
async def test_preview_items_render(tmp_path):
    poster = _Poster()
    body = Contribution(
        provider_id="task-queue", band=Band.URGENT, tier=1, urgency=Urgency.URGENT,
        count=1, summary="1 open user ask", age_band=AgeBand.ONE_H_TO_1D,
        item_ids=("t1:paused",),
        preview_items=(PreviewItem("t1", "paused", "rotate the token", AgeBand.ONE_H_TO_1D, "mint it"),),
    )
    c = _ctrl([_Fake("task-queue", [body])], poster, tmp_path)
    assert await c.tick() == FIRED
    assert "rotate the token" in poster.posts[0]
