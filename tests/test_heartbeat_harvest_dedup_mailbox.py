"""Delegation-harvest DURABLE dedup (MUST-FIX 4), gated behind the mailbox
kernel. The completed/failed result tombstones go durable so a RESTART never
re-harvests a delegation the concierge already surfaced, and the (id, status)
key makes a status-flip (completed -> failed) a distinct one-time harvest.
Kernel OFF => no tombstone layer, byte-identical to today.

These complement test_heartbeat_harvest_active_await.py (process-scoped
active-await scoping); here we pin the additional DURABLE layer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime import autonomous_loop_guard as guard  # noqa: E402
from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402

_WS = "00000000-0000-0000-0000-0000deadbeef"
_PEER = "7ca64ad4-0000-0000-0000-000000000000"


def _make_resp(json_payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(delegations_payload, post_recorder):
    client = MagicMock()

    async def _get(url, params=None, headers=None):
        if url.endswith("/delegations"):
            return _make_resp(delegations_payload)
        return _make_resp({"parent_id": ""})

    async def _post(url, json=None, headers=None, timeout=None):
        post_recorder.append((url, json))
        return _make_resp({}, status_code=200)

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    return client


def _row(did, status="completed", summary="done"):
    return {
        "delegation_id": did,
        "target_id": _PEER,
        "source_id": _WS,
        "status": status,
        "summary": summary,
        "response_preview": "APPROVED",
        "error": "",
    }


def _self_msgs(posts):
    return [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl"))
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "_in_flight_delegation_ids", lambda: set())
    hb_mod.reset_awaited_delegations_for_test()
    # Mailbox kernel ON, durable state under tmp.
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / ".molecule"))
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
async def test_durable_tombstone_survives_restart(tmp_path):
    """A completed result harvested once is NOT re-harvested after a restart
    (fresh HeartbeatLoop, empty in-memory seen-set) — even when the await is
    re-registered — because the (id, status) tombstone is durable."""
    hb1 = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    posts1: list = []
    await hb1._check_delegations(_make_client([_row("d1")], posts1))
    assert len(_self_msgs(posts1)) == 1, "first harvest wakes the agent once"

    tombstones = tmp_path / ".molecule" / ".delegation_tombstones"
    assert tombstones.exists(), "harvest must persist a durable tombstone"
    assert "d1\tcompleted" in tombstones.read_text()

    # RESTART: brand-new loop (empty _seen_delegation_ids + _harvest_tombstones),
    # await re-registered, same completed row served again.
    hb2 = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    posts2: list = []
    await hb2._check_delegations(_make_client([_row("d1")], posts2))
    assert _self_msgs(posts2) == [], "durable tombstone blocks re-harvest across restart"


@pytest.mark.asyncio
async def test_status_flip_completed_then_failed_harvested_once_each(tmp_path):
    """(id, status) keying: after a COMPLETED harvest, a later FAILED row for
    the same delegation is a DISTINCT one-time harvest (the FAILED branch is
    not swallowed by the completed tombstone)."""
    hb1 = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d2")
    posts1: list = []
    await hb1._check_delegations(_make_client([_row("d2", status="completed")], posts1))
    assert len(_self_msgs(posts1)) == 1

    # Restart, re-await, now the row reports FAILED.
    hb2 = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d2")
    posts2: list = []
    await hb2._check_delegations(_make_client([_row("d2", status="failed")], posts2))
    assert len(_self_msgs(posts2)) == 1, "a status-flip to failed is harvested once"

    tombstones = (tmp_path / ".molecule" / ".delegation_tombstones").read_text()
    assert "d2\tcompleted" in tombstones
    assert "d2\tfailed" in tombstones


@pytest.mark.asyncio
async def test_kernel_off_has_no_tombstone_file(tmp_path, monkeypatch):
    """Kernel OFF: _already_harvested is a no-op, no durable tombstone file is
    written — byte-identical to the pre-migration harvester."""
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d3")
    posts: list = []
    await hb._check_delegations(_make_client([_row("d3")], posts))
    assert len(_self_msgs(posts)) == 1
    assert not (tmp_path / ".molecule" / ".delegation_tombstones").exists()


# ── MUST-FIX 2: the delegation-result injector is GATED by the runaway
# circuit breaker (should_halt) AND still FIRES when the breaker is closed ────


@pytest.fixture(autouse=True)
def _reset_breaker():
    guard._DEFAULT.__init__()
    yield
    guard._DEFAULT.__init__()


@pytest.mark.asyncio
async def test_injector_fires_when_breaker_closed(monkeypatch):
    """Kernel ON + breaker CLOSED (should_halt False): the delegation-result
    self-wake FIRES normally — the kernel gates but does not suppress."""
    monkeypatch.setattr(guard, "should_halt", lambda: False)
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("dfire")
    posts: list = []
    await hb._check_delegations(_make_client([_row("dfire")], posts))
    assert len(_self_msgs(posts)) == 1, "breaker closed -> injector fires"


@pytest.mark.asyncio
async def test_injector_dropped_when_breaker_open(monkeypatch):
    """Kernel ON + breaker OPEN (should_halt True): the self-wake is DROPPED
    BEFORE injection — the runaway breaker gates the kernel injector."""
    monkeypatch.setattr(guard, "should_halt", lambda: True)
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("ddrop")
    posts: list = []
    await hb._check_delegations(_make_client([_row("ddrop")], posts))
    assert _self_msgs(posts) == [], "breaker OPEN -> kernel drops the self-wake"


@pytest.mark.asyncio
async def test_injector_fires_kernel_off_even_when_breaker_open(monkeypatch):
    """Kernel OFF: the pre-injection kernel gate is skipped entirely, so the
    harvester fires EXACTLY as before (byte-identical) even if should_halt
    would be True — the legacy end-of-turn guard still governs the OUTPUT."""
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.setattr(guard, "should_halt", lambda: True)
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("doff")
    posts: list = []
    await hb._check_delegations(_make_client([_row("doff")], posts))
    assert len(_self_msgs(posts)) == 1, "kernel OFF -> gate skipped -> fires"
