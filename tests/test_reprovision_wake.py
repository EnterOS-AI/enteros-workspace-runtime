"""Unit tests for reprovision_wake — the §5.2 proactive-wake plugin-set diff.

Covers the durable-state contract:
  * first boot (no state) records SILENTLY — never announces;
  * an addition is announced exactly ONCE (consumed-once: the state is
    rewritten before the additions are returned);
  * removals / no-change never announce;
  * corrupt state degrades to first-boot (silent record);
  * hidden entries and files are not plugins;
  * a state-write failure fails SILENT (no groundhog-day announcements);
  * the wake note names the plugins and instructs the proactive announce.
"""
from __future__ import annotations

import json
from pathlib import Path

from molecule_runtime import reprovision_wake
from molecule_runtime.reprovision_wake import (
    STATE_FILENAME,
    build_wake_note,
    record_and_diff,
    resolve_state_path,
)


def _mk_plugin(plugins_dir: Path, name: str) -> None:
    d = plugins_dir / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(f"name: {name}\n")


def _state(tmp_path: Path) -> Path:
    return tmp_path / STATE_FILENAME


def test_first_boot_records_silently(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")

    additions = record_and_diff(plugins_dir=plugins, state_path=_state(tmp_path))

    assert additions == []  # first boot: greeting territory, not ours
    recorded = json.loads(_state(tmp_path).read_text())
    assert recorded["plugins"] == ["alpha"]


def test_addition_announced_exactly_once(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    state = _state(tmp_path)

    assert record_and_diff(plugins_dir=plugins, state_path=state) == []  # boot 1

    _mk_plugin(plugins, "beta")  # self-install lands before the reprovision
    assert record_and_diff(plugins_dir=plugins, state_path=state) == ["beta"]  # boot 2

    # Consumed-once: the very next boot (same tree) announces nothing.
    assert record_and_diff(plugins_dir=plugins, state_path=state) == []  # boot 3


def test_multiple_additions_sorted(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    state = _state(tmp_path)
    record_and_diff(plugins_dir=plugins, state_path=state)

    _mk_plugin(plugins, "zeta")
    _mk_plugin(plugins, "alpha")
    assert record_and_diff(plugins_dir=plugins, state_path=state) == ["alpha", "zeta"]


def test_removal_never_announces(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    _mk_plugin(plugins, "beta")
    state = _state(tmp_path)
    record_and_diff(plugins_dir=plugins, state_path=state)

    # De-declared plugin dropped by the boot-install full-replace swap.
    import shutil

    shutil.rmtree(plugins / "beta")
    assert record_and_diff(plugins_dir=plugins, state_path=state) == []
    assert json.loads(state.read_text())["plugins"] == ["alpha"]


def test_corrupt_state_degrades_to_silent_record(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    state = _state(tmp_path)
    state.write_text("{not json")

    assert record_and_diff(plugins_dir=plugins, state_path=state) == []
    assert json.loads(state.read_text())["plugins"] == ["alpha"]  # state healed


def test_wrong_shape_state_degrades_to_silent_record(tmp_path: Path):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    state = _state(tmp_path)
    state.write_text(json.dumps({"plugins": "alpha"}))  # not a list

    assert record_and_diff(plugins_dir=plugins, state_path=state) == []


def test_hidden_entries_and_files_are_not_plugins(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    state = _state(tmp_path)
    record_and_diff(plugins_dir=plugins, state_path=state)

    (plugins / ".relay-plugins").mkdir()  # hidden dir — never a plugin
    (plugins / "notes.txt").write_text("x")  # plain file — never a plugin
    _mk_plugin(plugins, "real")
    assert record_and_diff(plugins_dir=plugins, state_path=state) == ["real"]


def test_missing_plugins_dir_is_empty_set(tmp_path: Path):
    state = _state(tmp_path)
    assert record_and_diff(plugins_dir=tmp_path / "absent", state_path=state) == []
    assert json.loads(state.read_text())["plugins"] == []


def test_state_write_failure_fails_silent(tmp_path: Path, monkeypatch):
    plugins = tmp_path / "plugins"
    _mk_plugin(plugins, "alpha")
    state = _state(tmp_path)
    record_and_diff(plugins_dir=plugins, state_path=state)
    _mk_plugin(plugins, "beta")

    # If the consume (state rewrite) cannot persist, announcing would fire
    # again on EVERY boot — so the contract is: no persist, no announce.
    monkeypatch.setattr(reprovision_wake, "_write_plugin_set", lambda *_a, **_k: False)
    assert record_and_diff(plugins_dir=plugins, state_path=state) == []


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
