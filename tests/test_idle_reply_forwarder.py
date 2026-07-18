"""reply_forwarder — the digest/idle self-fire reply delivery seam.

Pins the fix for the silent-discard defect (observed live 2026-07-17): the
idle/digest poster used to read and throw away the A2A response body, so the
agent's reply to a self-initiated turn — including answers to user questions
absorbed into a digest turn — never reached the canvas while the model
believed delivery succeeded.
"""

from __future__ import annotations

import json

import pytest

from molecule_runtime.idle_digest.reply_forwarder import (
    IDLE_SENTINEL,
    MAX_FORWARD_CHARS,
    describe_reply,
    extract_reply_text,
    forward_reply_to_user,
    should_suppress,
)


# ---------------------------------------------------------------------------
# extract_reply_text
# ---------------------------------------------------------------------------


def _envelope(result) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": "1", "result": result}).encode()


def test_extract_message_result_parts():
    body = _envelope({"parts": [{"kind": "text", "text": "yo · hi"}]})
    assert extract_reply_text(body) == "yo · hi"


def test_extract_joins_multiple_text_parts():
    body = _envelope({"parts": [{"text": "line one"}, {"text": "line two"}]})
    assert extract_reply_text(body) == "line one\nline two"


def test_extract_task_status_message_shape():
    body = _envelope(
        {"status": {"state": "completed", "message": {"parts": [{"text": "done"}]}}}
    )
    assert extract_reply_text(body) == "done"


def test_extract_task_artifacts_shape():
    body = _envelope(
        {"artifacts": [{"parts": [{"text": "artifact answer"}]}], "status": {}}
    )
    assert extract_reply_text(body) == "artifact answer"


def test_extract_error_envelope_is_empty():
    body = json.dumps({"jsonrpc": "2.0", "error": {"message": "boom"}}).encode()
    assert extract_reply_text(body) == ""


@pytest.mark.parametrize("junk", [b"", b"not json", b"\xff\xfe", None, 42, ["x"]])
def test_extract_junk_is_empty(junk):
    assert extract_reply_text(junk) == ""


def test_extract_accepts_decoded_dict():
    assert extract_reply_text({"result": {"parts": [{"text": "hi"}]}}) == "hi"


# ---------------------------------------------------------------------------
# should_suppress — the silence valve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", IDLE_SENTINEL, "(idle).", "(IDLE)", " (idle) ",
        "(no response generated)",
        # decoration-only noise must never become a chat bubble
        "```", ".", "...", "`'\"",
        # the autonomous-loop guard's internal breaker notices (a2a_executor)
        "[autonomous replay suppressed — delegation result already delivered; no new info, ending turn]",
        "[autonomous loop halted — replay guard tripped]",
        "[Autonomous loop halted — x]",
    ],
)
def test_suppresses_sentinels_noise_and_guard_notices(text):
    assert should_suppress(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "yo · hi",
        "I'm idle but here's a status update",
        "(idle) — but one thing:",
        # a substantive one-word status reply is NOT the sentinel — only the
        # parenthesized contract form is (review #327)
        "idle",
        "Idle.",
        "'idle'",
    ],
)
def test_real_replies_are_not_suppressed(text):
    assert should_suppress(text) is False


# ---------------------------------------------------------------------------
# forward_reply_to_user — delivery + policy outcomes, never-raise
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    def __init__(self, status_code: int = 200, exc: Exception | None = None):
        self.status_code = status_code
        self.exc = exc
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url, json=None, headers=None):  # noqa: A002 — httpx shape
        if self.exc:
            raise self.exc
        self.calls.append((url, json, headers))
        return _FakeResp(self.status_code)


def _headers(_ws):
    return {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_forward_delivers_to_notify_endpoint():
    client = _FakeClient(200)
    status = await forward_reply_to_user(
        "http://platform:8080/", "ws-1", "yo · hi", auth_headers=_headers, client=client
    )
    assert status == "delivered"
    url, payload, headers = client.calls[0]
    assert url == "http://platform:8080/workspaces/ws-1/notify"
    assert payload == {"message": "yo · hi"}
    assert headers["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_forward_suppressed_never_posts():
    client = _FakeClient(200)
    status = await forward_reply_to_user(
        "http://p", "ws-1", IDLE_SENTINEL, auth_headers=_headers, client=client
    )
    assert status == "suppressed"
    assert client.calls == []


@pytest.mark.asyncio
async def test_forward_403_is_policy_not_error():
    status = await forward_reply_to_user(
        "http://p", "ws-1", "hello", auth_headers=_headers, client=_FakeClient(403)
    )
    assert status.startswith("skipped: talk_to_user disabled")


@pytest.mark.asyncio
async def test_forward_5xx_reports_error_without_raising():
    status = await forward_reply_to_user(
        "http://p", "ws-1", "hello", auth_headers=_headers, client=_FakeClient(502)
    )
    assert status == "error: platform returned 502"


@pytest.mark.asyncio
async def test_forward_exception_reports_error_without_raising():
    status = await forward_reply_to_user(
        "http://p",
        "ws-1",
        "hello",
        auth_headers=_headers,
        client=_FakeClient(exc=RuntimeError("conn refused")),
    )
    assert status == "error: conn refused"


# ---------------------------------------------------------------------------
# describe_reply — SSOT classification (queued/error are logged outcomes)
# ---------------------------------------------------------------------------


def test_describe_result():
    kind, text = describe_reply(_envelope({"parts": [{"kind": "text", "text": "hi"}]}))
    assert (kind, text) == ("result", "hi")


def test_describe_queued_poll_mode():
    body = json.dumps(
        {"jsonrpc": "2.0", "id": "1", "status": "queued", "delivery_mode": "poll", "method": "message/send"}
    ).encode()
    assert describe_reply(body) == ("queued", "")


def test_describe_queued_push_async_shape():
    # a2a_proxy.go's cap-and-queue envelope predates parse()'s queued check.
    body = json.dumps(
        {"status": "queued", "delivery_mode": "push-async", "method": "message/send"}
    ).encode()
    assert describe_reply(body) == ("queued", "")


def test_describe_queued_busy_push():
    assert describe_reply(json.dumps({"queued": True, "queue_id": "q1"}).encode()) == ("queued", "")


def test_describe_error_surfaces_message():
    body = json.dumps({"jsonrpc": "2.0", "error": {"message": "boom", "code": -32000}}).encode()
    kind, text = describe_reply(body)
    assert kind == "error"
    assert "boom" in text


def test_describe_malformed():
    assert describe_reply(b"not json")[0] == "malformed"


# ---------------------------------------------------------------------------
# forward length cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_caps_wall_of_text():
    client = _FakeClient(200)
    long_text = "x" * (MAX_FORWARD_CHARS * 3)
    status = await forward_reply_to_user(
        "http://p", "ws-1", long_text, auth_headers=_headers, client=client
    )
    assert status == "delivered"
    [(_, payload, _)] = client.calls
    assert len(payload["message"]) == MAX_FORWARD_CHARS
    assert payload["message"].endswith("[reply truncated]")
