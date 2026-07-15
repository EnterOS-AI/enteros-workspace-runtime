"""P3: the trigger state dir is one shared location for API + daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from molecule_runtime import trigger_state
from molecule_runtime.channel_events import ChannelEventSocketManager
from molecule_runtime.channel_sdk import TRIGGER_A2A_SOCKET_ENV
from molecule_runtime.plugin_daemons import DaemonSpec
from molecule_runtime.trigger_state import (
    STATE_DIR_ENV,
    TRIGGER_STATE_SUBDIR,
    resolve_trigger_state_dir,
)


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STATE_DIR_ENV, "/custom/trigger/dir")
    assert resolve_trigger_state_dir() == Path("/custom/trigger/dir")


def test_falls_back_to_configs_volume_subdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    import molecule_runtime.configs_dir as configs_dir

    monkeypatch.setattr(configs_dir, "resolve", lambda: Path("/configs"))
    assert resolve_trigger_state_dir() == Path("/configs") / TRIGGER_STATE_SUBDIR


def test_api_default_and_daemon_injection_resolve_the_same_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no override, both sides go through resolve_trigger_state_dir → the
    # same configs-volume subdir, so the API writes the grid the daemon reads.
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    import molecule_runtime.configs_dir as configs_dir

    monkeypatch.setattr(configs_dir, "resolve", lambda: Path("/configs"))

    from molecule_runtime.internal_schedules import default_state_dir

    api_dir = default_state_dir()

    trig = DaemonSpec(name="tick", command=["python"], plugin="sched", kind="trigger")
    mgr = ChannelEventSocketManager(app=object(), specs=[trig])
    mgr._plugin_ids()
    mgr._paths = {"sched": Path("/tmp/t.sock")}
    mgr._tokens = {"sched": "tok"}
    mgr._inject_daemon_env()

    assert trig.env[STATE_DIR_ENV] == str(api_dir)
    assert trig.env[STATE_DIR_ENV] == str(Path("/configs") / TRIGGER_STATE_SUBDIR)
    # sanity: the trigger socket was injected too (unchanged behavior)
    assert TRIGGER_A2A_SOCKET_ENV in trig.env
