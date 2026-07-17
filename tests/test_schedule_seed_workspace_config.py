"""Workspace-config schedule seeding (scheduler RFC §8A P3 seam, runtime leg).

Core delivers template/org schedules INSIDE the provisioned ``config.yaml``
(top-level ``schedules:``) — the same config/asset channel that lands prompt
files, so the definitions are on the volume BEFORE boot (no core→runtime API
race on first provision). :func:`schedule_seed.seed_schedules_from_workspace_config`
reconciles that list into the volume grid via ``ScheduleStore.upsert_template``
(template semantics: runtime edits survive, tombstones respected).

Prove-fail properties covered here:
  * per-entry validate-and-skip (bad cron / missing name / oversize prompt);
  * prompt_file STRICTLY confined to the configs dir (absolute + traversal
    rejected — the traversal fixture points at a file that really EXISTS
    outside the confinement root, so removing the check seeds it and fails);
  * missing config.yaml / missing key / corrupt YAML = fail-soft no-op that
    leaves the grid file byte-identical (never raises past the seeder).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from molecule_runtime import schedule_seed
from molecule_runtime.schedule_store import ScheduleStore

TWO_SCHEDULES_YAML = """\
agent:
  name: demo
schedules:
  - name: daily-report
    cron: "0 9 * * *"
    prompt: write the daily report
  - name: weekly-digest
    cron_expr: "0 8 * * 1"
    prompt: write the weekly digest
    timezone: America/Vancouver
    enabled: false
"""

# A grid file NOT written by ScheduleStore: any rewrite through the store
# strips the comment, so byte-identity below proves "no write happened" —
# not merely "the same content was re-written".
HAND_EDITED_GRID = """\
# hand-edited grid — any store rewrite strips this comment
schedules:
- name: user-job
  cron: '0 * * * *'
  timezone: UTC
  prompt: mine
  enabled: true
  source: runtime
"""


def _write_config(configs_dir: Path, text: str) -> Path:
    configs_dir.mkdir(parents=True, exist_ok=True)
    path = configs_dir / "config.yaml"
    path.write_text(text)
    return path


def _store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "state" / "schedules.yaml")


def test_seed_happy_path_two_schedules(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, TWO_SCHEDULES_YAML)
    store = _store(tmp_path)
    assert store.list() == []  # precondition: nothing seeded yet

    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 2

    entries = {e["name"]: e for e in store.list()}
    assert set(entries) == {"daily-report", "weekly-digest"}
    assert entries["daily-report"]["source"] == "template"
    assert entries["daily-report"]["cron"] == "0 9 * * *"
    assert entries["daily-report"]["enabled"] is True  # default
    assert entries["weekly-digest"]["source"] == "template"
    assert entries["weekly-digest"]["cron"] == "0 8 * * 1"  # cron_expr alias mapped
    assert entries["weekly-digest"]["timezone"] == "America/Vancouver"
    assert entries["weekly-digest"]["enabled"] is False


def test_runtime_edit_survives_reseed(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, TWO_SCHEDULES_YAML)
    store = _store(tmp_path)
    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 2

    # The user edits a template-seeded entry — it becomes source='runtime'.
    store.update("daily-report", {"prompt": "user edited prompt"})

    # Re-provision re-runs the seeder over the SAME delivered config.
    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 2
    entries = {e["name"]: e for e in store.list()}
    assert entries["daily-report"]["prompt"] == "user edited prompt"  # NOT reverted
    assert entries["daily-report"]["source"] == "runtime"
    assert entries["weekly-digest"]["source"] == "template"  # sibling still template


def test_user_created_entry_with_template_name_is_preserved(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, TWO_SCHEDULES_YAML)
    store = _store(tmp_path)
    store.create({"name": "daily-report", "cron": "30 9 * * *", "prompt": "mine first"})

    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 2

    entries = {e["name"]: e for e in store.list()}
    assert entries["daily-report"]["prompt"] == "mine first"  # user entry untouched
    assert entries["daily-report"]["cron"] == "30 9 * * *"
    assert entries["daily-report"]["source"] == "runtime"
    assert entries["weekly-digest"]["source"] == "template"  # seeding still ran


def test_tombstoned_template_entry_not_resurrected(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, TWO_SCHEDULES_YAML)
    store = _store(tmp_path)
    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 2

    # The user deliberately deletes a template schedule — delete() tombstones it.
    assert store.delete("daily-report") is True

    schedule_seed.seed_schedules_from_workspace_config(configs, store)
    assert store.get("daily-report") is None  # tombstone respected, not re-seeded
    assert store.get("weekly-digest") is not None  # sibling proves the seeder ran


def test_prompt_file_traversal_and_absolute_rejected(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    # This file really EXISTS one level ABOVE the confinement root: without
    # the confinement check the entry would seed successfully, failing this test.
    (tmp_path / "secret.txt").write_text("outside the confinement root")
    (configs / "prompts").mkdir(parents=True)
    (configs / "prompts" / "ok.txt").write_text("confined prompt content")
    _write_config(
        configs,
        f"""\
schedules:
  - name: escape-relative
    cron: "0 1 * * *"
    prompt_file: ../secret.txt
  - name: escape-etc
    cron: "0 1 * * *"
    prompt_file: ../../etc/passwd
  - name: escape-absolute
    cron: "0 1 * * *"
    prompt_file: {str(tmp_path / 'secret.txt')!r}
  - name: confined-ok
    cron: "0 2 * * *"
    prompt_file: prompts/ok.txt
""",
    )
    store = _store(tmp_path)

    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 1

    entries = {e["name"]: e for e in store.list()}
    assert set(entries) == {"confined-ok"}  # every escapee skipped, sibling seeded
    assert entries["confined-ok"]["prompt"] == "confined prompt content"


def test_bad_cron_skipped_valid_sibling_seeded(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(
        configs,
        """\
schedules:
  - name: broken-cron
    cron: not-a-cron
    prompt: never seeds
  - name: good-job
    cron: "0 4 * * *"
    prompt: seeds fine
""",
    )
    store = _store(tmp_path)

    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 1

    entries = {e["name"]: e for e in store.list()}
    assert set(entries) == {"good-job"}


def test_missing_name_and_oversize_prompt_skipped(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    oversize = "x" * 20000  # > MAX_PROMPT_BYTES (16384)
    _write_config(
        configs,
        f"""\
schedules:
  - cron: "0 5 * * *"
    prompt: no name on this one
  - name: too-big
    cron: "0 5 * * *"
    prompt: {oversize}
  - name: good-job
    cron: "0 4 * * *"
    prompt: seeds fine
""",
    )
    store = _store(tmp_path)

    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 1

    entries = {e["name"]: e for e in store.list()}
    assert set(entries) == {"good-job"}


@pytest.mark.parametrize(
    "config_text",
    [
        "agent:\n  name: demo\n",  # no schedules key at all (every pre-PR-2 config)
        "schedules:\n",  # key present but null
        "schedules: []\n",  # key present but empty
        "schedules: not-a-list\n",  # malformed shape, not a list
    ],
)
def test_no_schedules_is_noop_grid_byte_identical(tmp_path: Path, config_text: str) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, config_text)
    grid_path = tmp_path / "state" / "schedules.yaml"
    grid_path.parent.mkdir(parents=True)
    grid_path.write_text(HAND_EDITED_GRID)
    before = grid_path.read_bytes()

    n = schedule_seed.seed_schedules_from_workspace_config(configs, ScheduleStore(grid_path))

    assert n == 0
    assert grid_path.read_bytes() == before  # not even a same-content rewrite


def test_missing_config_yaml_is_clean_noop(tmp_path: Path) -> None:
    configs = tmp_path / "configs"  # never created — no config.yaml at all
    store = _store(tmp_path)

    assert schedule_seed.seed_schedules_from_workspace_config(configs, store) == 0
    assert not store.path.exists()  # grid file never created


def test_corrupt_config_yaml_is_fail_soft(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, "schedules:\n  - name: x\n   cron: bad indent\n")
    grid_path = tmp_path / "state" / "schedules.yaml"
    grid_path.parent.mkdir(parents=True)
    grid_path.write_text(HAND_EDITED_GRID)
    before = grid_path.read_bytes()

    # Must return (0), never raise — a corrupt delivered config cannot block boot.
    n = schedule_seed.seed_schedules_from_workspace_config(configs, ScheduleStore(grid_path))

    assert n == 0
    assert grid_path.read_bytes() == before
