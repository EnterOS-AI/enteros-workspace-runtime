"""RC #203 (correctness): the durable harvest tombstone must be committed ONLY
AFTER the delegation result is durably delivered — never before.

THE BUG (pre-fix): ``_check_delegations`` recorded the durable ``(id, status)``
tombstone the moment the row was harvested (inside the old ``_already_harvested``
check-and-set), BEFORE the result was written to ``DELEGATION_RESULTS_FILE`` and
BEFORE the self-wake POST. A transient failure AFTER that point — the result-file
write raising, or the self-wake POST raising — left the tombstone durably
committed, so a restart re-harvest saw the tombstone and PERMANENTLY suppressed
the result: the agent never got it.

THE FIX: the tombstone check is CHECK-ONLY (``_is_harvested``); the commit
(``_commit_harvested``) runs ONLY once the result was durably written AND the
self-wake was sent/scheduled. A delivery that raises leaves the tombstone
uncommitted, so the result is RE-HARVESTED on the next pass / after restart —
never silently dropped.

These tests pin both delivery-failure modes the reviewer called out.
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
from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402

_WS = "00000000-0000-0000-0000-0000deadbeef"
_PEER = "7ca64ad4-0000-0000-0000-000000000000"


def _make_resp(json_payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(delegations_payload, post_recorder, *, fail_a2a_post=False, a2a_status=200):
    """httpx AsyncClient stub. When ``fail_a2a_post`` is set, the self-wake POST
    to /a2a RAISES (simulating a transient wake-path failure). When ``a2a_status``
    is a non-2xx code the self-wake POST RETURNS that status WITHOUT raising
    (httpx only raises on transport errors, not on 4xx/5xx). Every other call
    behaves normally."""
    client = MagicMock()

    async def _get(url, params=None, headers=None):
        if url.endswith("/delegations"):
            return _make_resp(delegations_payload)
        return _make_resp({"parent_id": ""})

    async def _post(url, json=None, headers=None, timeout=None):
        is_a2a = url.endswith(f"/workspaces/{_WS}/a2a")
        if fail_a2a_post and is_a2a:
            raise RuntimeError("simulated self-wake POST failure")
        post_recorder.append((url, json))
        return _make_resp({}, status_code=(a2a_status if is_a2a else 200))

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
async def test_result_file_write_failure_reharvests_after_restart(tmp_path, monkeypatch):
    """Result-file write FAILS post-check: no durable tombstone is committed, and
    a restart RE-HARVESTS the result (delivers it) rather than suppressing it."""
    tombstones = tmp_path / ".molecule" / ".delegation_tombstones"

    # Session A: point the results file at a non-existent parent dir so the
    # durable append raises FileNotFoundError -> delivery fails post-check.
    monkeypatch.setattr(
        hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "no_such_dir" / "results.jsonl")
    )
    hb_a = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    posts_a: list = []
    await hb_a._check_delegations(_make_client([_row("d1")], posts_a))

    assert _self_msgs(posts_a) == [], "a failed-write harvest must not have woken the agent"
    assert not tombstones.exists() or "d1\tcompleted" not in tombstones.read_text(), (
        "the durable tombstone must NOT be committed when delivery failed"
    )

    # RESTART: fresh loop, writable results file, await re-registered. The
    # uncommitted tombstone means the result is re-harvested and delivered.
    good_results = tmp_path / "results.jsonl"
    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(good_results))
    hb_b = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    posts_b: list = []
    await hb_b._check_delegations(_make_client([_row("d1")], posts_b))

    assert len(_self_msgs(posts_b)) == 1, "the previously-failed result must be RE-HARVESTED, not suppressed"
    lines = good_results.read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["delegation_id"] == "d1"
    assert "d1\tcompleted" in tombstones.read_text(), "tombstone committed only after the successful delivery"


@pytest.mark.asyncio
async def test_self_wake_post_failure_reharvests_after_restart(tmp_path):
    """Self-wake POST FAILS post-write: the tombstone is not committed, so a
    restart RE-HARVESTS and re-attempts the wake instead of suppressing it."""
    tombstones = tmp_path / ".molecule" / ".delegation_tombstones"

    # Session A: result-file write succeeds, but the self-wake POST raises.
    hb_a = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d2")
    posts_a: list = []
    await hb_a._check_delegations(_make_client([_row("d2")], posts_a, fail_a2a_post=True))

    assert _self_msgs(posts_a) == [], "the wake POST raised, so no self-message was recorded"
    assert not tombstones.exists() or "d2\tcompleted" not in tombstones.read_text(), (
        "a failed wake POST must leave the tombstone uncommitted"
    )

    # RESTART: fresh loop, await re-registered, wake path now healthy.
    hb_b = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d2")
    posts_b: list = []
    await hb_b._check_delegations(_make_client([_row("d2")], posts_b))

    assert len(_self_msgs(posts_b)) == 1, "the un-woken result must be RE-HARVESTED after restart"
    assert "d2\tcompleted" in tombstones.read_text(), "tombstone committed only after the wake succeeded"


@pytest.mark.asyncio
async def test_self_wake_5xx_response_reharvests_after_restart(tmp_path):
    """RC #203 (Finding 2): the self-wake POST returns HTTP 5xx and does NOT
    raise. The old code checked ONLY for an exception, so the 5xx still committed
    the tombstone and PERMANENTLY suppressed the result. The fix confirms delivery
    on a 2xx status ONLY, so a 5xx leaves the tombstone uncommitted and the result
    is RE-HARVESTED (re-delivered) after a restart."""
    tombstones = tmp_path / ".molecule" / ".delegation_tombstones"

    # Session A: the result-file write succeeds, but the self-wake POST returns
    # HTTP 500 (a non-raising failure — the exact gap the reviewer called out).
    hb_a = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d3")
    posts_a: list = []
    await hb_a._check_delegations(_make_client([_row("d3")], posts_a, a2a_status=500))

    assert not tombstones.exists() or "d3\tcompleted" not in tombstones.read_text(), (
        "a 5xx self-wake must leave the durable tombstone UNcommitted"
    )

    # RESTART: fresh loop, await re-registered, wake path now healthy (2xx).
    hb_b = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d3")
    posts_b: list = []
    await hb_b._check_delegations(_make_client([_row("d3")], posts_b))

    assert len(_self_msgs(posts_b)) == 1, "the 5xx-failed result must be RE-HARVESTED after restart"
    assert "d3\tcompleted" in tombstones.read_text(), "tombstone committed only after the 2xx wake"
