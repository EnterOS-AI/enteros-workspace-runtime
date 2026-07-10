"""Enter-OS boot-step emitter (task #51 — runtime-emit half).

Pins the fire-and-forget / best-effort contract of
``molecule_runtime.boot_step_emit.emit_boot_step``:

  (a) On the concierge, a valid call POSTs the exact wire body
      (step/total/key/label/status[/message]) to
      ``{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/boot-event`` with the SSOT
      auth headers (Bearer token).
  (b) A 404 (core#3739 not merged yet) AND a transport/connection error are
      swallowed — the call never raises. This is why the PR is safe to merge
      before the core endpoint exists.
  (c) No-op (no HTTP at all) when the workspace id or platform url is absent,
      or when the box is not the platform concierge.
  (d) An over-length ``key`` (> 8 chars, the server cap) is truncated
      client-side so it can't 400 or break the keycap layout.

Sync, no event loop needed — the emitter is a synchronous best-effort POST.
The httpx client is intercepted by monkeypatching ``boot_step_emit.httpx.Client``
with a factory that builds a client over ``httpx.MockTransport`` (the same
idiom test_config_relay.py uses) and records every request.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import boot_step_emit  # noqa: E402
from molecule_runtime.platform_agent_identity import (  # noqa: E402
    PLATFORM_AGENT_IMAGE_ENV,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Baseline: a concierge box with a resolvable workspace id + platform url
    and a token on file, so the happy path fires. Individual tests override.

    The ``MOLECULE_PLATFORM_AGENT_IMAGE_BAKED`` marker makes
    ``on_platform_agent_image()`` True → ``_is_concierge()`` True without
    depending on the management-MCP settings probe.
    """
    monkeypatch.setenv("WORKSPACE_ID", "ws-boot-test-1")
    monkeypatch.setenv("PLATFORM_URL", "http://platform.test:8080")
    monkeypatch.setenv(PLATFORM_AGENT_IMAGE_ENV, "1")
    # Give auth_headers a token to attach (env fallback path — no /configs).
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "boot-token-xyz")
    # platform_auth caches the token in-process; clear so the env token is read.
    from molecule_runtime import platform_auth

    platform_auth.clear_cache()
    platform_auth._reset_workspace_id_cache()
    yield
    platform_auth.clear_cache()
    platform_auth._reset_workspace_id_cache()


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
        return self._response or httpx.Response(200, json={"status": "broadcast"})


def _install_client(monkeypatch, recorder: _Recorder):
    """Monkeypatch boot_step_emit.httpx.Client so the emitter's internally
    constructed client routes through our MockTransport recorder. Swallows the
    ``timeout=`` kwarg the emitter passes.

    Captures the REAL ``httpx.Client`` before patching — the factory builds a
    real client over a MockTransport; referencing the (patched) module attribute
    inside the factory would recurse.
    """
    _real_client = httpx.Client

    def _factory(*_args, **_kwargs):
        return _real_client(transport=httpx.MockTransport(recorder.handler))

    monkeypatch.setattr(boot_step_emit.httpx, "Client", _factory)


# --------------------------------------------------------------------------- #
# (a) correct URL / body / status / auth
# --------------------------------------------------------------------------- #
def test_emits_correct_url_body_and_auth(monkeypatch):
    rec = _Recorder(response=httpx.Response(200, json={"status": "broadcast"}))
    _install_client(monkeypatch, rec)

    boot_step_emit.emit_boot_step(
        "MCP", "Management MCP", "running", step=4, total=8,
        message="launching npx @molecule-ai/mcp-server",
    )

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST"
    assert (
        str(req.url)
        == "http://platform.test:8080/workspaces/ws-boot-test-1/boot-event"
    )
    import json

    body = json.loads(req.content)
    assert body == {
        "step": 4,
        "total": 8,
        "key": "MCP",
        "label": "Management MCP",
        "status": "running",
        "message": "launching npx @molecule-ai/mcp-server",
    }
    # SSOT auth header (Bearer token) attached — the wsAuth trust boundary.
    assert req.headers.get("authorization") == "Bearer boot-token-xyz"


def test_message_omitted_when_none(monkeypatch):
    rec = _Recorder()
    _install_client(monkeypatch, rec)

    boot_step_emit.emit_boot_step("PLG", "Install plugins", "ok", step=1, total=8)

    import json

    body = json.loads(rec.requests[0].content)
    assert "message" not in body
    assert body["status"] == "ok"


@pytest.mark.parametrize("status", ["running", "ok", "failed"])
def test_all_valid_statuses_pass_through(monkeypatch, status):
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step("RT", "Start runtime", status, step=3, total=8)
    import json

    assert json.loads(rec.requests[0].content)["status"] == status


def test_invalid_status_is_dropped_no_http(monkeypatch):
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    # A status outside the server's closed set is a runtime bug — drop it
    # rather than eat a guaranteed 400.
    boot_step_emit.emit_boot_step("RT", "Start runtime", "bogus", step=3, total=8)
    assert rec.requests == []


# --------------------------------------------------------------------------- #
# (b) 404 + connection error swallowed, never raises
# --------------------------------------------------------------------------- #
def test_swallows_404_before_core_endpoint_exists(monkeypatch):
    # core#3739 not merged yet → the route doesn't exist → 404. MUST NOT raise;
    # boot must proceed exactly as today.
    rec = _Recorder(response=httpx.Response(404, text="not found"))
    _install_client(monkeypatch, rec)

    # No exception escapes.
    boot_step_emit.emit_boot_step("NET", "Register", "running", step=7, total=8)
    assert len(rec.requests) == 1  # it did attempt the POST


def test_swallows_connection_error(monkeypatch):
    rec = _Recorder(raise_exc=httpx.ConnectError("connection refused"))
    _install_client(monkeypatch, rec)

    # A transport error (platform unreachable) must be swallowed, not raised.
    boot_step_emit.emit_boot_step("NET", "Register", "running", step=7, total=8)
    assert len(rec.requests) == 1


def test_swallows_arbitrary_client_construction_error(monkeypatch):
    # Even a failure constructing the client (unexpected) must never escape.
    def _boom(*_a, **_k):
        raise RuntimeError("client blew up")

    monkeypatch.setattr(boot_step_emit.httpx, "Client", _boom)
    boot_step_emit.emit_boot_step("ID", "Load identity", "ok", step=2, total=8)
    # No assertion beyond "did not raise".


# --------------------------------------------------------------------------- #
# (c) no-ops when workspace id / platform url absent, or non-concierge
# --------------------------------------------------------------------------- #
def test_noop_when_workspace_id_absent(monkeypatch):
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    from molecule_runtime import platform_auth

    platform_auth._reset_workspace_id_cache()
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step("PLG", "Install plugins", "ok", step=1, total=8)
    assert rec.requests == []


def test_noop_when_platform_url_blank_and_not_in_container(monkeypatch, tmp_path):
    # Blank PLATFORM_URL + not in a container → _resolve_platform_url falls back
    # to localhost:8080, which is a valid URL, so it WOULD emit. To prove the
    # "platform url absent" no-op we force the resolver to return "".
    monkeypatch.setattr(boot_step_emit, "_resolve_platform_url", lambda: "")
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step("PLG", "Install plugins", "ok", step=1, total=8)
    assert rec.requests == []


def test_noop_when_not_concierge(monkeypatch):
    # Ordinary tenant: neither the baked marker nor a wired management MCP.
    monkeypatch.delenv(PLATFORM_AGENT_IMAGE_ENV, raising=False)
    monkeypatch.setattr(boot_step_emit, "_is_concierge", lambda: False)
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step("PLG", "Install plugins", "ok", step=1, total=8)
    assert rec.requests == []


def test_concierge_via_mcp_present_still_emits(monkeypatch):
    # De-baked concierge: image marker OFF, but the management MCP probe reports
    # present → _is_concierge() True (mirrors _is_platform_agent).
    monkeypatch.delenv(PLATFORM_AGENT_IMAGE_ENV, raising=False)
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.mcp_server_present",
        lambda: True,
    )
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step("PLG", "Install plugins", "ok", step=1, total=8)
    assert len(rec.requests) == 1


# --------------------------------------------------------------------------- #
# (d) key length cap enforced client-side (server cap = 8)
# --------------------------------------------------------------------------- #
def test_key_truncated_to_8_chars(monkeypatch):
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step(
        "TOOLINGXYZ", "Enumerate tools", "ok", step=6, total=8
    )
    import json

    body = json.loads(rec.requests[0].content)
    assert body["key"] == "TOOLINGX"  # first 8 chars
    assert len(body["key"]) <= 8


def test_boundary_key_of_exactly_8_is_unchanged(monkeypatch):
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    boot_step_emit.emit_boot_step("ONLINEXX", "Go online", "ok", step=8, total=8)
    import json

    assert json.loads(rec.requests[0].content)["key"] == "ONLINEXX"
