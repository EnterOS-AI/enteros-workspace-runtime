"""Glue test for the a2a_executor auto-disable/stale hook (RFC invariant #8).

Verifies the guard logic in `_attribute_schedule_outcome`:
  - only a trigger fire (source_type=self-scheduler AND schedule_name) is attributed;
  - the mailbox scheduler tick (same source_type, no schedule_name) is ignored;
  - the 3rd consecutive provider error disables the schedule via set_enabled,
    preserving its source;
  - health bookkeeping never raises into the turn.

The runtime's `a2a` dep is pip-installed in CI but not locally; we inject a
minimal stub only when it is absent so this runs in both places.
"""

from __future__ import annotations

import sys
import types

import pytest

if "a2a" not in sys.modules:  # local-only minimal stub (CI has the real package)
    try:  # pragma: no cover - env probe
        import a2a  # noqa: F401
    except ModuleNotFoundError:
        def _mod(name):
            m = types.ModuleType(name)
            sys.modules[name] = m
            return m

        _mod("a2a")
        _mod("a2a.server")
        ae = _mod("a2a.server.agent_execution")
        ae.AgentExecutor = type("AgentExecutor", (), {})
        ae.RequestContext = type("RequestContext", (), {})
        ev = _mod("a2a.server.events")
        ev.EventQueue = type("EventQueue", (), {})
        tk = _mod("a2a.server.tasks")
        tk.TaskUpdater = type("TaskUpdater", (), {})
        ty = _mod("a2a.types")
        ty.Part = type("Part", (), {})
        hp = _mod("a2a.helpers")
        hp.new_text_message = lambda *a, **k: None

from types import SimpleNamespace  # noqa: E402

from molecule_runtime import a2a_executor as ax  # noqa: E402
from molecule_runtime import schedule_outcome as so  # noqa: E402
from molecule_runtime.schedule_store import ScheduleStore  # noqa: E402
from molecule_runtime.trigger_state import GRID_FILENAME  # noqa: E402


def _ctx(metadata):
    return SimpleNamespace(message=SimpleNamespace(metadata=metadata), metadata=None)


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLECULE_TRIGGER_STATE_DIR", str(tmp_path))
    return tmp_path


def _grid(state_dir):
    return ScheduleStore(state_dir / GRID_FILENAME)


def test_trigger_fire_empty_advances_stale_streak(state_dir):
    ctx = _ctx({"source_type": "self-scheduler", "schedule_name": "nightly"})
    ax._attribute_schedule_outcome(ctx, final_text="(no response generated)")
    hm = so.load_health_map(state_dir)
    assert hm["nightly"].consecutive_empty_runs == 1


def test_mailbox_scheduler_tick_without_name_is_ignored(state_dir):
    # same source_type, NO schedule_name → must not create any health entry
    ctx = _ctx({"source_type": "self-scheduler"})
    ax._attribute_schedule_outcome(ctx, final_text="(no response generated)")
    assert so.load_health_map(state_dir) == {}


def test_non_scheduler_source_type_is_ignored(state_dir):
    ctx = _ctx({"source_type": "self-cron", "schedule_name": "nightly"})
    ax._attribute_schedule_outcome(ctx, error_text="rate limit exceeded")
    assert so.load_health_map(state_dir) == {}


def test_third_provider_error_disables_preserving_source(state_dir):
    store = _grid(state_dir)
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    assert store.get("nightly")["source"] == "template"

    ctx = _ctx({"source_type": "self-scheduler", "schedule_name": "nightly"})
    ax._attribute_schedule_outcome(ctx, error_text="429 Too Many Requests")
    ax._attribute_schedule_outcome(ctx, error_text="rate limited")
    assert store.get("nightly")["enabled"] is True  # not yet
    ax._attribute_schedule_outcome(ctx, error_text="overloaded")
    got = store.get("nightly")
    assert got["enabled"] is False           # auto-disabled on the 3rd
    assert got["source"] == "template"       # source PRESERVED (not restamped)


def test_interleaved_error_empty_never_disables_end_to_end(state_dir):
    store = _grid(state_dir)
    store.create({"name": "sweep", "cron": "0 * * * *", "prompt": "go"})
    ctx = _ctx({"source_type": "self-scheduler", "schedule_name": "sweep"})
    # the negative-control sequence, driven through the real executor hook
    for kind in ["429", "", "429", "", "429", "", "429"]:
        if kind:
            ax._attribute_schedule_outcome(ctx, error_text=kind)
        else:
            ax._attribute_schedule_outcome(ctx, final_text="(no response generated)")
    assert store.get("sweep")["enabled"] is True  # never disabled


def test_internal_error_does_not_advance_disable_streak(state_dir):
    store = _grid(state_dir)
    store.create({"name": "sweep", "cron": "0 * * * *", "prompt": "go"})
    ctx = _ctx({"source_type": "self-scheduler", "schedule_name": "sweep"})
    for _ in range(5):
        ax._attribute_schedule_outcome(ctx, error_text="KeyError: 'boom'")
    assert store.get("sweep")["enabled"] is True  # internal bugs are neutral
    assert so.load_health_map(state_dir)["sweep"].consecutive_sdk_errors == 0


def test_hook_never_raises_on_broken_state(state_dir, monkeypatch):
    # even if persistence blows up, the turn must not see an exception
    monkeypatch.setattr(so, "record_and_persist", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    ctx = _ctx({"source_type": "self-scheduler", "schedule_name": "nightly"})
    ax._attribute_schedule_outcome(ctx, final_text="x")  # must not raise
