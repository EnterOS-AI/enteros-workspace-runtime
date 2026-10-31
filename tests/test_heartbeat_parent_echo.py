"""runtime#420 — the delegation harvesters must not turn a parent's message into a loop.

Two paths fed the loop seen on prod 2026-10-30 (jrs seo-agent ↔ its concierge):

* ``_check_activity_delegations`` treated every peer a2a_receive row as a
  delegation result. The platform proxy writes that row on the RECEIVER's feed
  for every message delivered to it, so each parent request was re-injected as
  "Delegation results are ready … Peer response from <parent>" seconds after the
  executor had already started on it — and told the child to delegate a summary
  back to the parent.
* both harvesters told a child to report results back to its parent even when
  the result WAS the parent's reply.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402


_WS = "00000000-0000-0000-0000-000000000420"
_PARENT = "00000000-0000-0000-0000-0000000004a0"
_PARENT_NAME = "JRS Auto Customs Agent"


def _make_resp(json_payload, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(get_payloads, post_recorder):
    """GETs return ``get_payloads`` in order; every POST is recorded as (url, json)."""
    client = MagicMock()
    get_iter = iter(get_payloads)

    async def _get(url, params=None, headers=None):
        try:
            return next(get_iter)
        except StopIteration:
            return _make_resp([], status_code=200)

    async def _post(url, json=None, headers=None, timeout=None):
        post_recorder.append((url, json))
        return _make_resp({}, status_code=200)

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
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


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl"))
    monkeypatch.setattr(hb_mod, "_ACTIVITY_DELEGATION_CURSOR_FILE", str(tmp_path / "cursor"))
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    hb_mod.reset_awaited_delegations_for_test()
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["message/send", "message/stream"])
async def test_inbound_parent_request_is_not_reharvested(method):
    """The parent's request, as the proxy logs it on the child's feed, wakes nothing."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "id": "act-parent-req",
                    "activity_type": "a2a_receive",
                    "method": method,
                    "source_id": _PARENT,
                    "target_id": _WS,
                    "status": "ok",
                    "summary": f"{method} → seo-agent (ingest)",
                    "request_body": {"params": {"message": {"parts": [
                        {"kind": "text", "text": "PLATFORM ROOT — CTA_SPLIT: read this to the end."}
                    ]}}},
                },
            ]),
            *_parent_lookup(),
        ],
        post_recorder=posts,
    )

    await hb._check_activity_delegations(client)

    assert posts == [], f"an inbound request must not wake or notify; got {posts!r}"
    assert "act-parent-req" in hb._seen_activity_ids


@pytest.mark.asyncio
async def test_inbound_request_from_any_peer_is_not_reharvested():
    """Not only the parent: any peer's delivered request already ran as a turn."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "id": "act-peer-req",
                    "method": "message/send",
                    "source_id": "peer-sibling",
                    "status": "ok",
                    "summary": "message/send → Me (ingest)",
                    "request_body": {"params": {"message": {"parts": [
                        {"kind": "text", "text": "Receipt — no reply needed."}
                    ]}}},
                },
            ]),
        ],
        post_recorder=posts,
    )

    await hb._check_activity_delegations(client)

    assert posts == []
    assert "act-peer-req" in hb._seen_activity_ids


@pytest.mark.asyncio
async def test_parent_reply_is_not_reported_back_to_parent():
    """An awaited delegation TO the parent completes: the wake must not say "report it to the parent"."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-to-parent")
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "delegation_id": "deleg-to-parent",
                    "target_id": _PARENT,
                    "source_id": _WS,
                    "status": "completed",
                    "summary": "YOUR REPORT WAS RECEIVED AND VERIFIED",
                    "response_preview": "Next two units: …",
                    "error": "",
                },
            ]),
            *_parent_lookup(),
        ],
        post_recorder=posts,
    )

    await hb._check_delegations(client)

    a2a_posts = [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]
    assert len(a2a_posts) == 1, a2a_posts
    text = _texts(a2a_posts[0][1])
    assert "Report these results back to your parent" not in text, text
    assert f"do not send them back to '{_PARENT_NAME}'" in text, text


@pytest.mark.asyncio
async def test_other_peer_result_is_still_reported_to_parent():
    """A worker's result for a delegation to a non-parent peer still goes up to the parent."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-to-peer")
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "delegation_id": "deleg-to-peer",
                    "target_id": "peer-B",
                    "source_id": _WS,
                    "status": "completed",
                    "summary": "Built the report",
                    "response_preview": "Report body here.",
                    "error": "",
                },
            ]),
            *_parent_lookup(),
        ],
        post_recorder=posts,
    )

    await hb._check_delegations(client)

    a2a_posts = [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]
    assert len(a2a_posts) == 1, a2a_posts
    assert f"Report these results back to your parent '{_PARENT_NAME}'" in _texts(a2a_posts[0][1])


@pytest.mark.asyncio
async def test_parent_lookup_is_cached_with_its_id():
    """_resolve_parent caches both fields from one lookup; no second GET."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    client = _make_client(get_payloads=_parent_lookup(), post_recorder=[])

    assert await hb._resolve_parent(client) == (_PARENT, _PARENT_NAME)
    assert await hb._resolve_parent(client) == (_PARENT, _PARENT_NAME)
    assert client.get.await_count == 2  # self + parent, once


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
