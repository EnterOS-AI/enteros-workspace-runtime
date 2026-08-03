"""The schedule grid must live on the DURABLE plugin-state root, not /configs.

molecule-ai-workspace-runtime#370 / molecule-ai/molecule-core#5036.

``workspaceTeardownVolumes`` (molecule-controlplane
``internal/provisioner/local_docker_workspace.go:795-806``) removes the ``/configs``
and ``/workspace`` named volumes on EVERY teardown — including a plain restart,
and including the ``config_dir: existing-volume`` path, which only means core
picked no template, not that the provisioner preserved the volume. Only
``mol-ws-pstate-*`` / ``mol-ws-rtstate-*`` survive (``keepRuntimeState=true``).

So a grid rooted at ``<configs_dir>/schedules`` is destroyed on every restart:
user-created ``source='runtime'`` schedules, the last-fire watermark, the poke
queue and the run history all go with it. These tests pin the grid onto the
durable root the plugin-state contract already ships, and pin the one-time
carry-over of an existing legacy grid so the fix itself cannot wipe a live
workspace's schedules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from molecule_runtime import plugin_state, trigger_state
from molecule_runtime.trigger_state import (
    GRID_FILENAME,
    STATE_DIR_ENV,
    TRIGGER_STATE_SUBDIR,
    resolve_trigger_state_dir,
)


@pytest.fixture(autouse=True)
def _reset_migration_flag() -> None:
    """The carry-over is once-per-process; each test gets a clean slate."""
    trigger_state.reset_migration_state()


def _declare_durable_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Act as the provisioner: declare a durable plugin-state root.

    The probe is stubbed DURABLE exactly as ``test_plugin_state.py`` does: a
    ``tmp_path`` on the test host is not a distinct mount, so the real probe
    would (correctly) refute the declaration and every case would degrade to the
    legacy root — the tests would pass while proving nothing about the re-root.
    """
    from molecule_runtime import mailbox_dir

    assert plugin_state.CONTRACT_AVAILABLE, "plugin-state contract must be vendored"
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV or "", str(root))
    monkeypatch.setattr(
        mailbox_dir, "probe_durability", lambda base=None: mailbox_dir.DURABILITY_DURABLE
    )


def test_probe_refuting_durability_keeps_the_legacy_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Downgrade-only: a declared root the probe refutes must NOT take the grid.

    Relocating onto a root that is not actually durable would swap a known-lossy
    location for an unknown-lossy one while looking like a fix.
    """
    from molecule_runtime import mailbox_dir

    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    configs = tmp_path / "configs"
    configs.mkdir()
    monkeypatch.setattr("molecule_runtime.configs_dir.resolve", lambda: configs)
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV or "", str(tmp_path / "pluginstate"))
    monkeypatch.setattr(
        mailbox_dir,
        "probe_durability",
        lambda base=None: mailbox_dir.DURABILITY_EPHEMERAL,
    )

    assert resolve_trigger_state_dir() == configs / TRIGGER_STATE_SUBDIR


def test_grid_lands_on_the_durable_root_not_configs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole defect: with a durable root declared, nothing resolves to /configs."""
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    configs = tmp_path / "configs"
    configs.mkdir()
    monkeypatch.setattr("molecule_runtime.configs_dir.resolve", lambda: configs)

    durable = tmp_path / "pluginstate"
    durable.mkdir()
    _declare_durable_root(monkeypatch, durable)

    resolved = resolve_trigger_state_dir()

    assert durable in resolved.parents, f"{resolved} is not under the durable root"
    assert configs not in resolved.parents, (
        f"{resolved} is still rooted on the provisioner-owned /configs volume, "
        "which workspaceTeardownVolumes destroys on every restart"
    )


def test_explicit_env_override_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator override is unchanged — it outranks the durable root."""
    _declare_durable_root(monkeypatch, tmp_path / "pluginstate")
    monkeypatch.setenv(STATE_DIR_ENV, "/custom/trigger/dir")
    assert resolve_trigger_state_dir() == Path("/custom/trigger/dir")


def test_legacy_configs_root_when_no_durable_root_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No provisioner declaration → byte-identical legacy behaviour.

    Durability is DECLARED by the provisioner and may only be downgraded. An
    older control plane that does not inject the root must keep working exactly
    as before rather than silently relocating the grid.
    """
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    monkeypatch.delenv(plugin_state.STATE_ROOT_ENV or "", raising=False)
    configs = tmp_path / "configs"
    configs.mkdir()
    monkeypatch.setattr("molecule_runtime.configs_dir.resolve", lambda: configs)

    assert resolve_trigger_state_dir() == configs / TRIGGER_STATE_SUBDIR


def test_existing_legacy_grid_is_carried_over_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fix must not wipe the very schedules it exists to preserve.

    A workspace upgrading to this runtime still has its grid (and watermark,
    pokes, history) on /configs from the previous boot. First resolve carries
    them onto the durable root.
    """
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    configs = tmp_path / "configs"
    legacy = configs / TRIGGER_STATE_SUBDIR
    legacy.mkdir(parents=True)
    (legacy / GRID_FILENAME).write_text(
        "schedules:\n"
        "- name: drain-coordinator-ledger\n"
        "  cron: '*/30 * * * *'\n"
        "  timezone: UTC\n"
        "  prompt: drain the ledger\n"
        "  enabled: true\n"
        "  source: runtime\n",
        encoding="utf-8",
    )
    (legacy / "schedule-state.json").write_text('{"last_tick": 123}', encoding="utf-8")
    monkeypatch.setattr("molecule_runtime.configs_dir.resolve", lambda: configs)

    durable = tmp_path / "pluginstate"
    durable.mkdir()
    _declare_durable_root(monkeypatch, durable)

    resolved = resolve_trigger_state_dir()

    carried = resolved / GRID_FILENAME
    assert carried.is_file(), "legacy grid was not carried onto the durable root"
    assert "drain-coordinator-ledger" in carried.read_text(encoding="utf-8")
    assert (resolved / "schedule-state.json").is_file(), (
        "the last-fire watermark was not carried — schedules would miss/double-fire"
    )

    # The user-authored entry survives a full round-trip through the store.
    from molecule_runtime.schedule_store import ScheduleStore

    entries = ScheduleStore(carried).load()
    assert [e["name"] for e in entries] == ["drain-coordinator-ledger"]
    assert entries[0]["source"] == "runtime"


def test_carryover_never_clobbers_a_live_durable_grid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale /configs copy must never overwrite the authoritative durable grid.

    /configs is re-seeded from the org template on every provision, so a legacy
    grid can REAPPEAR after the migration already ran. Copying it again would
    revert every edit the user made since.
    """
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    configs = tmp_path / "configs"
    legacy = configs / TRIGGER_STATE_SUBDIR
    legacy.mkdir(parents=True)
    (legacy / GRID_FILENAME).write_text("schedules: []\n", encoding="utf-8")
    monkeypatch.setattr("molecule_runtime.configs_dir.resolve", lambda: configs)

    durable = tmp_path / "pluginstate"
    durable.mkdir()
    _declare_durable_root(monkeypatch, durable)

    first = resolve_trigger_state_dir()
    live = first / GRID_FILENAME
    live.write_text(
        "schedules:\n"
        "- name: scan-site-visits\n"
        "  cron: '0 * * * *'\n"
        "  timezone: UTC\n"
        "  prompt: scan\n"
        "  enabled: true\n"
        "  source: runtime\n",
        encoding="utf-8",
    )

    trigger_state.reset_migration_state()  # simulate a fresh process
    second = resolve_trigger_state_dir()

    assert second == first
    assert "scan-site-visits" in live.read_text(encoding="utf-8"), (
        "a re-seeded /configs grid clobbered the live durable grid"
    )


def test_api_and_daemon_still_resolve_the_same_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API writes the grid the daemon reads — the invariant P3b established."""
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    configs = tmp_path / "configs"
    configs.mkdir()
    monkeypatch.setattr("molecule_runtime.configs_dir.resolve", lambda: configs)
    durable = tmp_path / "pluginstate"
    durable.mkdir()
    _declare_durable_root(monkeypatch, durable)

    from molecule_runtime.channel_events import ChannelEventSocketManager
    from molecule_runtime.internal_schedules import default_state_dir
    from molecule_runtime.plugin_daemons import DaemonSpec

    api_dir = default_state_dir()

    trig = DaemonSpec(name="tick", command=["python"], plugin="sched", kind="trigger")
    mgr = ChannelEventSocketManager(app=object(), specs=[trig])
    mgr._plugin_ids()
    mgr._paths = {"sched": Path("/tmp/t.sock")}
    mgr._tokens = {"sched": "tok"}
    mgr._inject_daemon_env()

    assert trig.env[STATE_DIR_ENV] == str(api_dir)
    assert durable in Path(trig.env[STATE_DIR_ENV]).parents
