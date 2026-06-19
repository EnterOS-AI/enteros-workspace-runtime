"""Behavioral tests for molecule_runtime/a2a_tools_messaging.py.

Regression coverage for molecule-core#1156 — closes the test gap that let
the broadcast / upload / notify regressions ship without a test pinning
the correct failure shape.

The runtime's full a2a package is stubbed in conftest.py (it's heavy +
lives only in the workspace image), so this module never hits the
network. Every test stubs ``httpx.AsyncClient`` + the three resolve
helpers (``_resolve_workspace_id``, ``_resolve_platform_url``,
``_auth_headers_for_heartbeat``) with monkeypatch — matches the style
of ``tests/test_a2a_message_send_contract.py``.

Coverage:
  * ``tool_broadcast_message`` — success path, 403 (broadcast ability
    disabled), empty message short-circuit.
  * ``tool_send_message_to_user`` (a.k.a. "talk to user") — upload
    failure short-circuit (no notify fired), platform error on /notify.
  * ``_upload_chat_files`` — OSError on file read, invalid (non-JSON
    or shape-mismatch) response.

All async tests use ``asyncio.run`` rather than pytest-asyncio (the
project deliberately does not depend on pytest-asyncio — the existing
contract test follows the same pattern).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid

import pytest


# --------------------------------------------------------------------------
# Test fakes — httpx stub that captures outbound POSTs and returns canned
# responses. Mirrors the pattern in test_a2a_message_send_contract.py.
# --------------------------------------------------------------------------

class _FakeResponse:
    """httpx.Response stand-in. status_code + text + json() are the only
    attrs the messaging code touches."""

    def __init__(self, status_code: int, body=None, raw_text: str | None = None):
        self.status_code = status_code
        self._body = body
        self._raw_text = raw_text

    def json(self):
        if self._raw_text is not None:
            raise ValueError(f"Invalid JSON: {self._raw_text}")
        return self._body

    @property
    def text(self) -> str:
        return self._raw_text if self._raw_text is not None else json.dumps(self._body or {})


class _FakeAsyncClient:
    """Context-manager async httpx client. Records every (url, json, files)
    POST and returns a queue of pre-loaded responses (one per call)."""

    def __init__(self, responses: list[_FakeResponse], captured: list[dict], timeout=None):
        self._responses = list(responses)
        self._captured = captured
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, files=None, headers=None):
        self._captured.append({"url": url, "json": json, "files": files, "headers": headers})
        if not self._responses:
            raise AssertionError(f"unexpected POST: {url}")
        return self._responses.pop(0)


# Each test sets WORKSPACE_ID to a deterministic UUID and stubs the
# three resolve helpers to deterministic values, so the URL on the wire
# is predictable and the assertion can match the exact path.
_WS = "11111111-1111-1111-1111-111111111111"
_BASE = "http://platform.local"


@pytest.fixture
def stub_resolve(monkeypatch):
    """Stub the three platform-resolve helpers used by every tool in the
    module. Returns a dict that records the calls so tests can assert
    resolution behavior independently of httpx."""
    calls = {"resolve_workspace_id": [], "resolve_platform_url": [], "auth": []}

    def _resolve_workspace_id():
        calls["resolve_workspace_id"].append(())
        return _WS

    def _resolve_platform_url(workspace_id):
        calls["resolve_platform_url"].append(workspace_id)
        return _BASE

    def _auth_headers_for_heartbeat(workspace_id):
        calls["auth"].append(workspace_id)
        return {"authorization": "token test"}

    import molecule_runtime.a2a_tools_messaging as m
    monkeypatch.setattr(m, "_resolve_workspace_id", _resolve_workspace_id)
    monkeypatch.setattr(m, "_resolve_platform_url", _resolve_platform_url)
    monkeypatch.setattr(m, "_auth_headers_for_heartbeat", _auth_headers_for_heartbeat)
    return calls


# ==========================================================================
# 1. tool_broadcast_message
# ==========================================================================

def test_broadcast_message_success_returns_delivered_count(monkeypatch, stub_resolve):
    """200 response with {"delivered": N} → human-readable success string."""
    import molecule_runtime.a2a_tools_messaging as m

    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            [_FakeResponse(200, {"delivered": 7})], captured, timeout=timeout,
        ),
    )

    result = asyncio.run(m.tool_broadcast_message("system ok"))

    assert result == "Broadcast sent to 7 workspace(s)"
    assert len(captured) == 1
    assert captured[0]["url"] == f"{_BASE}/workspaces/{_WS}/broadcast"
    assert captured[0]["json"] == {"message": "system ok"}
    assert captured[0]["headers"] == {"authorization": "token test"}


def test_broadcast_message_403_returns_ability_disabled_error(monkeypatch, stub_resolve):
    """403 → 'broadcast ability not enabled' with the hint from the body
    (CR2 contract: a workspace that has broadcast_enabled=false should
    get a clear hint, not a generic platform error)."""
    import molecule_runtime.a2a_tools_messaging as m

    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            [_FakeResponse(403, {"error": "broadcast_disabled",
                                  "hint": "ask an org admin to PATCH /workspaces/<id>/abilities"})],
            captured, timeout=timeout,
        ),
    )

    result = asyncio.run(m.tool_broadcast_message("test"))

    assert result.startswith("Error: broadcast ability not enabled")
    assert "PATCH /workspaces/<id>/abilities" in result


def test_broadcast_message_403_without_hint_still_returns_canonical_error(
    monkeypatch, stub_resolve,
):
    """403 with non-JSON or no hint → still the canonical 'broadcast
    ability not enabled' string with no trailing garbage (the conditional
    hint-append must not introduce a trailing space)."""
    import molecule_runtime.a2a_tools_messaging as m

    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            [_FakeResponse(403, {})], captured, timeout=timeout,  # no hint
        ),
    )

    result = asyncio.run(m.tool_broadcast_message("test"))

    assert result == "Error: broadcast ability not enabled."
    assert not result.endswith(" ")


def test_broadcast_message_empty_returns_required_error(monkeypatch, stub_resolve):
    """Empty / missing message short-circuits to a clear error string
    WITHOUT firing any HTTP request. This is the regression-guard
    clause — the prior shape fired the broadcast anyway and the
    platform surfaced a generic 400."""
    import molecule_runtime.a2a_tools_messaging as m

    captured: list[dict] = []

    # NOTE: we still stub AsyncClient so the test fails loudly if the
    # empty-message short-circuit is removed (a regression would call
    # the client and pop a response we never queued).
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient([], captured, timeout=timeout),
    )

    assert asyncio.run(m.tool_broadcast_message("")) == "Error: message is required"
    assert asyncio.run(m.tool_broadcast_message(None)) == "Error: message is required"
    assert captured == [], "empty message must NOT trigger any HTTP call"


# ==========================================================================
# 2. tool_send_message_to_user  (the "talk_to_user" surface)
# ==========================================================================

def test_send_message_to_user_upload_failure_short_circuits_no_notify(
    monkeypatch, stub_resolve, tmp_path,
):
    """When ``_upload_chat_files`` returns an error string, the tool must
    surface that error AND must NOT fire the /notify call. Half-rendering
    an attachment chip and a partial message was the regression in
    #1156's upload-failure path."""
    import molecule_runtime.a2a_tools_messaging as m

    p = tmp_path / "report.txt"
    p.write_text("hello")

    captured: list[dict] = []
    # No /notify response queued — a regression that fires notify after
    # an upload error would AssertionError here.
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            [_FakeResponse(500, {"error": "boom"})],  # only the upload response
            captured, timeout=timeout,
        ),
    )

    result = asyncio.run(
        m.tool_send_message_to_user(
            "Here's the report:", attachments=[str(p)],
        )
    )

    # The error string bubbles up from _upload_chat_files verbatim.
    assert "Error" in result
    assert "500" in result  # the upload returned 500
    # And critically: no /notify call was made.
    notify_calls = [c for c in captured if c["url"].endswith("/notify")]
    upload_calls = [c for c in captured if c["url"].endswith("/chat/uploads")]
    assert upload_calls, "upload must have been attempted"
    assert notify_calls == [], (
        "upload failure must short-circuit BEFORE firing /notify; "
        f"saw: {[c['url'] for c in captured]}"
    )


def test_send_message_to_user_platform_error_on_notify(
    monkeypatch, stub_resolve, tmp_path,
):
    """When the upload succeeds but the platform's /notify returns a
    non-200 status, the tool surfaces a clear platform error and the
    exact status code (caller can branch on the 403 talk_to_user_disabled
    vs other 4xx/5xx)."""
    import molecule_runtime.a2a_tools_messaging as m

    p = tmp_path / "log.txt"
    p.write_text("log content")

    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            [
                # upload OK
                _FakeResponse(200, {"files": [
                    {"uri": f"workspace:/{p.name}", "name": p.name,
                     "mimeType": "text/plain", "size": p.stat().st_size},
                ]}),
                # /notify: platform error
                _FakeResponse(500, {"error": "internal"}),
            ],
            captured, timeout=timeout,
        ),
    )

    result = asyncio.run(
        m.tool_send_message_to_user(
            "See attached", attachments=[str(p)],
        )
    )

    assert result == "Error: platform returned 500"
    notify_calls = [c for c in captured if c["url"].endswith("/notify")]
    assert len(notify_calls) == 1
    # The notify payload must include the attachment from the upload,
    # proving the call was reached (not short-circuited by an upload
    # error) before the platform rejected it.
    assert "attachments" in notify_calls[0]["json"]
    assert len(notify_calls[0]["json"]["attachments"]) == 1


# ==========================================================================
# 3. _upload_chat_files — internal helper
# ==========================================================================

def _run_upload(paths, fake_responses, monkeypatch, stub_resolve):
    """Drive _upload_chat_files end-to-end with a stubbed httpx client
    and the resolve helpers already stubbed by the ``stub_resolve``
    fixture. Returns the captured POST list and the function result.

    The helper takes the httpx client as a positional arg, so we
    construct the fake client once via the stubbed AsyncClient and
    pass it in (the function awaits ``client.post(...)`` directly).
    """
    import molecule_runtime.a2a_tools_messaging as m

    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx, "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            list(fake_responses), captured, timeout=timeout,
        ),
    )
    # _upload_chat_files takes the client as a positional arg; use the
    # same context-manager protocol the production code uses.
    async def _drive():
        async with m.httpx.AsyncClient(timeout=10.0) as client:
            return await m._upload_chat_files(client, paths, workspace_id=_WS)

    result = asyncio.run(_drive())
    return captured, result


def test_upload_chat_files_oserror_on_read(monkeypatch, stub_resolve, tmp_path):
    """A path that resolves but raises OSError on read (e.g. the file
    becomes unreadable between the isfile check and the read) must
    return ([], error_string) — NOT raise. The regression-guard for
    the read-failure surface."""
    p = tmp_path / "doomed.txt"
    p.write_text("data")  # exists for the isfile check

    # Force open() to raise OSError on the read attempt only.
    import builtins
    real_open = builtins.open

    def flaky_open(file, *args, **kwargs):
        if str(file) == str(p):
            raise OSError("simulated read failure (EIO)")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    captured, (uploaded, err) = _run_upload([str(p)], [], monkeypatch, stub_resolve)

    assert uploaded == []
    assert err is not None
    assert "Error reading" in err
    assert str(p) in err
    # The OSError is caught before the HTTP client is even touched.
    assert captured == [], "OSError on read must short-circuit before HTTP"


def test_upload_chat_files_invalid_response_non_json(monkeypatch, stub_resolve, tmp_path):
    """HTTP 200 but the body isn't JSON → returns ([], error_string).
    A real platform always returns JSON; a regression that ate the
    JSON-decode error and crashed the helper was the #1156 follow-up."""
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")

    captured, (uploaded, err) = _run_upload(
        [str(p)], [_FakeResponse(200, raw_text="<html>500</html>")],
        monkeypatch, stub_resolve,
    )

    assert uploaded == []
    assert err is not None
    assert "Error parsing upload response" in err


def test_upload_chat_files_invalid_response_shape_mismatch(
    monkeypatch, stub_resolve, tmp_path,
):
    """HTTP 200 with a JSON body whose ``files`` array length doesn't
    match the request count (e.g. platform dropped one file
    silently) → returns ([], error_string). The shape-mismatch check
    is the load-bearing guard against the partial-attach regression."""
    p1 = tmp_path / "a.txt"
    p1.write_text("a")
    p2 = tmp_path / "b.txt"
    p2.write_text("b")

    captured, (uploaded, err) = _run_upload(
        [str(p1), str(p2)],
        # Platform only echoes one file back — drop is silent at the
        # network layer, the helper must catch the count mismatch.
        [_FakeResponse(200, {"files": [
            {"uri": "workspace:/a.txt", "name": "a.txt",
             "mimeType": "text/plain", "size": 1},
        ]})],
        monkeypatch, stub_resolve,
    )

    assert uploaded == []
    assert err is not None
    assert "upload returned" in err
    assert "1" in err  # reported count
    assert "2" in err  # expected count


def test_upload_chat_files_success_returns_metadata(monkeypatch, stub_resolve, tmp_path):
    """Happy path — the round-trip returns the platform's metadata
    array and a None error. Pins the contract that the helper's
    success shape is (list[dict], None) so callers can branch on the
    error slot being None vs a string."""
    p = tmp_path / "ok.txt"
    p.write_text("ok")

    meta = [{"uri": f"workspace:/{p.name}", "name": p.name,
             "mimeType": "text/plain", "size": p.stat().st_size}]

    captured, (uploaded, err) = _run_upload(
        [str(p)], [_FakeResponse(200, {"files": meta})],
        monkeypatch, stub_resolve,
    )

    assert err is None
    assert uploaded == meta
    # The multipart files part carried exactly one entry.
    assert len(captured) == 1
    assert captured[0]["url"].endswith("/chat/uploads")
    assert isinstance(captured[0]["files"], list)
    assert captured[0]["files"][0][0] == "files"  # form field name
    assert captured[0]["files"][0][1][0] == p.name  # filename


def test_upload_chat_files_empty_paths_returns_empty_no_http(
    monkeypatch, stub_resolve,
):
    """No paths → no-op. The helper must not construct an httpx client
    or hit the platform at all when there is nothing to upload. This
    is the load-bearing optimization the broadcast path relies on
    (every broadcast with no attachments skips the upload round-trip)."""
    captured: list[dict] = []
    monkeypatch.setattr(
        __import__("molecule_runtime.a2a_tools_messaging", fromlist=["httpx"]).httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient([], captured, timeout=timeout),
    )

    import molecule_runtime.a2a_tools_messaging as m

    async def _drive():
        async with m.httpx.AsyncClient(timeout=10.0) as client:
            return await m._upload_chat_files(client, [], workspace_id=_WS)

    uploaded, err = asyncio.run(_drive())
    assert uploaded == []
    assert err is None
    assert captured == []
