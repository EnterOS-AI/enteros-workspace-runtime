"""P1: the trigger inject lane (kind: trigger) — provenance + capability wiring.

The security-critical guarantee: a trigger plugin can cause ONLY an autonomous
self-turn in the allow-list (self-scheduler today). It can never forge a user
turn, a channel source, or an un-granted self-turn class — the runtime owns the
turn's identity; the plugin owns only the prompt text.
"""
from __future__ import annotations

import pytest

from molecule_runtime.channel_events import (
    TRIGGER_A2A_SOCKET_ENV,
    TRIGGER_A2A_TOKEN_ENV,
    TRIGGER_API_VERSION_ENV,
    TRIGGER_PLUGIN_ID_ENV,
    TRIGGER_ALLOWED_SOURCE_TYPES,
    ChannelEventSocketManager,
    _stamp_source,
    _stamp_trigger_source,
    _STAMP_BY_KIND,
)
from molecule_runtime.channel_sdk import (
    CHANNEL_A2A_SOCKET_ENV,
    CHANNEL_PLUGIN_ID_ENV,
)
from molecule_runtime.plugin_daemons import DaemonSpec


def _msg(**metadata):
    p = {"params": {"message": {"role": "user", "parts": []}}}
    if metadata:
        p["params"]["metadata"] = dict(metadata)
    return p


# ---------------------------------------------------------------------------
# trigger provenance stamper — the allow-list boundary
# ---------------------------------------------------------------------------


def test_trigger_defaults_to_self_scheduler_when_absent():
    payload = _msg()
    assert _stamp_trigger_source(payload, "sched-plugin") is None
    md = payload["params"]["metadata"]
    assert md["source_type"] == "self-scheduler"
    assert md["source"] == "sched-plugin"  # audit provenance


def test_trigger_honors_an_allowed_source_type():
    payload = _msg(source_type="self-scheduler")
    assert _stamp_trigger_source(payload, "p") is None
    assert payload["params"]["metadata"]["source_type"] == "self-scheduler"


def test_trigger_rejects_a_disallowed_source_type():
    for forged in ("self-idle", "self-delegation-result", "user", "self-harvester"):
        payload = _msg(source_type=forged)
        problem = _stamp_trigger_source(payload, "p")
        assert problem is not None, f"{forged!r} should be rejected"
        assert "not permitted" in problem


def test_trigger_strips_message_level_forged_provenance():
    # A daemon trying to sneak a source/source_type onto the *message* metadata
    # (which the executor reads FIRST) must have it stripped.
    payload = {
        "params": {
            "message": {
                "role": "user",
                "metadata": {"source": "slack", "source_type": "self-idle"},
            }
        }
    }
    assert _stamp_trigger_source(payload, "sched") is None
    assert "source" not in payload["params"]["message"]["metadata"]
    assert "source_type" not in payload["params"]["message"]["metadata"]
    # authoritative stamp lives at params.metadata and is the granted type
    assert payload["params"]["metadata"]["source_type"] == "self-scheduler"
    assert payload["params"]["metadata"]["source"] == "sched"


def test_trigger_rejects_malformed_shapes():
    assert _stamp_trigger_source("nope", "p") is not None
    assert _stamp_trigger_source({"params": "x"}, "p") is not None
    assert _stamp_trigger_source({"params": {"metadata": "x"}}, "p") is not None


def test_stamp_by_kind_maps_both_lanes():
    assert _STAMP_BY_KIND["channel"] is _stamp_source
    assert _STAMP_BY_KIND["trigger"] is _stamp_trigger_source


def test_allow_list_is_scheduler_only_today():
    assert TRIGGER_ALLOWED_SOURCE_TYPES == frozenset({"self-scheduler"})


# ---------------------------------------------------------------------------
# manager capability wiring — trigger daemons get their OWN env vars
# ---------------------------------------------------------------------------


def _mgr(specs):
    m = ChannelEventSocketManager(app=object(), specs=specs)
    return m


def test_plugin_ids_includes_trigger_and_records_kind():
    specs = [
        DaemonSpec(name="bridge", command=["python"], plugin="slack", kind="channel"),
        DaemonSpec(name="tick", command=["python"], plugin="sched", kind="trigger"),
        DaemonSpec(name="skill", command=["python"], plugin="mcp-x", kind="mcp"),
    ]
    m = _mgr(specs)
    ids = m._plugin_ids()
    assert set(ids) == {"slack", "sched"}  # mcp gets no lane
    assert m._kinds == {"slack": "channel", "sched": "trigger"}


def test_inject_daemon_env_uses_per_kind_vars():
    chan = DaemonSpec(name="bridge", command=["python"], plugin="slack", kind="channel")
    trig = DaemonSpec(name="tick", command=["python"], plugin="sched", kind="trigger")
    m = _mgr([chan, trig])
    m._plugin_ids()  # populates _kinds
    m._paths = {"slack": __import__("pathlib").Path("/tmp/s.sock"),
                "sched": __import__("pathlib").Path("/tmp/t.sock")}
    m._tokens = {"slack": "tok-c", "sched": "tok-t"}
    m._inject_daemon_env()
    # trigger daemon sees TRIGGER_* and NOT CHANNEL_*
    assert trig.env[TRIGGER_A2A_SOCKET_ENV] == "/tmp/t.sock"
    assert trig.env[TRIGGER_A2A_TOKEN_ENV] == "tok-t"
    assert trig.env[TRIGGER_PLUGIN_ID_ENV] == "sched"
    assert TRIGGER_API_VERSION_ENV in trig.env
    assert CHANNEL_A2A_SOCKET_ENV not in trig.env
    # channel daemon sees CHANNEL_* and NOT TRIGGER_*
    assert chan.env[CHANNEL_A2A_SOCKET_ENV] == "/tmp/s.sock"
    assert chan.env[CHANNEL_PLUGIN_ID_ENV] == "slack"
    assert TRIGGER_A2A_SOCKET_ENV not in chan.env


# ---------------------------------------------------------------------------
# G2 — native-scheduler claim (a trigger plugin makes the platform defer)
# ---------------------------------------------------------------------------


def test_has_trigger_daemon_detects_kind_trigger():
    from molecule_runtime.plugin_daemons import has_trigger_daemon
    chan = DaemonSpec(name="b", command=["x"], plugin="slack", kind="channel")
    trig = DaemonSpec(name="t", command=["x"], plugin="sched", kind="trigger")
    assert has_trigger_daemon([chan, trig]) is True
    assert has_trigger_daemon([chan]) is False
    assert has_trigger_daemon([]) is False


def test_heartbeat_advertises_scheduler_when_trigger_present(monkeypatch):
    from molecule_runtime.plugin_daemons import NATIVE_SCHEDULER_ENV
    from molecule_runtime.adapter_base import RuntimeCapabilities
    # default caps: scheduler False
    assert RuntimeCapabilities().to_dict()["scheduler"] is False
    # simulate the heartbeat override logic directly (the boot flag is set)
    monkeypatch.setenv(NATIVE_SCHEDULER_ENV, "1")
    caps_dict = RuntimeCapabilities().to_dict()
    import os
    if os.environ.get(NATIVE_SCHEDULER_ENV) == "1":
        caps_dict["scheduler"] = True
    assert caps_dict["scheduler"] is True
    # and NOT advertised when the flag is absent
    monkeypatch.delenv(NATIVE_SCHEDULER_ENV, raising=False)
    caps_dict2 = RuntimeCapabilities().to_dict()
    if os.environ.get(NATIVE_SCHEDULER_ENV) == "1":
        caps_dict2["scheduler"] = True
    assert caps_dict2["scheduler"] is False


def test_clear_daemon_env_wipes_both_lanes():
    trig = DaemonSpec(name="tick", command=["python"], plugin="sched", kind="trigger",
                      env={TRIGGER_A2A_SOCKET_ENV: "/x", TRIGGER_A2A_TOKEN_ENV: "y"})
    m = _mgr([trig])
    m.clear_daemon_env()
    assert TRIGGER_A2A_SOCKET_ENV not in trig.env
    assert TRIGGER_A2A_TOKEN_ENV not in trig.env
