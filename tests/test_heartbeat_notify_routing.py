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

Tests below pin the behaviour on ``_check_delegations``, the heartbeat's walk
of ``/workspaces/:id/delegations``. The ``activity_logs`` a2a_receive poller
that #384 was attributable to (and its double-prefix regression) is gone: every
row it read was a message delivered to the workspace (runtime#420).
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
    """Redirect the results file so the test never
    touches /tmp on the host. Also stub auth_headers so we don't hit
    the platform_auth disk cache."""
    monkeypatch.setattr(
        hb_mod, "DELEGATION_RESULTS_FILE", str(tmp_path / "results.jsonl")
    )
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    # Loop fix: _check_delegations now only harvests rows the concierge has an
    # OPEN in-flight await for. These #384/#138 tests assert the harvest
    # routing for a delegation the concierge initiated, so register that await
    # explicitly. Reset between cases for hermeticity (process-scoped registry).
    hb_mod.reset_awaited_delegations_for_test()
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
async def test_heartbeat_drops_success_delegation_chat_push():
    """When the canonical delegations endpoint returns a completed row,
    heartbeat must wake the agent via the A2A self-message path but
    must NOT POST to /notify. (Task #384 regression.)"""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-success-1")
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
    hb_mod.register_awaited_delegation("deleg-fail-1")
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
async def test_heartbeat_self_message_stamps_typed_marker():
    """Issue #138: the delegation-harvester self-message must carry
    source_type=self-harvester so the executor's drop/queue decision keys off
    the typed marker rather than text wording."""
    hb = HeartbeatLoop("http://test-platform", _WS)
    hb_mod.register_awaited_delegation("deleg-success-138")
    posts: list[tuple[str, dict]] = []
    client = _make_client(
        get_payloads=[
            _make_resp([
                {
                    "delegation_id": "deleg-success-138",
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

    a2a_posts = [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]
    assert len(a2a_posts) == 1
    params = a2a_posts[0][1]["params"]
    assert params["metadata"]["source_type"] == "self-harvester"
