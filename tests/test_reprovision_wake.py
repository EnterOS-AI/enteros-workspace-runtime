"""Unit tests for reprovision_wake — the §5.2 proactive-wake plugin-set diff.

Covers the staged-consumption contract (review 2026-07-05):
  * first boot (no state) records SILENTLY — never announces;
  * an addition is announced; the diff is NOT consumed by detection alone
    (a misconfigured boot that never schedules the send re-detects it);
  * mark_wake_attempt persists a bounded pre-send marker; consume clears it;
  * a marked-but-unconsumed announcement (crash mid-send) re-arms next boot
    with the attempt count carried, and is dropped LOUDLY after
    MAX_WAKE_ATTEMPTS;
  * removals / no-change / corrupt state never announce;
  * hidden entries and files are not plugins;
  * a pending plugin that has since been removed is not re-announced;
  * the wake note names the plugins and instructs the proactive announce.
"""
from __future__ import annotations

import json
from pathlib import Path

from molecule_runtime.reprovision_wake import (
    MAX_WAKE_ATTEMPTS,
    STATE_FILENAME,
    build_wake_note,
    consume_wake_note,
    mark_wake_attempt,
    prepare_wake_plan,
    resolve_state_path,
)


def _mk_plugin(plugins_dir: Path, name: str) -> None:
    d = plugins_dir / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(f"name: {name}\n")


def _state(tmp_path: Path) -> Path:
    return tmp_path / STATE_FILENAME


def _plan(tmp_path: Path):
    return prepare_wake_plan(plugins_dir=tmp_path / "plugins", state_path=_state(tmp_path))


def test_first_boot_records_silently(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")

    plan = _plan(tmp_path)

    assert plan.additions == []  # first boot: greeting territory, not ours
    recorded = json.loads(_state(tmp_path).read_text())
    assert recorded["plugins"] == ["alpha"]
    assert "pending" not in recorded


def test_addition_detected_but_not_consumed_by_detection(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _plan(tmp_path)  # boot 1 — baseline

    _mk_plugin(plugins, "beta")  # self-install lands before the reprovision
    plan2 = _plan(tmp_path)  # boot 2 — misconfigured: never schedules the send
    assert plan2.additions == ["beta"]
    assert plan2.attempts == 0

    # The state was NOT consumed by detection alone — the next boot must
    # re-detect the same announcement (this is the misconfigured-boot /
    # crash-before-scheduling deferral the review demanded).
    plan3 = _plan(tmp_path)
    assert plan3.additions == ["beta"]
    assert plan3.attempts == 0


def test_mark_then_consume_clears_announcement(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _plan(tmp_path)
    _mk_plugin(plugins, "beta")

    plan = _plan(tmp_path)
    assert plan.additions == ["beta"]
    assert mark_wake_attempt(plan)  # adapter ready, send scheduled
    pending = json.loads(_state(tmp_path).read_text())["pending"]
    assert pending == {"additions": ["beta"], "attempts": 1}

    assert consume_wake_note(plan)  # send succeeded
    st = json.loads(_state(tmp_path).read_text())
    assert "pending" not in st and st["plugins"] == ["alpha", "beta"]

    assert _plan(tmp_path).additions == []  # announced exactly once


def test_marked_but_unconsumed_rearms_with_attempt_carried(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _plan(tmp_path)
    _mk_plugin(plugins, "beta")

    plan = _plan(tmp_path)
    assert mark_wake_attempt(plan)
    # ... crash mid-send: consume never runs. Next boot re-detects with the
    # attempt count carried forward.
    plan2 = _plan(tmp_path)
    assert plan2.additions == ["beta"]
    assert plan2.attempts == 1


def test_replay_bounded_by_max_attempts(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _plan(tmp_path)
    _mk_plugin(plugins, "beta")

    for expected_attempts in range(MAX_WAKE_ATTEMPTS):
        plan = _plan(tmp_path)
        assert plan.additions == ["beta"], f"attempt round {expected_attempts}"
        assert plan.attempts == expected_attempts
        assert mark_wake_attempt(plan)  # send scheduled, crashes every time

    # Attempts exhausted: dropped loudly, state cleaned, no announcement.
    plan = _plan(tmp_path)
    assert plan.additions == []
    st = json.loads(_state(tmp_path).read_text())
    assert "pending" not in st and "beta" in st["plugins"]


def test_pending_plugin_since_removed_is_not_reannounced(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _plan(tmp_path)
    _mk_plugin(plugins, "beta")
    plan = _plan(tmp_path)
    assert mark_wake_attempt(plan)

    import shutil

    shutil.rmtree(plugins / "beta")  # de-declared before the retry boot
    plan2 = _plan(tmp_path)
    assert plan2.additions == []
    assert "pending" not in json.loads(_state(tmp_path).read_text())


def test_multiple_additions_sorted(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _plan(tmp_path)

    _mk_plugin(plugins, "zeta")
    _mk_plugin(plugins, "alpha")
    assert _plan(tmp_path).additions == ["alpha", "zeta"]


def test_removal_never_announces(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _mk_plugin(plugins, "beta")
    _plan(tmp_path)

    import shutil

    shutil.rmtree(plugins / "beta")  # boot-install full-replace dropped it
    assert _plan(tmp_path).additions == []
    assert json.loads(_state(tmp_path).read_text())["plugins"] == ["alpha"]


def test_corrupt_state_degrades_to_silent_record(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _state(tmp_path).write_text("{not json")

    assert _plan(tmp_path).additions == []
    assert json.loads(_state(tmp_path).read_text())["plugins"] == ["alpha"]  # healed


def test_wrong_shape_state_degrades_to_silent_record(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _state(tmp_path).write_text(json.dumps({"plugins": "alpha"}))  # not a list

    assert _plan(tmp_path).additions == []


def test_hidden_entries_and_files_are_not_plugins(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _plan(tmp_path)

    (plugins / ".relay-plugins").mkdir()  # hidden dir — never a plugin
    (plugins / "notes.txt").write_text("x")  # plain file — never a plugin
    _mk_plugin(plugins, "real")
    assert _plan(tmp_path).additions == ["real"]


def test_missing_plugins_dir_is_empty_set(tmp_path: Path):
    plan = prepare_wake_plan(plugins_dir=tmp_path / "absent", state_path=_state(tmp_path))
    assert plan.additions == []
    assert json.loads(_state(tmp_path).read_text())["plugins"] == []


def test_mark_wake_attempt_fails_closed_on_unwritable_state(tmp_path: Path, monkeypatch):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _plan(tmp_path)
    _mk_plugin(plugins, "beta")
    plan = _plan(tmp_path)
    assert plan.additions == ["beta"]

    # If the attempt marker cannot persist, the caller must not send —
    # an unpersisted attempt cannot be bounded (unbounded-replay guard).
    from molecule_runtime import reprovision_wake

    monkeypatch.setattr(reprovision_wake, "_write_state", lambda *_a, **_k: False)
    assert mark_wake_attempt(plan) is False


def test_mark_wake_attempt_refuses_empty_plan(tmp_path: Path):
    plan = prepare_wake_plan(plugins_dir=tmp_path / "plugins", state_path=_state(tmp_path))
    assert mark_wake_attempt(plan) is False  # nothing to announce


def test_resolve_state_path_uses_config_path_env():
    env = {"WORKSPACE_CONFIG_PATH": "/tmp/cfg"}
    assert resolve_state_path(env) == Path("/tmp/cfg") / STATE_FILENAME
    assert resolve_state_path({}) == Path("/configs") / STATE_FILENAME


def test_build_wake_note_names_plugins_and_instructs_proactive_announce():
    note = build_wake_note(["lark-channel-molecule", "beta"])
    assert "self-reprovisioned" in note
    assert "lark-channel-molecule, beta" in note
    # The operator contract: never wake silent — tell the user, then resume.
    assert "tell the user" in note
    assert "resume" in note
