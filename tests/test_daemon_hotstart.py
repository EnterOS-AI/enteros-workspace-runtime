"""Daemon hot-start (scheduler-as-trigger-plugin, per-workspace delivery).

A kind:trigger plugin installed AFTER boot (the moment a workspace gets its
first schedule) must arm its daemon WITHOUT a restart. Two seams:

  * ``DaemonSupervisor.supervise()`` adds daemons to a running supervisor and
    spawns monitors only for the new keys (idempotent, no-op once stopped);
  * ``DaemonRuntime.ensure_daemons()`` — the idempotent holder both boot and
    POST /internal/daemons/reload call — cold-starts on first daemon, and on a
    warm call binds/supervises only the NEW daemons while leaving running ones
    untouched; it sets/clears NATIVE_SCHEDULER_ENV so a hot-installed trigger
    flips the native-scheduler capability on.

Prove-fail: a supervise() that re-spawns existing daemons, an ensure_daemons()
that re-cold-starts (dropping live daemons) or forgets to flip the env, all fail
here.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from molecule_runtime import plugin_daemons
from molecule_runtime.plugin_daemons import DaemonSpec, DaemonSupervisor


def _wait_for(pred, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _fast_supervisor(specs, **overrides):
    kwargs = dict(
        backoff_base_seconds=0.05, backoff_cap_seconds=0.2, max_fast_failures=3,
        fast_failure_seconds=5.0, term_grace_seconds=2.0, poll_interval_seconds=0.02,
    )
    kwargs.update(overrides)
    return DaemonSupervisor(specs, **kwargs)


def _sleeper(out):
    script = (
        "import os, sys, time\n"
        f"open({str(out)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    return [sys.executable, "-c", script]


# --------------------------- DaemonSupervisor.supervise ---------------------


def test_supervise_adds_and_starts_new_daemon(tmp_path):
    out_a, out_b = tmp_path / "a.pid", tmp_path / "b.pid"
    a = DaemonSpec(name="a", plugin="p", command=_sleeper(out_a))
    b = DaemonSpec(name="b", plugin="p", command=_sleeper(out_b))
    sup = _fast_supervisor([a])
    sup.start()
    try:
        assert _wait_for(out_a.exists)
        added = sup.supervise([b])
        assert [s.key for s in added] == [b.key]
        assert _wait_for(out_b.exists)          # the new daemon actually spawned
        assert sup.states[a.key] == "running"   # original untouched
        assert sup.states[b.key] == "running"
    finally:
        sup.stop()


def test_supervise_is_idempotent_for_existing_key(tmp_path):
    out_a = tmp_path / "a.pid"
    a = DaemonSpec(name="a", plugin="p", command=_sleeper(out_a))
    sup = _fast_supervisor([a])
    sup.start()
    try:
        assert _wait_for(out_a.exists)
        first_pid = out_a.read_text()
        # Re-supervising the SAME key must not re-spawn or duplicate the spec.
        added = sup.supervise([a])
        assert added == []
        assert len([s for s in sup.specs if s.key == a.key]) == 1
        time.sleep(0.1)
        assert out_a.read_text() == first_pid   # not restarted
    finally:
        sup.stop()


def test_supervise_is_noop_once_stopped(tmp_path):
    a = DaemonSpec(name="a", plugin="p", command=_sleeper(tmp_path / "a.pid"))
    b = DaemonSpec(name="b", plugin="p", command=_sleeper(tmp_path / "b.pid"))
    sup = _fast_supervisor([a])
    sup.start()
    sup.stop()
    assert sup.supervise([b]) == []             # shutting down — adds nothing


# ------------------------- DaemonRuntime.ensure_daemons ---------------------


class _FakeSupervisor:
    def __init__(self, specs):
        self.specs = list(specs)
        self.started = 0
        self.supervised = []
    def start(self):
        self.started += 1
    def supervise(self, new_specs):
        existing = {s.key for s in self.specs}
        added = [s for s in new_specs if s.key not in existing]
        self.specs.extend(added)
        self.supervised.append([s.key for s in added])
        return added
    def stop(self):
        pass


class _FakeTransport:
    def __init__(self, app, specs, **kw):
        self.specs = list(specs)
        self.started = 0
        self.added = []
    async def start(self):
        self.started += 1
        return True
    async def add_specs(self, new_specs):
        self.added.append([s.key for s in new_specs])
        return [s.key for s in new_specs]
    def clear_daemon_env(self):
        pass
    async def stop(self):
        pass


def _patch_holder(monkeypatch, specs_sequence):
    """Patch the holder's collaborators; discover returns successive spec sets."""
    from molecule_runtime import channel_events, plugins
    calls = {"i": 0}

    def _discover(*a, **k):
        i = min(calls["i"], len(specs_sequence) - 1)
        calls["i"] += 1
        return list(specs_sequence[i])

    monkeypatch.setattr(plugins, "load_plugins", lambda **k: object())
    monkeypatch.setattr(plugin_daemons, "discover_daemon_specs", _discover)
    monkeypatch.setattr(plugin_daemons, "DaemonSupervisor", _FakeSupervisor)
    monkeypatch.setattr(channel_events, "ChannelEventSocketManager", _FakeTransport)

    async def _bound(*a, **k):
        return True

    monkeypatch.setattr(plugin_daemons, "wait_until_server_bound", _bound)


@pytest.mark.asyncio
async def test_ensure_cold_start_arms_trigger_and_sets_native_env(tmp_path, monkeypatch):
    monkeypatch.delenv(plugin_daemons.NATIVE_SCHEDULER_ENV, raising=False)
    trig = DaemonSpec(name="scheduler", plugin="molecule-scheduler", kind="trigger",
                      command=["python", "scheduler.py"])
    _patch_holder(monkeypatch, [[trig]])
    from molecule_runtime.daemon_runtime import DaemonRuntime

    holder = DaemonRuntime(app=object(), config_path=str(tmp_path), server=None)
    result = await holder.ensure_daemons()

    assert result["trigger"] is True
    assert trig.key in result["added"]
    assert holder.supervisor is not None and holder.supervisor.started == 1
    assert holder.event_transport.started == 1
    # G2: a trigger plugin flips the native-scheduler capability on.
    assert os.environ.get(plugin_daemons.NATIVE_SCHEDULER_ENV) == "1"


@pytest.mark.asyncio
async def test_ensure_no_specs_clears_native_env(tmp_path, monkeypatch):
    monkeypatch.setenv(plugin_daemons.NATIVE_SCHEDULER_ENV, "1")
    _patch_holder(monkeypatch, [[]])
    from molecule_runtime.daemon_runtime import DaemonRuntime

    holder = DaemonRuntime(app=object(), config_path=str(tmp_path), server=None)
    result = await holder.ensure_daemons()

    assert result == {"armed": 0, "added": [], "trigger": False}
    assert holder.supervisor is None
    assert plugin_daemons.NATIVE_SCHEDULER_ENV not in os.environ


@pytest.mark.asyncio
async def test_ensure_warm_add_supervises_only_new(tmp_path, monkeypatch):
    a = DaemonSpec(name="a", plugin="chan", kind="channel", command=["python", "-c", "1"])
    b = DaemonSpec(name="scheduler", plugin="molecule-scheduler", kind="trigger",
                   command=["python", "scheduler.py"])
    # First ensure sees {a}; second sees {a, b} — only b should be added.
    _patch_holder(monkeypatch, [[a], [a, b]])
    from molecule_runtime.daemon_runtime import DaemonRuntime

    holder = DaemonRuntime(app=object(), config_path=str(tmp_path), server=None)
    await holder.ensure_daemons()                 # cold: arms a
    cold_started = holder.supervisor.started
    result = await holder.ensure_daemons()        # warm: adds only b

    assert holder.supervisor.started == cold_started  # NOT re-cold-started
    assert holder.event_transport.added == [[b.key]]  # only b bound
    assert holder.supervisor.supervised[-1] == [b.key]
    assert result["trigger"] is True
    assert b.key in result["added"] and a.key not in result["added"]


@pytest.mark.asyncio
async def test_ensure_warm_noop_when_nothing_new(tmp_path, monkeypatch):
    a = DaemonSpec(name="a", plugin="chan", kind="channel", command=["python", "-c", "1"])
    _patch_holder(monkeypatch, [[a], [a]])
    from molecule_runtime.daemon_runtime import DaemonRuntime

    holder = DaemonRuntime(app=object(), config_path=str(tmp_path), server=None)
    await holder.ensure_daemons()
    result = await holder.ensure_daemons()

    assert result["added"] == []
    assert holder.event_transport.added == []     # no re-bind on a no-op reload
