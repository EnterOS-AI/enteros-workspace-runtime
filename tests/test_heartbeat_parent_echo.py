"""runtime#420 — the delegation harvester must not turn a parent's message into a loop.

Two paths fed the loop seen on prod 2026-10-30 (jrs seo-agent ↔ its concierge):

* the heartbeat polled the workspace's own a2a_receive feed for "delegation
  results". The platform proxy writes that row on the RECEIVER's feed for every
  message delivered to it, so each parent request was re-injected as
  "Delegation results are ready … Peer response from <parent>" seconds after the
  executor had already started on it, telling the child to delegate a summary
  back to the parent. The poll is gone.
* the delegation harvester told a child to report results back to its parent
  even when the result WAS the parent's reply.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.builtin_tools import delegation as delegation_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402


_WS = "00000000-0000-0000-0000-000000000420"
_PARENT = "00000000-0000-0000-0000-0000000004a0"
_PARENT_NAME = "JRS Auto Customs Agent"


def _make_resp(json_payload, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(get_payloads, post_recorder, get_urls=None):
    """GETs return ``get_payloads`` in order; every POST is recorded as (url, json)."""
    client = MagicMock()
    get_iter = iter(get_payloads)

    async def _get(url, params=None, headers=None):
        if get_urls is not None:
            get_urls.append(url)
        try:
            return next(get_iter)
        except StopIteration:
            return _make_resp([], status_code=200)

    async def _post(url, json=None, headers=None, timeout=None):
        post_recorder.append((url, json))
        return _make_resp({}, status_code=200)

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    client.aclose = AsyncMock()
    return client


def _texts(obj) -> str:
    """Every ``text`` value in a POST body, joined — the wake message the agent reads."""
    out: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "text" and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return "\n".join(out)


def _parent_lookup():
    """GET /workspaces/<self> then GET /workspaces/<parent>, as _resolve_parent issues them."""
    return [_make_resp({"parent_id": _PARENT}), _make_resp({"name": _PARENT_NAME})]


def _row(did, status="completed", target_id=None, summary="done"):
    row = {
        "delegation_id": did,
        "source_id": _WS,
        "status": status,
        "summary": summary,
        "response_preview": "",
        "error": "boom" if status == "failed" else "",
    }
    if target_id is not None:
        row["target_id"] = target_id
    return row


async def _wake_text(hb, rows) -> str:
    """Run one _check_delegations pass over ``rows``; return the single wake's text."""
    posts: list[tuple[str, dict]] = []
    client = _make_client(get_payloads=[_make_resp(rows), *_parent_lookup()], post_recorder=posts)
    await hb._check_delegations(client)
    a2a_posts = [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]
    assert len(a2a_posts) == 1, a2a_posts
    return _texts(a2a_posts[0][1])


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl"))
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    hb_mod.reset_awaited_delegations_for_test()
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
async def test_loop_polls_delegations_but_not_the_a2a_receive_feed(monkeypatch):
    """One loop pass reads /delegations only; inbound messages are never re-read as results."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    get_urls: list[str] = []
    client = _make_client(get_payloads=[], post_recorder=[], get_urls=get_urls)
    monkeypatch.setattr(hb_mod.httpx, "AsyncClient", lambda *a, **kw: client)

    class _StopAtFirstSleep:
        """heartbeat's own ``asyncio`` name only: the test's event loop keeps the real one."""

        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(_seconds):
            raise asyncio.CancelledError

    monkeypatch.setattr(hb_mod, "asyncio", _StopAtFirstSleep())

    with pytest.raises(asyncio.CancelledError):
        await hb._loop()

    assert get_urls, "the loop must still poll delegations"
    assert all(u.endswith(f"/workspaces/{_WS}/delegations") for u in get_urls), get_urls


@pytest.mark.asyncio
async def test_parent_reply_is_not_reported_back_to_parent():
    """An awaited delegation TO the parent completes: the wake must not say "report it to the parent"."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-to-parent")

    text = await _wake_text(hb, [_row("deleg-to-parent", target_id=_PARENT, summary="REPORT RECEIVED")])

    assert "Report these results back to your parent" not in text, text
    assert f"do not send them back to '{_PARENT_NAME}'" in text, text
    assert "send_message_to_user" in text, text


@pytest.mark.asyncio
async def test_parent_reply_without_target_id_is_recognised(monkeypatch):
    """GET /delegations' activity_logs fallback lists the completed delegate_result
    row without target_id; the callee comes from the local delegate_task_async record."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    monkeypatch.setitem(
        delegation_mod._delegations,
        "deleg-to-parent",
        delegation_mod.DelegationTask(
            task_id="deleg-to-parent", workspace_id=_PARENT, task_description="report"
        ),
    )

    text = await _wake_text(hb, [_row("deleg-to-parent", summary="REPORT RECEIVED")])

    assert "Report these results back to your parent" not in text, text
    assert f"do not send them back to '{_PARENT_NAME}'" in text, text


@pytest.mark.asyncio
async def test_failed_delegation_to_parent_is_still_reported():
    """A failed delegation to the parent is no reply: keep the report-back instruction."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-to-parent")

    text = await _wake_text(hb, [_row("deleg-to-parent", status="failed", target_id=_PARENT)])

    assert f"Report these results back to your parent '{_PARENT_NAME}'" in text, text
    assert "reply from your parent" not in text, text


@pytest.mark.asyncio
async def test_other_peer_result_is_still_reported_to_parent():
    """A worker's result for a delegation to a non-parent peer still goes up to the parent."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-to-peer")

    text = await _wake_text(hb, [_row("deleg-to-peer", target_id="peer-B", summary="Built the report")])

    assert f"Report these results back to your parent '{_PARENT_NAME}'" in text, text
    assert "Leave out" not in text, text


@pytest.mark.asyncio
async def test_mixed_batch_reports_only_the_other_results():
    """A parent reply and a peer result in one poll: report up, minus the marked parent reply."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-to-parent")
    hb_mod.register_awaited_delegation("deleg-to-peer")

    text = await _wake_text(hb, [
        _row("deleg-to-parent", target_id=_PARENT, summary="REPORT RECEIVED"),
        _row("deleg-to-peer", target_id="peer-B", summary="Built the report"),
    ])

    parent_line = next(l for l in text.splitlines() if "REPORT RECEIVED" in l)
    peer_line = next(l for l in text.splitlines() if "Built the report" in l)
    assert f"(reply from your parent '{_PARENT_NAME}')" in parent_line, text
    assert "reply from your parent" not in peer_line, text
    assert f"Report these results back to your parent '{_PARENT_NAME}'" in text, text
    assert f"Leave out the ones marked as replies from '{_PARENT_NAME}'" in text, text


@pytest.mark.asyncio
async def test_parent_lookup_is_cached_with_its_id():
    """_resolve_parent caches both fields from one lookup; no second GET inside the TTL."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    client = _make_client(get_payloads=_parent_lookup(), post_recorder=[])

    assert await hb._resolve_parent(client) == (_PARENT, _PARENT_NAME)
    assert await hb._resolve_parent(client) == (_PARENT, _PARENT_NAME)
    assert client.get.await_count == 2  # self + parent, once


@pytest.mark.asyncio
async def test_parent_lookup_is_reread_after_a_reparent(monkeypatch):
    """A canvas re-parent changes parent_id without a restart; the cache expires."""
    monkeypatch.setattr(hb_mod, "_PARENT_CACHE_SECONDS", 0)
    hb = HeartbeatLoop("http://test-platform", _WS)
    client = _make_client(
        get_payloads=[
            *_parent_lookup(),
            _make_resp({"parent_id": "new-parent"}), _make_resp({"name": "New Parent"}),
        ],
        post_recorder=[],
    )

    assert await hb._resolve_parent(client) == (_PARENT, _PARENT_NAME)
    assert await hb._resolve_parent(client) == ("new-parent", "New Parent")


@pytest.mark.asyncio
async def test_parent_lookup_failure_is_retried():
    """A failed lookup is not cached as "no parent" (the next cycle retries)."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    client = _make_client(
        get_payloads=[_make_resp({}, status_code=503), *_parent_lookup()],
        post_recorder=[],
    )

    assert await hb._resolve_parent(client) == ("", "")
    assert await hb._resolve_parent(client) == (_PARENT, _PARENT_NAME)
