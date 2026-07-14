"""Unit tests for the goal-state digest provider (task #219, PR-5).

State dir and clock are injected (tmp_path + a controllable now), so these run
without a live workspace or the mailbox volume. Exercises the cadence band (the
loop driver), the post-on_included double-fire guard, the legacy migration rules
(source-rank + .migrated one-shot), set/clear/get, and the trust markers.
"""
from __future__ import annotations

import json

import pytest

from molecule_runtime.idle_digest import (
    AgeBand,
    Band,
    Policy,
    ProviderRunner,
    Urgency,
    should_fire,
    signature,
    validate,
)
from molecule_runtime.idle_digest.providers.goal import (
    CADENCE_DEFAULT_SECONDS,
    FLEET_PERSONA_DEFAULT,
    GoalStateProvider,
)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _provider(tmp_path, clock=None):
    return GoalStateProvider(state_dir=tmp_path, now_fn=clock or _Clock())


# ---------------------------------------------------------------------------
# empty / no goal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_goal_contributes_nothing(tmp_path):
    assert await _provider(tmp_path).contribute() == []


@pytest.mark.asyncio
async def test_hand_edited_non_mapping_goal_yaml_does_not_crash(tmp_path):
    """goal.yaml is a hand-edit surface: a valid-but-non-mapping file must read
    as 'no goal', never crash the tick (which would disable the provider after
    3 failures and silently kill the backlog loop)."""
    p = _provider(tmp_path)
    for bad in ("just pull the backlog\n", "- a\n- b\n", "42\n"):
        (tmp_path / "goal.yaml").write_text(bad)
        assert await p.contribute() == []  # no crash, no goal
        assert p.get() is None
        p.on_included(1234.0)  # must not raise either


@pytest.mark.asyncio
async def test_cleared_goal_contributes_nothing(tmp_path):
    p = _provider(tmp_path)
    p.set("do the thing")
    assert await p.contribute()  # present
    p.clear()
    assert await p.contribute() == []


# ---------------------------------------------------------------------------
# envelope shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_is_tier7_base_normal(tmp_path):
    p = _provider(tmp_path)
    p.set("Keep the docs backlog moving.")
    [c] = await p.contribute()
    assert c.band is Band.BASE and c.tier == 7
    assert c.urgency is Urgency.NORMAL  # never urgent
    assert c.count == 1
    assert "Background objective" in c.summary
    validate(c)


@pytest.mark.asyncio
async def test_freshly_set_goal_is_due(tmp_path):
    p = _provider(tmp_path)
    p.set("x")
    [c] = await p.contribute()
    assert c.age_band is AgeBand.DUE  # never surfaced -> due (fires)


@pytest.mark.asyncio
async def test_item_id_changes_with_goal_text(tmp_path):
    p = _provider(tmp_path)
    p.set("goal A")
    [a] = await p.contribute()
    p.set("goal B")
    [b] = await p.contribute()
    assert a.item_ids != b.item_ids


# ---------------------------------------------------------------------------
# cadence band — the loop driver + double-fire guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cadence_band_lifecycle(tmp_path):
    clock = _Clock(1000.0)
    p = _provider(tmp_path, clock)
    p.set("loop goal", cadence_seconds=3600)

    [due] = await p.contribute()
    assert due.age_band is AgeBand.DUE

    # simulate a fire that included the goal
    p.on_included(clock())
    [just] = await p.contribute()
    assert just.age_band is AgeBand.JUST_INCLUDED  # within cadence -> quiet

    # still within cadence 300s later
    clock.advance(300)
    [still] = await p.contribute()
    assert still.age_band is AgeBand.JUST_INCLUDED

    # cadence elapses -> due again
    clock.advance(3600)
    [again] = await p.contribute()
    assert again.age_band is AgeBand.DUE


@pytest.mark.asyncio
async def test_no_double_fire_with_post_included_baseline(tmp_path):
    """The load-bearing guard: after on_included the goal reports
    just-included; baselining that (per the engine's post-on_included rule)
    means the next tick sees no change — no double fire."""
    clock = _Clock(1000.0)
    p = _provider(tmp_path, clock)
    p.set("loop goal", cadence_seconds=3600)

    due = await p.contribute()
    assert should_fire(due, ()) is True  # fires against empty baseline

    p.on_included(clock())  # assembler callback
    just = await p.contribute()
    baseline = signature(just)  # recomputed POST-callback (just-included)

    clock.advance(300)  # one idle tick later, still within cadence
    tick = await p.contribute()
    assert should_fire(tick, baseline) is False  # no double fire

    clock.advance(3600)  # cadence elapsed
    later = await p.contribute()
    assert should_fire(later, baseline) is True  # re-fires at cadence


# ---------------------------------------------------------------------------
# cadence floor
# ---------------------------------------------------------------------------


def test_cadence_floor_clamped(tmp_path):
    p = _provider(tmp_path)
    doc = p.set("x", cadence_seconds=60)  # below the 300 floor
    assert doc.cadence_seconds == 300


def test_cadence_default_when_unspecified(tmp_path):
    p = _provider(tmp_path)
    doc = p.set("x")
    assert doc.cadence_seconds == CADENCE_DEFAULT_SECONDS


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migrate_legacy_value(tmp_path):
    p = _provider(tmp_path)
    doc = p.migrate_from_config("Work the incident queue.")
    assert doc is not None
    assert doc.source == "legacy-idle-prompt-migration"
    assert p.get()["goal"] == "Work the incident queue."


def test_migrate_persona_default_labelled(tmp_path):
    p = _provider(tmp_path)
    doc = p.migrate_from_config(FLEET_PERSONA_DEFAULT)
    assert doc is not None and doc.source == "fleet-persona-default"


def test_migrate_is_one_shot(tmp_path):
    p = _provider(tmp_path)
    p.migrate_from_config("first")
    again = p.migrate_from_config("second")  # marker exists -> no-op
    assert again is None
    assert p.get()["goal"] == "first"


def test_migrate_empty_value_does_not_burn_marker(tmp_path):
    p = _provider(tmp_path)
    assert p.migrate_from_config("") is None
    assert p.get() is None
    assert not (tmp_path / ".migrated").exists()  # nothing migrated -> no marker
    # a later boot with a materialized value can still perform the one-shot
    doc = p.migrate_from_config("late directive")
    assert doc is not None and p.get()["goal"] == "late directive"


def test_migrate_never_overwrites_workspace_mcp_goal(tmp_path):
    p = _provider(tmp_path)
    p.set("agent's own goal", source="workspace-mcp")
    assert p.migrate_from_config("legacy value") is None  # never clobbers
    assert p.get()["goal"] == "agent's own goal"


def test_migrate_overwrites_fleet_persona_default(tmp_path):
    p = _provider(tmp_path)
    # a provision default was written first (lower rank)
    p.set("persona default", source="fleet-persona-default")
    # reset the marker path is separate; migrate a legacy value (higher rank)
    doc = p.migrate_from_config("real legacy directive")
    assert doc is not None and doc.source == "legacy-idle-prompt-migration"
    assert p.get()["goal"] == "real legacy directive"


# ---------------------------------------------------------------------------
# persistence / audit
# ---------------------------------------------------------------------------


def test_round_trip_persists(tmp_path):
    p1 = _provider(tmp_path)
    p1.set("persisted", cadence_seconds=1800, set_by="operator")
    p2 = GoalStateProvider(state_dir=tmp_path)  # fresh instance, same dir
    got = p2.get()
    assert got["goal"] == "persisted" and got["cadence_seconds"] == 1800
    assert got["set_by"] == "operator"


def test_history_audited(tmp_path):
    p = _provider(tmp_path)
    p.set("g1")
    p.clear()
    lines = (tmp_path / "goal_history.jsonl").read_text().strip().splitlines()
    actions = [json.loads(x)["action"] for x in lines]
    assert actions == ["set", "clear"]


# ---------------------------------------------------------------------------
# trust
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_official_and_runner_accepts(tmp_path):
    p = _provider(tmp_path)
    p.set("x")
    assert p.official is True and p.provider_id == "goal-state"
    res = await ProviderRunner(Policy()).gather([p])
    assert len(res.contributions) == 1 and not res.newly_disabled


# ── MOLECULE_IDLE_GOAL env bootstrap (provision-time deterministic seed) ──


def _env_provider(tmp_path):
    from molecule_runtime.idle_digest.providers.goal import GoalStateProvider

    return GoalStateProvider(state_dir=tmp_path / "goal-state")


def test_env_bootstrap_seeds_goal(tmp_path):
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    doc = p.bootstrap_from_env({g.IDLE_GOAL_ENV: "Keep the e2e pipeline green."})
    assert doc is not None
    got = p.get()
    assert got["goal"] == "Keep the e2e pipeline green."
    assert got["source"] == g.SOURCE_ENV_BOOTSTRAP


@pytest.mark.asyncio
async def test_env_bootstrap_goal_is_due_before_default_cadence_elapses(tmp_path):
    """The fire-loop interval and the goal's repeat cadence are independent.

    A provisioned goal keeps the contract's one-hour repeat cadence, but its
    first contribution is due immediately because it has never been included.
    """
    from molecule_runtime.idle_digest.contract import AgeBand
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    doc = p.bootstrap_from_env({g.IDLE_GOAL_ENV: "Keep the e2e pipeline green."})

    assert doc is not None
    assert doc.cadence_seconds == g.CADENCE_DEFAULT_SECONDS == 3600
    [contribution] = await p.contribute()
    assert contribution.age_band is AgeBand.DUE


def test_env_bootstrap_idempotent_across_boots(tmp_path):
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    env = {g.IDLE_GOAL_ENV: "same goal"}
    assert p.bootstrap_from_env(env) is not None
    # every-boot re-seed with the same value is a no-op (no history churn)
    assert p.bootstrap_from_env(env) is None


def test_env_bootstrap_never_clobbers_agent_goal(tmp_path):
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    p.set("my own objective", set_by="agent", source=g.SOURCE_WORKSPACE_MCP)
    assert p.bootstrap_from_env({g.IDLE_GOAL_ENV: "provision goal"}) is None
    assert p.get()["goal"] == "my own objective"


def test_env_bootstrap_replaces_fleet_default(tmp_path):
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    p.set(g.FLEET_PERSONA_DEFAULT, set_by="provision", source=g.SOURCE_FLEET_DEFAULT)
    doc = p.bootstrap_from_env({g.IDLE_GOAL_ENV: "specific tenant objective"})
    assert doc is not None and p.get()["goal"] == "specific tenant objective"


def test_env_bootstrap_noop_when_unset(tmp_path):
    p = _env_provider(tmp_path)
    assert p.bootstrap_from_env({}) is None
    assert p.get() is None


def test_env_bootstrap_replaces_equal_rank_legacy_migration(tmp_path):
    # Both config.idle_prompt AND env set: migration runs first (rank 1), the
    # env seed then replaces it (equal rank, `>` blocks only strictly-higher).
    # A later "consistency" edit flipping > to >= would invert this contract.
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    p.migrate_from_config("legacy config objective")
    doc = p.bootstrap_from_env({g.IDLE_GOAL_ENV: "provision env objective"})
    assert doc is not None
    assert p.get()["goal"] == "provision env objective"
    assert p.get()["source"] == g.SOURCE_ENV_BOOTSTRAP


def test_env_bootstrap_changed_env_replaces_old_seed(tmp_path):
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    assert p.bootstrap_from_env({g.IDLE_GOAL_ENV: "objective v1"}) is not None
    assert p.bootstrap_from_env({g.IDLE_GOAL_ENV: "objective v2"}) is not None
    assert p.get()["goal"] == "objective v2"


def test_env_bootstrap_does_not_resurrect_agent_clear(tmp_path):
    # never-clobber-agent extends to a deliberate goal_clear: the provision
    # env must not resurrect a goal the agent explicitly retired.
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    env = {g.IDLE_GOAL_ENV: "provision objective"}
    assert p.bootstrap_from_env(env) is not None
    p.clear(set_by="agent")
    assert p.bootstrap_from_env(env) is None
    assert p.get() is None


def test_env_bootstrap_reseeds_after_provision_side_clear(tmp_path):
    # A NON-agent clear (provision/operator housekeeping) does not block.
    from molecule_runtime.idle_digest.providers import goal as g

    p = _env_provider(tmp_path)
    env = {g.IDLE_GOAL_ENV: "provision objective"}
    assert p.bootstrap_from_env(env) is not None
    p.clear(set_by="provision")
    assert p.bootstrap_from_env(env) is not None
