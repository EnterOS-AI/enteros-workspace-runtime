"""Reconcile-on-boot schedule seeding (P4b): template schedules.yaml -> volume grid."""

from __future__ import annotations

from pathlib import Path

import pytest

from molecule_runtime import schedule_seed
from molecule_runtime.schedule_store import ScheduleStore

TRIGGER_MANIFEST = """\
name: scheduler
version: 1.0.0
description: native scheduler trigger plugin
kind: trigger
contributes:
  daemons:
    - name: tick
      command: python
      args: ["-m", "scheduler"]
"""

GRID_YAML = """\
schedules:
  - name: nightly-sweep
    cron: "0 3 * * *"
    prompt: run the nightly sweep
"""


def _write_trigger_plugin(plugins_dir: Path, name: str, grid: str | None) -> Path:
    d = plugins_dir / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(TRIGGER_MANIFEST)
    if grid is not None:
        (d / "schedules.yaml").write_text(grid)
    return d


def test_trigger_plugin_schedule_files_finds_shipped_grid(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write_trigger_plugin(plugins, "scheduler", GRID_YAML)
    _write_trigger_plugin(plugins, "no-grid", None)  # trigger plugin w/o schedules.yaml

    found = schedule_seed.trigger_plugin_schedule_files(
        workspace_plugins_dir=str(plugins), shared_plugins_dir=str(tmp_path / "empty")
    )
    assert [p.name for p in found] == ["schedules.yaml"]
    assert found[0].parent.name == "scheduler"


def test_seed_reconciles_into_volume_grid(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write_trigger_plugin(plugins, "scheduler", GRID_YAML)
    grid_path = tmp_path / "state" / "schedules.yaml"

    n = schedule_seed.seed_schedules_from_plugins(
        workspace_plugins_dir=str(plugins),
        shared_plugins_dir=str(tmp_path / "empty"),
        grid_path=grid_path,
    )
    assert n == 1
    entries = {e["name"]: e for e in ScheduleStore(grid_path).list()}
    assert entries["nightly-sweep"]["source"] == "template"
    assert entries["nightly-sweep"]["cron"] == "0 3 * * *"


def test_seed_preserves_user_edits_across_reprovision(tmp_path: Path, monkeypatch) -> None:
    grid_path = tmp_path / "schedules.yaml"
    src = tmp_path / "src.yaml"
    src.write_text(GRID_YAML)
    monkeypatch.setattr(
        schedule_seed, "trigger_plugin_schedule_files", lambda **kw: [src]
    )
    # A user creates their own schedule on the live grid.
    ScheduleStore(grid_path).create(
        {"name": "user-job", "cron": "0 * * * *", "prompt": "mine", "source": "runtime"}
    )
    assert schedule_seed.seed_schedules_from_plugins(grid_path=grid_path) == 1
    names = {e["name"]: e for e in ScheduleStore(grid_path).list()}
    assert names["user-job"]["prompt"] == "mine"  # preserved
    assert names["nightly-sweep"]["source"] == "template"  # seeded


def test_seed_no_trigger_plugin_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(schedule_seed, "trigger_plugin_schedule_files", lambda **kw: [])
    assert schedule_seed.seed_schedules_from_plugins(grid_path=tmp_path / "g.yaml") == 0


def test_seed_is_fail_soft_on_bad_template(tmp_path: Path, monkeypatch) -> None:
    grid_path = tmp_path / "schedules.yaml"
    bad = tmp_path / "bad.yaml"
    bad.write_text("schedules:\n  - name: x\n    cron: not-a-cron\n    prompt: p\n")
    monkeypatch.setattr(
        schedule_seed, "trigger_plugin_schedule_files", lambda **kw: [bad]
    )
    # Must not raise; bad source skipped, applied count 0, grid left empty.
    assert schedule_seed.seed_schedules_from_plugins(grid_path=grid_path) == 0
    assert ScheduleStore(grid_path).list() == []
