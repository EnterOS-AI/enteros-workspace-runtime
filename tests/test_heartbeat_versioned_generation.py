"""Versioned-heartbeat / generation contract — producer unit tests.

Closes the RFC gap flagged in review of runtime PR #353 (merged): #353 added
the versioned-heartbeat producer (``schema_version`` + ``observed_generation``
facts-up) and the ``generation`` capture (desired-state-down) but shipped with
NO dedicated test. This pins the producer's three load-bearing behaviors:

1. EMIT: the heartbeat request body carries ``schema_version`` and
   ``observed_generation`` on BOTH the primary beat AND the 401-refresh-retry
   beat (the two ``client.post`` send paths in ``_send_heartbeat``).
2. CAPTURE (``_capture_generation_from_heartbeat``): a response's integer
   ``generation`` is stored into ``_last_seen_generation`` for the next echo,
   and the capture DEFENSIVELY rejects ``bool`` (a Python bool is an ``int``
   subclass — a JSON ``true`` is never a generation), non-int, and non-JSON /
   missing / non-dict bodies, leaving ``_last_seen_generation`` unchanged.
3. ROUND-TRIP: after a response carrying ``generation=N`` is captured, the NEXT
   emitted beat echoes ``observed_generation=N`` — on both the primary and the
   401-retry send paths.

Style mirrors ``tests/test_heartbeat_is_busy.py`` /
``tests/test_workspace_comms_conformance.py``: drive the REAL body builder
(``HeartbeatLoop._send_heartbeat``) / capture fn with an httpx-shaped capturing
stub, so the assertions are against the ACTUAL wire bytes, not a re-mock that
could omit the field. Fully offline — the transport is mocked; no live platform.
"""
from __future__ import annotations

from typing import Any

import httpx

from molecule_runtime.heartbeat import HEARTBEAT_SCHEMA_VERSION, HeartbeatLoop


# ── httpx-shaped capturing stubs (mirror the conformance gate) ───────────────


class _FakeResponse:
    """httpx.Response-shaped stub whose .json() returns a configurable body."""

    def __init__(self, body: Any = None) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self._body = body if body is not None else {"status": "ok"}

    def json(self) -> Any:
        return self._body


class _CapturingSyncClient:
    """Records the json= body of each POST; returns a fixed response body."""

    def __init__(self, response_body: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response_body = response_body

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": kwargs.get("json")})
        return _FakeResponse(self._response_body)


class _Retry401Client:
    """First POST raises a 401 HTTPStatusError (drives the refresh-and-retry-once
    path in _send_heartbeat); the retry POST succeeds. Records every body."""

    def __init__(self, response_body: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response_body = response_body

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": kwargs.get("json")})
        if len(self.calls) == 1:
            req = httpx.Request("POST", url)
            resp = httpx.Response(401, request=req)
            raise httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)
        return _FakeResponse(self._response_body)


def _new_loop() -> HeartbeatLoop:
    return HeartbeatLoop(
        platform_url="http://platform.test",
        workspace_id="0b8f2c4a-1d3e-4f56-9a7b-2c8d4e6f1a90",
    )


# ── 1. EMIT: both send paths carry schema_version + observed_generation ──────


def test_primary_beat_carries_schema_version_and_observed_generation() -> None:
    loop = _new_loop()
    client = _CapturingSyncClient()
    loop._send_heartbeat(client)

    assert client.calls and "/registry/heartbeat" in client.calls[-1]["url"]
    body = client.calls[-1]["json"]
    assert body["schema_version"] == HEARTBEAT_SCHEMA_VERSION
    # Nothing captured yet → the facts-up echo is the "no generation seen" 0.
    assert body["observed_generation"] == 0


def test_retry_beat_carries_schema_version_and_observed_generation() -> None:
    """The 401-refresh-retry beat is a SEPARATE body built inline; pin that it
    also carries the versioned fields (a regression that added them only to the
    primary body would leave the retry beat non-versioned)."""
    loop = _new_loop()
    client = _Retry401Client()
    loop._send_heartbeat(client)

    # Two POSTs: the 401 primary + the retry.
    assert len(client.calls) == 2
    retry_body = client.calls[-1]["json"]
    assert retry_body["schema_version"] == HEARTBEAT_SCHEMA_VERSION
    assert retry_body["observed_generation"] == 0


# ── 2. CAPTURE: store int, reject bool / non-int / non-JSON / missing ────────


def test_capture_stores_int_generation() -> None:
    loop = _new_loop()
    assert loop._last_seen_generation == 0
    loop._capture_generation_from_heartbeat(_FakeResponse({"generation": 42}))
    assert loop._last_seen_generation == 42


def test_capture_rejects_bool_generation() -> None:
    """Python bool is an int subclass — a JSON ``true``/``false`` must NOT be
    accepted as a generation, or observed_generation would echo True/False."""
    for flag in (True, False):
        loop = _new_loop()
        loop._last_seen_generation = 7  # prove it is left untouched
        loop._capture_generation_from_heartbeat(_FakeResponse({"generation": flag}))
        assert loop._last_seen_generation == 7


def test_capture_rejects_non_int_generation() -> None:
    for bad in ("5", 3.14, None, [1], {"n": 1}):
        loop = _new_loop()
        loop._last_seen_generation = 9
        loop._capture_generation_from_heartbeat(_FakeResponse({"generation": bad}))
        assert loop._last_seen_generation == 9, f"accepted non-int {bad!r}"


def test_capture_ignores_missing_generation_field() -> None:
    loop = _new_loop()
    loop._last_seen_generation = 3
    loop._capture_generation_from_heartbeat(_FakeResponse({"status": "ok"}))
    assert loop._last_seen_generation == 3


def test_capture_ignores_non_dict_body() -> None:
    loop = _new_loop()
    loop._last_seen_generation = 4
    loop._capture_generation_from_heartbeat(_FakeResponse(["not", "a", "dict"]))
    assert loop._last_seen_generation == 4


def test_capture_ignores_non_json_body() -> None:
    """A body whose .json() raises (non-JSON response) must not crash and must
    leave the last-seen generation unchanged."""

    class _NonJSONResponse:
        def json(self) -> Any:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    loop = _new_loop()
    loop._last_seen_generation = 11
    loop._capture_generation_from_heartbeat(_NonJSONResponse())
    assert loop._last_seen_generation == 11


# ── 3. ROUND-TRIP: captured generation N is echoed on the next beat ──────────


def test_roundtrip_next_beat_echoes_captured_generation() -> None:
    """Drive the REAL send path: a heartbeat whose response carries
    ``generation=17`` is captured on that beat (via _send_heartbeat's own
    _capture_generation_from_heartbeat call), and the NEXT beat echoes
    ``observed_generation=17``."""
    loop = _new_loop()
    client = _CapturingSyncClient(response_body={"generation": 17})

    loop._send_heartbeat(client)  # beat 1: emits 0, captures 17 from response
    first_body = client.calls[-1]["json"]
    assert first_body["observed_generation"] == 0
    assert loop._last_seen_generation == 17

    loop._send_heartbeat(client)  # beat 2: echoes the captured 17
    second_body = client.calls[-1]["json"]
    assert second_body["observed_generation"] == 17


def test_roundtrip_capture_on_retry_path_then_echo() -> None:
    """The 401-retry send path also captures the response generation, so a beat
    that only succeeded on retry still updates the echo for the following beat."""
    loop = _new_loop()
    retry_client = _Retry401Client(response_body={"generation": 23})
    loop._send_heartbeat(retry_client)  # succeeds on retry, captures 23
    assert loop._last_seen_generation == 23

    next_client = _CapturingSyncClient()
    loop._send_heartbeat(next_client)
    assert next_client.calls[-1]["json"]["observed_generation"] == 23
