"""SDK-owned tool-call emit primitive (ADR-004 — shared tool-call display).

Pins the fire-and-forget / best-effort contract of
``molecule_runtime.tool_trace.emit_tool_call`` + ``summarize_tool``:

  (a) A valid call POSTs the exact six-key ``agent_log`` wire body
      ({activity_type, source_id, target_id, summary, status, method}) to
      ``{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity`` with the SSOT
      Bearer auth header — byte-parity with claude-code's ``_report_tool_use``
      so core#2636's ToolTraceChip reconstruction is identical per-runtime.
  (b) EVERY failure is swallowed and the call NEVER raises — a non-2xx
      response, a transport/connection error, an error constructing the
      client, and an unset ``WORKSPACE_ID`` (which makes the a2a_client
      PEP-562 resolver raise) all no-op without propagating. Losing the
      telemetry MUST NOT abort the tool or the turn.
  (c) The generic summary default is ``🛠 name(…)`` (the engine ships only
      the generic fallback; richer per-tool summaries live in each adapter).
  (d) A falsy ``name`` no-ops (no HTTP at all).

The emitter is async and constructs its own ``httpx.AsyncClient`` via a lazy
``import httpx`` inside the guarded body. We intercept it by monkeypatching
``httpx.AsyncClient`` with a factory that builds a real client over
``httpx.MockTransport`` (the same idiom test_boot_step_emit.py uses for the
sync client) and records every request.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import tool_trace  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Baseline: a resolvable workspace id + platform url and a token on file
    so the happy path fires. Individual tests override.
    """
    monkeypatch.setenv("WORKSPACE_ID", "ws-tooltrace-test-1")
    # Give auth_headers a token to attach (env fallback path — no /configs).
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "tooltrace-token-xyz")
    # platform_auth + a2a_client cache in-process; clear so env is re-read.
    from molecule_runtime import platform_auth

    platform_auth.clear_cache()
    platform_auth._reset_workspace_id_cache()
    import molecule_runtime.a2a_client as a2a_client

    a2a_client._WORKSPACE_ID_cache = None
    # PLATFORM_URL is a module-level constant frozen at a2a_client import
    # time (matches how the emitter reads it), so setting the env var here
    # would be too late. Patch the constant the emitter actually imports.
    monkeypatch.setattr(a2a_client, "PLATFORM_URL", "http://platform.test:8080")
    yield
    platform_auth.clear_cache()
    platform_auth._reset_workspace_id_cache()
    a2a_client._WORKSPACE_ID_cache = None


class _Recorder:
    """Records requests seen by the MockTransport and replays a scripted
    response (or raises a scripted exception)."""

    def __init__(self, *, response=None, raise_exc=None):
        self.requests: list[httpx.Request] = []
        self._response = response
        self._raise_exc = raise_exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response or httpx.Response(200, json={"status": "ok"})


def _install_async_client(monkeypatch, recorder: _Recorder):
    """Monkeypatch ``httpx.AsyncClient`` so the emitter's internally
    constructed (lazily imported) client routes through our MockTransport
    recorder. Swallows the ``timeout=`` kwarg the emitter passes.

    Captures the REAL ``httpx.AsyncClient`` before patching — the factory
    builds a real async client over a MockTransport; referencing the (patched)
    module attribute inside the factory would recurse.
    """
    _real_async_client = httpx.AsyncClient

    def _factory(*_args, **_kwargs):
        return _real_async_client(transport=httpx.MockTransport(recorder.handler))

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


# --------------------------------------------------------------------------- #
# (a) correct URL / body / status / auth
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_emits_correct_url_body_and_auth(monkeypatch):
    rec = _Recorder(response=httpx.Response(200, json={"status": "ok"}))
    _install_async_client(monkeypatch, rec)

    await tool_trace.emit_tool_call(name="Bash", summary="⚡ Bash(ls -la)")

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST"
    assert (
        str(req.url)
        == "http://platform.test:8080/workspaces/ws-tooltrace-test-1/activity"
    )
    body = json.loads(req.content)
    # Exactly these six keys — byte-parity with claude-code so core#2636's
    # ToolTraceChip reconstruction is identical across every runtime.
    assert body == {
        "activity_type": "agent_log",
        "source_id": "ws-tooltrace-test-1",
        "target_id": "ws-tooltrace-test-1",
        "summary": "⚡ Bash(ls -la)",
        "status": "ok",
        "method": "Bash",
    }
    # SSOT auth header (Bearer token) — the wsAuth trust boundary.
    assert req.headers.get("authorization") == "Bearer tooltrace-token-xyz"


@pytest.mark.asyncio
async def test_default_summary_is_generic_fallback(monkeypatch):
    # No summary= → the engine's generic 🛠 name(…) fallback is used.
    rec = _Recorder()
    _install_async_client(monkeypatch, rec)

    await tool_trace.emit_tool_call(name="Grep")

    body = json.loads(rec.requests[0].content)
    assert body["summary"] == "🛠 Grep(…)"
    assert body["method"] == "Grep"
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_status_passthrough(monkeypatch):
    rec = _Recorder()
    _install_async_client(monkeypatch, rec)

    await tool_trace.emit_tool_call(name="Write", status="error")

    assert json.loads(rec.requests[0].content)["status"] == "error"


@pytest.mark.asyncio
async def test_long_summary_capped(monkeypatch):
    # An adapter-supplied summary longer than the 256-char cap is truncated so
    # a giant tool-arg string can't write a multi-KB activity row.
    rec = _Recorder()
    _install_async_client(monkeypatch, rec)

    await tool_trace.emit_tool_call(name="Read", summary="x" * 5000)

    body = json.loads(rec.requests[0].content)
    assert len(body["summary"]) == 256


# --------------------------------------------------------------------------- #
# summarize_tool unit shape
# --------------------------------------------------------------------------- #
def test_summarize_tool_generic_shape():
    # The engine ships ONLY the generic fallback (U+1F6E0 + U+2026).
    assert tool_trace.summarize_tool("Edit") == "🛠 Edit(…)"
    # args are accepted but ignored by the v1 default.
    assert tool_trace.summarize_tool("Edit", {"file": "x"}) == "🛠 Edit(…)"


def test_summarize_tool_caps_long_name():
    assert len(tool_trace.summarize_tool("T" * 5000)) == 256


# --------------------------------------------------------------------------- #
# (b) failures swallowed, never raises
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_swallows_non_2xx(monkeypatch):
    # A non-2xx (e.g. platform rejects the row) MUST NOT raise — telemetry
    # failure must never abort the tool or the turn.
    rec = _Recorder(response=httpx.Response(404, text="not found"))
    _install_async_client(monkeypatch, rec)

    # No exception escapes.
    await tool_trace.emit_tool_call(name="Bash")
    assert len(rec.requests) == 1  # it did attempt the POST


@pytest.mark.asyncio
async def test_swallows_connection_error(monkeypatch):
    rec = _Recorder(raise_exc=httpx.ConnectError("connection refused"))
    _install_async_client(monkeypatch, rec)

    # A transport error (platform unreachable) is swallowed, not raised.
    await tool_trace.emit_tool_call(name="Bash")
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_swallows_client_construction_error(monkeypatch):
    # Even a failure constructing the client (unexpected) must never escape.
    def _boom(*_a, **_k):
        raise RuntimeError("client blew up")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    # No assertion beyond "did not raise".
    await tool_trace.emit_tool_call(name="Bash")


@pytest.mark.asyncio
async def test_swallows_unset_workspace_id(monkeypatch):
    # Unset WORKSPACE_ID makes the a2a_client PEP-562 resolver raise
    # RuntimeError when the emitter imports WORKSPACE_ID. The outer try
    # swallows it — no HTTP, no raise.
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    import molecule_runtime.a2a_client as a2a_client

    a2a_client._WORKSPACE_ID_cache = None
    rec = _Recorder()
    _install_async_client(monkeypatch, rec)

    await tool_trace.emit_tool_call(name="Bash")
    assert rec.requests == []


# --------------------------------------------------------------------------- #
# (d) falsy name no-ops (no HTTP at all)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_noop_on_empty_name(monkeypatch):
    rec = _Recorder()
    _install_async_client(monkeypatch, rec)

    await tool_trace.emit_tool_call(name="")
    assert rec.requests == []
