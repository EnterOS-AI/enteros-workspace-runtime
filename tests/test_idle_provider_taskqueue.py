"""Unit tests for the task-queue digest provider (task #219, PR-6).

Injected sqlite path + clock — no live workspace. Covers the writer library
(add/set_current/update/complete/pivot/awaiting), the single-current invariant,
the E1-urgent / E2-base envelopes, tombstone pruning, and crash-safety.
"""
from __future__ import annotations

import pytest

from molecule_runtime.idle_digest import (
    AgeBand,
    Band,
    Policy,
    ProviderRunner,
    Urgency,
    validate,
)
from molecule_runtime.idle_digest.providers.task_queue import (
    STATUS_CURRENT,
    STATUS_PAUSED,
    TaskQueueProvider,
)


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _p(tmp_path, clock=None):
    return TaskQueueProvider(db_path=tmp_path / "state.sqlite", now_fn=clock or _Clock())


async def _envs(p):
    return {c.band: c for c in await p.contribute()}


# ---------------------------------------------------------------------------
# empty / basic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_contributes_nothing(tmp_path):
    assert await _p(tmp_path).contribute() == []


@pytest.mark.asyncio
async def test_queued_task_in_e2(tmp_path):
    p = _p(tmp_path)
    p.add_task("nightly backup audit")
    envs = await _envs(p)
    assert Band.BASE in envs
    e2 = envs[Band.BASE]
    assert e2.tier == 1 and e2.urgency is Urgency.NORMAL
    assert "1 queued" in e2.summary
    validate(e2)


# ---------------------------------------------------------------------------
# single-current invariant + D3 pivot
# ---------------------------------------------------------------------------


def test_single_current_invariant(tmp_path):
    p = _p(tmp_path)
    a = p.add_task("task A")
    p.set_current(a)
    b = p.add_task("task B")
    p.set_current(b)  # demotes A -> paused
    rows = {r.id: r.status for r in p.list_tasks()}
    assert rows[a] == STATUS_PAUSED and rows[b] == STATUS_CURRENT


def test_set_current_unknown_id_preserves_queue(tmp_path):
    """A stale/mistyped id must NOT demote the real current task (queue is never
    left with no current row)."""
    p = _p(tmp_path)
    a = p.add_task("real work", status=STATUS_CURRENT)
    with pytest.raises(ValueError):
        p.set_current("task-nonexistent")
    assert next(r for r in p.list_tasks() if r.id == a).status == STATUS_CURRENT


def test_update_current_unknown_id_preserves_queue(tmp_path):
    p = _p(tmp_path)
    a = p.add_task("real work", status=STATUS_CURRENT)
    with pytest.raises(ValueError):
        p.update_task("task-nonexistent", status=STATUS_CURRENT)
    assert next(r for r in p.list_tasks() if r.id == a).status == STATUS_CURRENT


def test_add_current_demotes_existing(tmp_path):
    p = _p(tmp_path)
    a = p.add_task("A", status=STATUS_CURRENT)
    p.add_task("B", status=STATUS_CURRENT)  # demotes A
    statuses = sorted(r.status for r in p.list_tasks())
    assert statuses == [STATUS_CURRENT, STATUS_PAUSED]
    assert next(r for r in p.list_tasks() if r.id == a).status == STATUS_PAUSED


@pytest.mark.asyncio
async def test_pivot_to_user_pauses_interrupted_and_is_current(tmp_path):
    p = _p(tmp_path)
    work = p.add_task("rotate the bot token", status=STATUS_CURRENT)
    uid = p.pivot_to_user("answer the user's question")
    rows = {r.id: r for r in p.list_tasks()}
    assert rows[work].status == STATUS_PAUSED  # interrupted task survives
    assert rows[uid].status == STATUS_CURRENT and rows[uid].origin == "user"
    # the pivoted user task is CURRENT -> renders in E2, not the urgent band
    envs = await _envs(p)
    assert Band.URGENT not in envs or "answer the user" not in envs[Band.URGENT].summary


# ---------------------------------------------------------------------------
# E1 urgent — open user/lifecycle rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_user_ask_is_urgent(tmp_path):
    p = _p(tmp_path)
    p.add_task("confirm the canary window?", origin="user", status="queued")
    envs = await _envs(p)
    assert Band.URGENT in envs
    e1 = envs[Band.URGENT]
    assert e1.urgency is Urgency.URGENT and e1.count == 1
    assert "confirm the canary window" in e1.summary
    validate(e1)


@pytest.mark.asyncio
async def test_lifecycle_resume_row_is_urgent(tmp_path):
    p = _p(tmp_path)
    p.add_task("resume: rotate token after reprovision", origin="lifecycle", status="queued")
    envs = await _envs(p)
    assert Band.URGENT in envs and envs[Band.URGENT].count == 1


@pytest.mark.asyncio
async def test_current_user_row_not_in_urgent(tmp_path):
    p = _p(tmp_path)
    p.add_task("user thing", origin="user", status=STATUS_CURRENT)
    envs = await _envs(p)
    assert Band.URGENT not in envs  # current excluded from E1


@pytest.mark.asyncio
async def test_item_ids_encode_status(tmp_path):
    p = _p(tmp_path)
    tid = p.add_task("x", origin="user", status="queued")
    [e1] = [c for c in await p.contribute() if c.band is Band.URGENT]
    assert f"{tid}:queued" in e1.item_ids


# ---------------------------------------------------------------------------
# awaiting-user / complete / prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_awaiting_user_surfaces_and_resolves(tmp_path):
    p = _p(tmp_path)
    p.upsert_awaiting_user("req-1", "confirm the deploy?")
    p.upsert_awaiting_user("req-1", "confirm the deploy?")  # idempotent
    envs = await _envs(p)
    assert "1 awaiting user" in envs[Band.BASE].summary
    p.resolve_awaiting_user("req-1")
    envs2 = await _envs(p)
    assert Band.BASE not in envs2 or "awaiting user" not in envs2[Band.BASE].summary


@pytest.mark.asyncio
async def test_completed_task_disappears(tmp_path):
    p = _p(tmp_path)
    tid = p.add_task("do it")
    p.complete_task(tid)
    assert await p.contribute() == []  # no active work


def test_tombstone_prune(tmp_path):
    clock = _Clock()
    p = _p(tmp_path, clock)
    tid = p.add_task("old")
    p.complete_task(tid)
    clock.advance(15 * 86400)  # older than the 14-day window
    assert p.prune_tombstones() == 1
    assert p.list_tasks(status="done") == []


# ---------------------------------------------------------------------------
# age bands + crash-safety + trust
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_age_band_buckets(tmp_path):
    clock = _Clock()
    p = _p(tmp_path, clock)
    p.add_task("stale", origin="user", status="blocked")
    clock.advance(2 * 86400)  # > 1 day
    [e1] = [c for c in await p.contribute() if c.band is Band.URGENT]
    assert e1.age_band is AgeBand.OVER_1D


@pytest.mark.asyncio
async def test_corrupt_store_does_not_crash(tmp_path):
    dbp = tmp_path / "state.sqlite"
    dbp.write_bytes(b"not a database")
    p = TaskQueueProvider(db_path=dbp)
    # a corrupt db must not stall/crash the tick
    assert await p.contribute() == []


@pytest.mark.asyncio
async def test_official_and_runner_accepts(tmp_path):
    p = _p(tmp_path)
    p.add_task("x")
    assert p.official is True and p.provider_id == "task-queue"
    res = await ProviderRunner(Policy()).gather([p])
    assert res.contributions and not res.newly_disabled


def test_invalid_origin_and_status_rejected(tmp_path):
    p = _p(tmp_path)
    with pytest.raises(ValueError):
        p.add_task("x", origin="bogus")
    with pytest.raises(ValueError):
        p.add_task("x", status="bogus")


def test_round_trip_persists(tmp_path):
    p1 = _p(tmp_path)
    p1.add_task("persisted", origin="user", status="blocked")
    p2 = TaskQueueProvider(db_path=tmp_path / "state.sqlite")
    rows = p2.list_tasks()
    assert len(rows) == 1 and rows[0].title == "persisted"
