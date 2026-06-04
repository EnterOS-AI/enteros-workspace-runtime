"""Contract test: the runtime's ``/registry/register`` and
``/registry/heartbeat`` request bodies satisfy the REAL core-side
binding contract (the structs in workspace-server that decode them).

Why this test exists — the same blind spot class as #2251
=========================================================
The A2A ``message/send`` contract (test_a2a_message_send_contract.py)
was unverified because the receiver's Pydantic schema lived behind a
stub. The *register* / *heartbeat* wire shapes have the mirror-image
gap: the receiver is Go, so no Python test can import its struct, and
the existing Go tests in workspace-server/.../registry_test.go bind
HAND-WRITTEN JSON literals (``{"id":"ws-123","agent_card":{...}}``) —
NOT the bytes the runtime actually emits. So nothing pins
producer (this repo) → consumer (workspace-server ``RegisterPayload`` /
``HeartbeatPayload``).

If the runtime ever drops a key the Go side marks ``binding:"required"``
(``id`` + ``agent_card`` on register; ``workspace_id`` on heartbeat),
the workspace-server returns 400 "invalid request body" at boot — the
workspace registers as undialable (``workspaces.url`` left empty, the
internal#688 incident shape) or stops heartbeating. Neither side's unit
suite would catch it.

This test encodes the core struct's REQUIRED-FIELD contract as the SSOT
constant ``CORE_*_REQUIRED`` (kept byte-synced with
workspace-server/internal/models/workspace.go — see the file refs in
each constant's comment) and asserts the runtime's PRODUCED body carries
every required key, with the correct JSON spelling. The companion Go
test (registry_payload_contract_test.go) feeds the same golden bodies
through the real ``ShouldBindJSON`` so the two halves can't drift apart
silently: if the Go struct adds a required field, the Go test fails; if
the runtime drops one, this test fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime import main as runtime_main  # noqa: E402


# ---------------------------------------------------------------------------
# SSOT: the keys workspace-server marks ``binding:"required"`` on the structs
# that decode these bodies. Keep byte-synced with
# workspace-server/internal/models/workspace.go.
# ---------------------------------------------------------------------------

# RegisterPayload (workspace.go:66) — `id` and `agent_card` are
# binding:"required"; `url` is conditionally required (push-mode) and
# enforced by the handler, not the struct tag, so it's not in this set.
CORE_REGISTER_REQUIRED = {"id", "agent_card"}

# HeartbeatPayload (workspace.go:81) — only `workspace_id` is
# binding:"required"; every other field is optional / has a zero-value
# default on the Go side.
CORE_HEARTBEAT_REQUIRED = {"workspace_id"}

# DelegationHandler.Record (delegation.go:610) anonymous bind struct —
# target_id + task + delegation_id are all binding:"required". The runtime
# producer is _record_delegation_on_platform (builtin_tools/delegation.py:131).
CORE_DELEGATION_RECORD_REQUIRED = {"target_id", "task", "delegation_id"}


# ---------------------------------------------------------------------------
# Helpers to capture the REAL outbound bodies the runtime puts on the wire.
# ---------------------------------------------------------------------------

class _CapturingClient:
    """httpx.AsyncClient stand-in capturing the first POST json body."""

    def __init__(self, captured: dict):
        self._captured = captured

    async def post(self, url, json=None, headers=None, timeout=None):
        self._captured.setdefault("url", url)
        self._captured.setdefault("json", json)

        class _Resp:
            status_code = 200

            def json(self_inner):
                return {}

        return _Resp()

    async def aclose(self):
        return None


# ---------------------------------------------------------------------------
# 1. The register body the runtime emits carries every core-required key.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_body_satisfies_core_required_fields(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(runtime_main.asyncio, "sleep", _instant)

    captured: dict = {}
    client = _CapturingClient(captured)

    # Exactly the call main.py makes (main.py:515) with the hand-rolled
    # agent_card_dict shape (main.py:484).
    agent_card_dict = {
        "name": "pm",
        "description": "team lead",
        "version": "1.0.0",
        "url": "https://ws.example/a2a",
        "skills": [{"id": "coding", "name": "coding", "description": "coding", "tags": []}],
        "capabilities": {"streaming": True, "pushNotifications": False},
        "configuration_status": "ready",
    }
    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://platform.example",
        workspace_id="11111111-1111-1111-1111-111111111111",
        workspace_url="https://ws.example/a2a",
        agent_card=agent_card_dict,
        headers={},
    )
    assert ok is True

    body = captured["json"]
    assert captured["url"].endswith("/registry/register")
    missing = CORE_REGISTER_REQUIRED - set(body)
    assert not missing, (
        f"register body is missing core-required key(s) {missing}; "
        f"workspace-server RegisterPayload would 400. body keys={sorted(body)}"
    )
    # The required keys must be non-empty (Go binding:required rejects the
    # zero value "" for a string).
    assert body["id"], "register `id` must be non-empty (Go would reject \"\")"
    assert body["agent_card"], "register `agent_card` must be present/non-null"


def test_register_required_set_catches_dropped_id_regression():
    """Red→green proof: if a future refactor drops `id` from the register
    body, the contract check fails with a missing-key error (the same 400
    the Go side would raise)."""
    regressed_body = {
        # `id` intentionally dropped — the regression we're guarding against.
        "url": "https://ws.example/a2a",
        "agent_card": {"name": "pm"},
    }
    missing = CORE_REGISTER_REQUIRED - set(regressed_body)
    assert missing == {"id"}, (
        "the required-field contract must flag a dropped `id`; "
        f"got missing={missing}"
    )


# ---------------------------------------------------------------------------
# 2. The heartbeat body the runtime emits carries `workspace_id`.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_body_satisfies_core_required_fields(monkeypatch):
    """Drive HeartbeatLoop._loop for exactly one cycle and capture the real
    /registry/heartbeat POST body, then assert it carries every key the
    core HeartbeatPayload marks binding:"required"."""
    _WS = "00000000-0000-0000-0000-000000000688"
    captured: dict = {}

    # The loop builds its own httpx.AsyncClient; hand it our capturing one.
    monkeypatch.setattr(
        hb_mod.httpx, "AsyncClient", lambda *a, **kw: _CapturingClient(captured)
    )
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})

    # Break out of the infinite loop after the first heartbeat POST: the
    # loop calls asyncio.sleep(interval) once per cycle, so raise there.
    sleeps = {"n": 0}

    async def _sleep_then_cancel(_seconds):
        sleeps["n"] += 1
        import asyncio
        raise asyncio.CancelledError

    monkeypatch.setattr(hb_mod.asyncio, "sleep", _sleep_then_cancel)

    # The delegation checks fire their own GETs against the (post-only)
    # capturing client; no-op them so the cycle reaches the sleep cleanly.
    async def _noop(self, client):  # noqa: ANN001
        return None

    monkeypatch.setattr(hb_mod.HeartbeatLoop, "_check_delegations", _noop)
    monkeypatch.setattr(hb_mod.HeartbeatLoop, "_check_activity_delegations", _noop)

    hb = hb_mod.HeartbeatLoop("https://platform.example", _WS)
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await hb._loop()

    assert "json" in captured, "heartbeat loop did not emit a POST"
    body = captured["json"]
    assert captured["url"].endswith("/registry/heartbeat")
    missing = CORE_HEARTBEAT_REQUIRED - set(body)
    assert not missing, (
        f"heartbeat body is missing core-required key(s) {missing}; "
        f"workspace-server HeartbeatPayload would 400. body keys={sorted(body)}"
    )
    assert body["workspace_id"] == _WS, (
        "heartbeat must carry the workspace_id (binding:required on the Go side)"
    )


def test_heartbeat_required_set_catches_dropped_workspace_id_regression():
    """Red→green proof: a heartbeat body that drops workspace_id (e.g. a
    rename to `id`) is flagged by the contract check."""
    regressed_body = {
        # renamed workspace_id -> id, the exact kind of drift #2251 was.
        "id": "ws-688",
        "error_rate": 0.0,
        "active_tasks": 0,
    }
    missing = CORE_HEARTBEAT_REQUIRED - set(regressed_body)
    assert missing == {"workspace_id"}, (
        "the required-field contract must flag a renamed/dropped "
        f"workspace_id; got missing={missing}"
    )


# ---------------------------------------------------------------------------
# 3. The /delegations/record body carries every key the core Record handler
#    marks binding:"required" (target_id + task + delegation_id). Same #2251
#    class: the A2A send funnels through build_message_send_params (covered by
#    test_a2a_message_send_contract.py), but the platform-mirror record POST is
#    a separate hand-rolled body nothing pinned.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delegation_record_body_satisfies_core_required_fields(monkeypatch):
    import molecule_runtime.builtin_tools.delegation as dele

    captured: dict = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured.setdefault("url", url)
            captured.setdefault("json", json)

            class _R:
                status_code = 200

            return _R()

    monkeypatch.setattr(dele.httpx, "AsyncClient", lambda *a, **kw: _Client())

    await dele._record_delegation_on_platform(
        "task-abc",
        "22222222-2222-2222-2222-222222222222",
        "build the report",
    )

    body = captured["json"]
    assert captured["url"].endswith("/delegations/record")
    missing = CORE_DELEGATION_RECORD_REQUIRED - set(body)
    assert not missing, (
        f"delegations/record body missing core-required key(s) {missing}; "
        f"the Record handler would 400. body keys={sorted(body)}"
    )
    assert body["target_id"] and body["task"] and body["delegation_id"], (
        "all three Record-required keys must be non-empty (Go rejects \"\")"
    )


def test_delegation_record_required_set_catches_dropped_task_regression():
    """Red→green proof: dropping `task` from the record body is flagged."""
    regressed_body = {
        "target_id": "22222222-2222-2222-2222-222222222222",
        # `task` dropped — the regression.
        "delegation_id": "task-abc",
    }
    missing = CORE_DELEGATION_RECORD_REQUIRED - set(regressed_body)
    assert missing == {"task"}, (
        f"the required-field contract must flag a dropped `task`; got {missing}"
    )
