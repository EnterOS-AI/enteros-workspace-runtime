"""Task #384 — heartbeat must not pollute canvas chat with raw delegation envelopes.

Symptom (pre-fix): every heartbeat cycle that observed a completed
delegation POSTed a ``Delegation completed: ...`` message to
``/workspaces/:id/notify`` (the canvas chat path). The agent self-
message path already wakes the agent for the same row, so the chat
push was pure noise — and when the activity_logs summary column was
already prefixed ``Delegation completed:`` upstream, the heartbeat
double-prefixed it (``Delegation completed: Delegation completed
(...)``). CTO empirical 2026-05-20 chloe-dong canvas: four such raw
envelopes in a row.

CTO decision (per ``feedback_surface_actionable_failure_reason_to_user``
+ ``feedback_decide_routine_prod_ops_no_go_ask``):

1. Drop SUCCESS chat pushes entirely. The agent's own self-message
   triggers it to call ``send_message_to_user`` with a synthesized
   NL summary — the agent owns the user-visible surface.
2. KEEP FAILURE chat pushes. If a delegation failed the agent might
   swallow it, so the canvas must still surface an actionable reason.

Tests below pin the new behaviour against both heartbeat poll paths:

* ``_check_delegations`` — the canonical ``/workspaces/:id/delegations``
  table walker.
* ``_check_activity_delegations`` — the ``activity_logs`` poller
  (task #354 fix; the path that #384 was attributable to).

Plus a regression for the double-prefix bug.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402


_WS = "00000000-0000-0000-0000-000000000384"


def _make_resp(json_payload, status_code: int = 200):
    """Build a stub httpx response with the .json() / .status_code shape
    that heartbeat consumes."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(get_payloads, post_recorder):
    """Build an AsyncMock httpx client that returns a queue of canned
    GET payloads (one per call, in order) and records every POST call.

    ``post_recorder`` is a list — each entry is ``(url, json_body)``.
    """
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


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect the results file + cursor file so the test never
    touches /tmp on the host. Also stub auth_headers so we don't hit
    the platform_auth disk cache."""
    monkeypatch.setattr(
        hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl")
    )
    monkeypatch.setattr(
        hb_mod, "_ACTIVITY_DELEGATION_CURSOR_FILE", str(tmp_path / "cursor")
    )
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    yield


@pytest.mark.asyncio
async def test_heartbeat_drops_success_delegation_chat_push():
    """When the canonical delegations endpoint returns a completed row,
    heartbeat must wake the agent via the A2A self-message path but
    must NOT POST to /notify. (Task #384 regression.)"""
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "delegation_id": "deleg-success-1",
                    "target_id": "peer-A",
                    "source_id": _WS,
                    "status": "completed",
                    "summary": "Built the report",
                    "response_preview": "Report body here.",
                    "error": "",
                },
            ]),
        ],
        post_recorder=posts,
    )

    await hb._check_delegations(client)

    notify_posts = [p for p in posts if "/notify" in p[0]]
    a2a_posts = [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]
    assert notify_posts == [], (
        f"completed delegation must NOT push to /notify; got {notify_posts!r}"
    )
    # Agent self-message still fires so the agent picks up the result.
    assert len(a2a_posts) == 1, (
        f"expected exactly one A2A self-message; got {a2a_posts!r}"
    )


@pytest.mark.asyncio
async def test_heartbeat_keeps_failure_delegation_chat_push():
    """When a delegation FAILED, heartbeat must still POST to /notify
    with the actionable reason so the user sees the failure on the
    canvas. (Per feedback_surface_actionable_failure_reason_to_user.)"""
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "delegation_id": "deleg-fail-1",
                    "target_id": "peer-B",
                    "source_id": _WS,
                    "status": "failed",
                    "summary": "",
                    "response_preview": "",
                    "error": "peer unreachable: connection refused",
                },
            ]),
        ],
        post_recorder=posts,
    )

    await hb._check_delegations(client)

    notify_posts = [p for p in posts if "/notify" in p[0]]
    assert len(notify_posts) == 1, (
        f"failed delegation must push to /notify exactly once; got {notify_posts!r}"
    )
    body = notify_posts[0][1]
    assert body.get("type") == "delegation_result"
    msg = body.get("message", "")
    # Status word + actionable reason both surfaced.
    assert "failed" in msg
    assert "connection refused" in msg


@pytest.mark.asyncio
async def test_heartbeat_no_double_prefix():
    """When the activity_logs ``summary`` column is already prefixed
    ``Delegation completed:`` upstream, the heartbeat must not stack
    another ``Delegation completed:`` on top. Regression for the
    chloe-dong canvas observation (task #384)."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    posts: list[tuple[str, dict]] = []
    # Force the failure-status branch by patching NOTIFY_STATUSES so the
    # /notify push fires for this synthetic ``completed`` row — this lets
    # us assert on the dedupe logic. We are NOT relaxing the success-
    # suppression contract; the other two tests pin that.
    with patch.object(hb_mod, "NOTIFY_STATUSES", frozenset({"completed"})):
        client = _make_client(
            get_payloads=[
                # 1st GET: /workspaces/:id/activity?type=a2a_receive — one peer row.
                _make_resp([
                    {
                        "id": "act-row-1",
                        "source_id": "peer-double-prefix",
                        "summary": "Delegation completed: peer ack ok",
                        "request_body": {"params": {"message": {"parts": []}}},
                    },
                ]),
                # 2nd GET: parent lookup — pretend no parent (empty dict).
                _make_resp({"parent_id": ""}),
            ],
            post_recorder=posts,
        )
        await hb._check_activity_delegations(client)

    notify_posts = [p for p in posts if "/notify" in p[0]]
    assert len(notify_posts) == 1
    msg = notify_posts[0][1]["message"]
    # Exactly ONE occurrence of "Delegation completed:" in the prefix
    # slot — no ``Delegation completed: Delegation completed:`` stacking.
    assert msg.count("Delegation completed:") == 1, (
        f"upstream prefix must be stripped before re-rendering; got {msg!r}"
    )
    # And the status word is present once via our new format, immediately
    # followed by the de-prefixed peer payload.
    assert msg.startswith("Delegation completed: peer ack ok"), msg
