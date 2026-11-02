"""runtime#424 — a delegation result is not marked delivered before its wake gets through.

hermes reads a result only from the heartbeat's self-wake (it never reads
DELEGATION_RESULTS_FILE), so before this fix it lost:

* a result harvested inside the 60 s self-message cooldown: marked seen, written
  and tombstoned, with no wake;
* a result whose wake POST did not get through: already marked seen, so it was
  never harvested again;

and the idle loops, which skipped while the results file was non-empty, skipped
forever once hermes had one result, because nothing ever consumes that file.
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime import mailbox_dir  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402


_WS = "00000000-0000-0000-0000-000000000424"
_PEER = "00000000-0000-0000-0000-0000000004b0"


def _make_resp(json_payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _row(did, status="completed"):
    return {
        "delegation_id": did,
        "target_id": _PEER,
        "source_id": _WS,
        "status": status,
        "summary": "done",
        "response_preview": "",
        "error": "boom" if status == "failed" else "",
    }


class _Platform:
    """Fake httpx client: /delegations lists ``rows``; each self-wake POST takes
    the next outcome from ``wake`` (an HTTP status, or an exception to raise)."""

    def __init__(self, rows, wake=(200,)):
        self.rows = list(rows)
        self.wake = list(wake)
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.client = MagicMock()
        self.client.get = AsyncMock(side_effect=self._get)
        self.client.post = AsyncMock(side_effect=self._post)

    async def _get(self, url, params=None, headers=None):
        self.gets.append(url)
        if url.endswith("/delegations"):
            return _make_resp(self.rows)
        return _make_resp({"parent_id": ""})

    async def _post(self, url, json=None, headers=None, timeout=None):
        if url.endswith(f"/workspaces/{_WS}/a2a"):
            outcome = self.wake.pop(0) if self.wake else 200
            self.posts.append((url, json))
            if isinstance(outcome, BaseException):
                raise outcome
            return _make_resp({}, status_code=outcome)
        self.posts.append((url, json))
        return _make_resp({})

    def wakes(self):
        return [p for p in self.posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]

    def notices(self):
        return [p for p in self.posts if p[0].endswith("/notify")]


def _result_lines():
    path = Path(hb_mod._delegation_results_file())
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _tombstones(tmp_path):
    path = tmp_path / ".molecule" / ".delegation_tombstones"
    return path.read_text() if path.exists() else ""


async def _poll(hb, platform, *, after_cooldown=True):
    if after_cooldown:
        hb._last_self_message_time = 0.0
    await hb._check_delegations(platform.client)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "_in_flight_delegation_ids", lambda: set())
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / ".molecule"))
    hb_mod.reset_awaited_delegations_for_test()
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
async def test_result_inside_the_cooldown_waits_for_the_next_poll(tmp_path):
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")])

    hb._last_self_message_time = time.time()  # a wake went out a moment ago
    await _poll(hb, platform, after_cooldown=False)

    assert platform.gets == [] and platform.wakes() == []
    assert _result_lines() == [], "nothing may be written without a wake"
    assert "d1" not in hb._seen_delegation_ids
    assert "d1\tcompleted" not in _tombstones(tmp_path)

    await _poll(hb, platform)

    assert len(platform.wakes()) == 1
    assert [r["delegation_id"] for r in _result_lines()] == ["d1"]
    assert "d1\tcompleted" in _tombstones(tmp_path)


@pytest.mark.asyncio
async def test_wake_that_did_not_get_through_is_retried_without_rewriting_its_result(tmp_path):
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")], wake=[503, 200])

    await _poll(hb, platform)

    assert len(platform.wakes()) == 1
    assert hb.delegation_results_waiting()
    assert "d1" not in hb._seen_delegation_ids
    assert "d1" in hb_mod._active_await_delegation_ids(), "the await must survive for the retry"
    assert "d1\tcompleted" not in _tombstones(tmp_path)

    # A poll inside the cooldown neither retries nor forgets the retry.
    await _poll(hb, platform, after_cooldown=False)
    assert len(platform.wakes()) == 1 and hb.delegation_results_waiting()

    await _poll(hb, platform)

    assert len(platform.wakes()) == 2
    assert not hb.delegation_results_waiting()
    assert [r["delegation_id"] for r in _result_lines()] == ["d1"], "written once, not per attempt"
    assert "d1\tcompleted" in _tombstones(tmp_path)
    assert "d1" not in hb_mod._active_await_delegation_ids()


@pytest.mark.asyncio
async def test_connect_error_is_retried():
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")], wake=[httpx.ConnectError("refused"), 200])

    await _poll(hb, platform)
    assert hb.delegation_results_waiting()

    await _poll(hb, platform)
    assert len(platform.wakes()) == 2 and not hb.delegation_results_waiting()


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried(tmp_path):
    """The agent may already be on it; a second wake is the worse outcome."""
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")], wake=[httpx.ReadTimeout("slow")])

    await _poll(hb, platform)
    await _poll(hb, platform)

    assert len(platform.wakes()) == 1
    assert not hb.delegation_results_waiting()
    assert "d1\tcompleted" not in _tombstones(tmp_path)


@pytest.mark.asyncio
async def test_retries_stop_after_the_cap():
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")], wake=[503] * 10)

    for _ in range(hb_mod._MAX_WAKE_ATTEMPTS + 2):
        await _poll(hb, platform)

    assert len(platform.wakes()) == hb_mod._MAX_WAKE_ATTEMPTS
    assert not hb.delegation_results_waiting()
    assert [r["delegation_id"] for r in _result_lines()] == ["d1"]


@pytest.mark.asyncio
async def test_a_retry_whose_row_is_gone_is_dropped():
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")], wake=[503])

    await _poll(hb, platform)
    assert hb.delegation_results_waiting()

    platform.rows = []  # fell out of the listing
    await _poll(hb, platform)
    assert not hb.delegation_results_waiting()


@pytest.mark.asyncio
async def test_failure_notice_is_pushed_once_not_per_attempt():
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1", status="failed")], wake=[503, 200])

    await _poll(hb, platform)
    assert platform.notices() == []

    await _poll(hb, platform)
    assert len(platform.notices()) == 1


@pytest.mark.asyncio
async def test_failed_result_write_is_harvested_again(tmp_path, monkeypatch):
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")])
    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "missing" / "results.jsonl"))

    await _poll(hb, platform)
    assert platform.wakes() == [] and "d1" not in hb._seen_delegation_ids

    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl"))
    await _poll(hb, platform)
    assert len(platform.wakes()) == 1


@pytest.mark.asyncio
async def test_waiting_does_not_depend_on_the_results_file():
    """hermes never consumes the file: once the wake is delivered, idle may run."""
    hb = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    platform = _Platform([_row("d1")])

    await _poll(hb, platform)

    assert _result_lines(), "the result stays in the file (no reader consumed it)"
    assert not hb.delegation_results_waiting()


def test_idle_loops_skip_on_the_heartbeat_signal():
    """Structural: both idle loops key on the heartbeat, not on the results file."""
    from molecule_runtime import main

    src = inspect.getsource(main)
    assert "skip_check=heartbeat.delegation_results_waiting," in src, "idle digest"
    assert "if heartbeat.delegation_results_waiting():" in src, "legacy idle loop"
    assert "_check_delegation_results_pending" not in src
