"""Active-await scoping of the delegation-result harvester — root fix for the
recurring concierge RE-NARRATION loop (supersedes the watermark band-aid).

THE LOOP (confirmed, agents-team concierge): heartbeat._check_delegations read
EVERY completed delegation row where source_id==self, deduped only by the
IN-MEMORY _seen_delegation_ids set, wrote them to DELEGATION_RESULTS_FILE, and
fired a self-message that woke the concierge to NARRATE the backlog ("Reviewed
the 28 delegation results from peer 7ca64ad4 / codex-reviewer ... Summary of
backlog"). The tenant had 178 historical completed PR-review delegation rows;
because _seen_delegation_ids is process-scoped, every restart re-read all 178 →
re-narrated → loop. The same path also harvested codex-reviewer's autonomous
org-pr-review-sweep results, which the concierge never asked to be woken for.

FIX: the harvester surfaces a delegation result ONLY when the concierge has an
OPEN in-flight await for it — a delegation it initiated this process and is
genuinely awaiting (registered via register_awaited_delegation, or present in
the in-container in-flight registry). Process-scoped and EMPTY on a fresh boot,
so the loop dies at the SOURCE across restarts.

These tests pin the four contract points the principal called out:

  1. a PEER's autonomous / scheduled-sweep result (no open await) is NOT
     harvested / narrated;
  2. a genuinely user-awaited delegation the concierge initiated IS still
     delivered exactly once (and not re-delivered on a re-poll);
  3. on a (re)boot facing a backlog of historical completed rows the concierge
     already processed, ZERO are re-narrated;
  4. a restart (fresh HeartbeatLoop, empty await set) does not re-deliver an
     already-delivered result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402

_WS = "00000000-0000-0000-0000-0000deadbeef"
_PEER = "7ca64ad4-0000-0000-0000-000000000000"  # codex-reviewer in the incident


def _make_resp(json_payload, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(delegations_payload, post_recorder):
    """httpx AsyncClient stub: GET /delegations returns delegations_payload
    every time (the harvester polls it); any other GET (parent lookup) returns
    an empty/parentless body; POSTs are recorded as (url, json_body)."""
    client = MagicMock()

    async def _get(url, params=None, headers=None):
        if url.endswith("/delegations"):
            return _make_resp(delegations_payload)
        # parent-workspace lookup → no parent
        return _make_resp({"parent_id": ""})

    async def _post(url, json=None, headers=None, timeout=None):
        post_recorder.append((url, json))
        return _make_resp({}, status_code=200)

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    return client


def _delegation_row(did: str, status: str = "completed", summary: str = "PR review done"):
    return {
        "delegation_id": did,
        "target_id": _PEER,
        "source_id": _WS,  # the concierge initiated it (passes the source-id gate)
        "status": status,
        "summary": summary,
        "response_preview": "APPROVED",
        "error": "",
    }


def _a2a_self_messages(posts):
    return [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep the harvester off the host /tmp and the platform auth/disk cache,
    and start every case with an EMPTY await registry (the fresh-boot state)."""
    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl"))
    monkeypatch.setattr(hb_mod, "_ACTIVITY_DELEGATION_CURSOR_FILE", str(tmp_path / "cursor"))
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    # Neutralise the in-container in-flight registry so _active_await_delegation_ids
    # reflects ONLY what each test registers explicitly (hermetic).
    monkeypatch.setattr(hb_mod, "_in_flight_delegation_ids", lambda: set())
    hb_mod.reset_awaited_delegations_for_test()
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
async def test_peer_autonomous_sweep_result_not_harvested():
    """(1) A completed delegation row the concierge holds NO open await for —
    a peer's autonomous/scheduled org-pr-review-sweep result — must NOT be
    harvested: no DELEGATION_RESULTS_FILE write, no self-wake A2A message."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    client = _make_client([_delegation_row("sweep-deleg-1")], posts)

    await hb._check_delegations(client)

    assert _a2a_self_messages(posts) == [], (
        "a sweep result with no open await must not self-wake the concierge"
    )
    assert not Path(hb_mod.DELEGATION_RESULTS_FILE).exists(), (
        "no results file should be written for a non-awaited row"
    )


@pytest.mark.asyncio
async def test_user_awaited_result_delivered_exactly_once():
    """(2) A delegation the concierge initiated and is AWAITING is delivered
    exactly once: the result is written + one self-message fires; a second
    poll of the same row does NOT re-deliver."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("user-deleg-1")
    posts: list[tuple[str, dict]] = []
    client = _make_client([_delegation_row("user-deleg-1")], posts)

    await hb._check_delegations(client)

    assert len(_a2a_self_messages(posts)) == 1, "awaited result must wake the agent once"
    lines = Path(hb_mod.DELEGATION_RESULTS_FILE).read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["delegation_id"] == "user-deleg-1"

    # Re-poll the SAME completed row — already delivered (seen-set), and the
    # await was discarded on delivery: no second self-message, no duplicate row.
    await hb._check_delegations(client)
    assert len(_a2a_self_messages(posts)) == 1, "must not re-deliver an already-delivered result"
    lines2 = Path(hb_mod.DELEGATION_RESULTS_FILE).read_text().strip().splitlines()
    assert len(lines2) == 1, "results file must not gain a duplicate row"


@pytest.mark.asyncio
async def test_boot_with_historical_backlog_renarrates_zero():
    """(3) A fresh boot facing a backlog of historical completed rows the
    concierge already processed (no open awaits) re-narrates ZERO of them —
    the loop is dead at the source, not via a replay-suppressor."""
    hb = HeartbeatLoop("http://test-platform", _WS)  # fresh boot: empty await set
    posts: list[tuple[str, dict]] = []
    backlog = [_delegation_row(f"hist-{i}") for i in range(178)]
    client = _make_client(backlog, posts)

    await hb._check_delegations(client)

    assert _a2a_self_messages(posts) == [], (
        "a fresh boot must not re-narrate ANY of the 178 historical rows"
    )
    assert not Path(hb_mod.DELEGATION_RESULTS_FILE).exists()


@pytest.mark.asyncio
async def test_restart_does_not_redeliver():
    """(4) After a result is delivered once, a RESTART (new HeartbeatLoop with
    a fresh, empty await set) seeing the same completed row does not re-deliver
    it — this is what makes the fix hold across restarts."""
    posts: list[tuple[str, dict]] = []
    row = [_delegation_row("restart-deleg-1")]

    # Session A: concierge is awaiting it → delivered once.
    hb_a = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("restart-deleg-1")
    await hb_a._check_delegations(_make_client(row, posts))
    assert len(_a2a_self_messages(posts)) == 1

    # Simulate process restart: new loop, in-memory seen-set AND the await
    # registry are both empty again (process-scoped). The row is now history.
    hb_mod.reset_awaited_delegations_for_test()
    posts_b: list[tuple[str, dict]] = []
    hb_b = HeartbeatLoop("http://test-platform", _WS)
    await hb_b._check_delegations(_make_client(row, posts_b))

    assert _a2a_self_messages(posts_b) == [], (
        "a restarted concierge holds no await for the prior result — no re-delivery"
    )


@pytest.mark.asyncio
async def test_in_container_in_flight_registry_counts_as_await(monkeypatch):
    """The in-container in-flight registry (builtin delegate_task_async →
    _delegations) is a valid open-await source on its own, so a genuinely
    user-awaited builtin delegation is harvested WITHOUT any explicit
    register_awaited_delegation call (no change to the delegation tool needed)."""
    monkeypatch.setattr(
        hb_mod, "_in_flight_delegation_ids", lambda: {"builtin-deleg-1"}
    )
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    client = _make_client([_delegation_row("builtin-deleg-1")], posts)

    await hb._check_delegations(client)

    assert len(_a2a_self_messages(posts)) == 1, (
        "an in-flight builtin delegation must be harvestable via the registry"
    )
