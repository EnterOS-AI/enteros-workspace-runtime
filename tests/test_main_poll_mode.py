"""Tests for poll-mode inbound delivery (E1).

Locks in:
  * ``A2AConfig.delivery_mode`` parse + default "push";
  * ``resolve_delivery_mode`` precedence (env override wins over config);
  * ``register_with_platform`` ALWAYS declares the resolved ``delivery_mode``
    (push AND poll — the sticky-poll fix; omission would let a stale poll row
    on the platform silently swallow all inbound A2A for a push runtime);
  * ``maybe_start_poll_delivery`` starts the SSOT poller + registers the consumer
    in poll mode, and is a NO-OP in push mode (never-run-both invariant);
  * the consumer posts to 127.0.0.1 (the LOCAL executor) — NOT the platform
    proxy — pinning the no-echo-loop contract.
"""
from __future__ import annotations

import asyncio
import http.server
import socket
import threading
from pathlib import Path

import pytest

import molecule_runtime.main as m
from molecule_runtime import config as config_mod


# ---------------------------------------------------------------------------
# config parse + resolution
# ---------------------------------------------------------------------------
def test_a2aconfig_delivery_mode_defaults_push():
    assert config_mod.A2AConfig().delivery_mode == "push"


def _disable_opt_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_mod,
        "Path",
        lambda p: Path(p)
        if str(p) != "/opt/molecule-platform-agent-template/config.yaml"
        else (tmp_path / "opt_missing" / "config.yaml"),
    )


def test_load_config_parses_delivery_mode(tmp_path, monkeypatch):
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "config.yaml").write_text(
        "name: WS\nmodel: anthropic/claude\nruntime: claude-code\n"
        "a2a:\n  delivery_mode: Poll\n"
    )
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))
    _disable_opt_fallback(monkeypatch, tmp_path)
    loaded = config_mod.load_config()
    # normalised to lower-case
    assert loaded.a2a.delivery_mode == "poll"


def test_resolve_delivery_mode_default_push():
    assert m.resolve_delivery_mode({}, None) == "push"
    assert m.resolve_delivery_mode({}, "push") == "push"


def test_resolve_delivery_mode_config_poll():
    assert m.resolve_delivery_mode({}, "poll") == "poll"


def test_resolve_delivery_mode_env_override_wins():
    # env wins over config (mirrors LOG_LEVEL), case-insensitive
    assert m.resolve_delivery_mode({"MOLECULE_DELIVERY_MODE": "Poll"}, "push") == "poll"
    assert m.resolve_delivery_mode({"MOLECULE_DELIVERY_MODE": "PUSH"}, "poll") == "push"


# ---------------------------------------------------------------------------
# register_with_platform — delivery_mode ALWAYS declared (sticky-poll fix)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status=200):
        self.status_code = status

    def json(self):
        return {}


class _CapturingClient:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, headers=None):
        self.bodies.append(json)
        return _FakeResp(200)


@pytest.mark.asyncio
async def test_register_body_declares_delivery_mode_in_push(monkeypatch):
    """Sticky-poll fix (prod agents-team 2026-07-05): push MUST be declared
    explicitly. An omitted delivery_mode resolves to the row's EXISTING value
    on the platform side, so a workspace whose row was ever poll could never
    heal back to push by re-registering — the tenant staged all inbound A2A
    for a poller the push runtime never starts (silently deaf agent)."""
    monkeypatch.setattr(m, "identity_gate_payload", lambda: {})
    client = _CapturingClient()
    ok = await m.register_with_platform(
        client,
        platform_url="http://p",
        workspace_id="ws",
        workspace_url="http://ws",
        agent_card={},
        headers={},
        delivery_mode="push",
    )
    assert ok is True
    assert client.bodies[0]["delivery_mode"] == "push"


@pytest.mark.asyncio
async def test_register_body_omits_delivery_mode_when_unrecognized(monkeypatch):
    """An unrecognized resolved mode is NOT forwarded — the platform 400s
    invalid values, which would turn a local misconfig into a boot-register
    failure. Omission falls back to the server-side resolution instead."""
    monkeypatch.setattr(m, "identity_gate_payload", lambda: {})
    client = _CapturingClient()
    ok = await m.register_with_platform(
        client,
        platform_url="http://p",
        workspace_id="ws",
        workspace_url="http://ws",
        agent_card={},
        headers={},
        delivery_mode="carrier-pigeon",
    )
    assert ok is True
    assert "delivery_mode" not in client.bodies[0]


@pytest.mark.asyncio
async def test_register_body_adds_delivery_mode_in_poll(monkeypatch):
    monkeypatch.setattr(m, "identity_gate_payload", lambda: {})
    client = _CapturingClient()
    ok = await m.register_with_platform(
        client,
        platform_url="http://p",
        workspace_id="ws",
        workspace_url="http://ws",
        agent_card={},
        headers={},
        delivery_mode="poll",
    )
    assert ok is True
    assert client.bodies[0]["delivery_mode"] == "poll"


# ---------------------------------------------------------------------------
# maybe_start_poll_delivery — gating + SSOT poller wiring
# ---------------------------------------------------------------------------
def test_maybe_start_poll_delivery_push_is_noop():
    calls = {"pollers": 0, "callback": 0}

    def _start(*a, **k):
        calls["pollers"] += 1

    def _setcb(cb):
        calls["callback"] += 1

    started = m.maybe_start_poll_delivery(
        "push", "http://p", "ws", 8000, object(),
        start_pollers=_start, set_callback=_setcb,
    )
    assert started is False
    assert calls == {"pollers": 0, "callback": 0}


def test_maybe_start_poll_delivery_poll_starts_pollers_and_consumer():
    captured = {}

    def _start(platform_url, workspace_ids):
        captured["start_args"] = (platform_url, workspace_ids)

    def _setcb(cb):
        captured["cb"] = cb

    started = m.maybe_start_poll_delivery(
        "poll", "http://plat", "ws-1", 9000, object(),
        start_pollers=_start, set_callback=_setcb,
    )
    assert started is True
    # SSOT helper called exactly with (platform_url, [workspace_id])
    assert captured["start_args"] == ("http://plat", ["ws-1"])
    # consumer callback registered
    assert callable(captured["cb"])


# ---------------------------------------------------------------------------
# consumer posts to 127.0.0.1 — the no-echo-loop contract
# ---------------------------------------------------------------------------
class _CapturingAsyncClient:
    last = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _CapturingAsyncClient.last = {"url": url, "json": json, "headers": headers}

        class _R:
            status_code = 200

        return _R()


@pytest.mark.asyncio
async def test_local_executor_post_targets_loopback(monkeypatch):
    monkeypatch.setattr(m.httpx, "AsyncClient", _CapturingAsyncClient)
    monkeypatch.setattr(m, "self_source_headers", lambda wsid: {"X-Workspace-ID": wsid})
    await m._post_polled_message_to_local_executor(8123, "ws-9", "hello peer")
    sent = _CapturingAsyncClient.last
    # MUST be loopback — a platform-proxy post would re-create an a2a_receive row
    # and infinite-loop the poller.
    assert sent["url"] == "http://127.0.0.1:8123/"
    assert sent["json"]["method"] == "message/send"
    assert sent["json"]["jsonrpc"] == "2.0"


def test_consumer_marshals_onto_loop(monkeypatch):
    scheduled = {}

    def _fake_rcts(coro, loop):
        scheduled["loop"] = loop
        coro.close()  # avoid "coroutine never awaited" warning

    monkeypatch.setattr(m.asyncio, "run_coroutine_threadsafe", _fake_rcts)
    sentinel_loop = object()
    consume = m.make_poll_inbox_consumer(8000, "ws", sentinel_loop)
    consume({"text": "ping", "peer_id": "peer-1"})
    assert scheduled["loop"] is sentinel_loop


def test_consumer_skips_empty_text(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        m.asyncio, "run_coroutine_threadsafe",
        lambda coro, loop: (called.__setitem__("n", called["n"] + 1), coro.close()),
    )
    consume = m.make_poll_inbox_consumer(8000, "ws", object())
    consume({"text": ""})
    consume({})
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# E1 startup-race fix — pre-bind window must not drop a staged message.
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_post_polled_message_retries_until_executor_binds(monkeypatch):
    # THE E1 regression guard: a message staged + posted DURING the pre-bind
    # window (uvicorn not yet listening) must be held and delivered once the
    # local executor comes up — NOT dropped on the first connection-refused.
    monkeypatch.setattr(m, "_LOCAL_POST_CONNECT_BACKOFF_SECONDS", 0.02)
    monkeypatch.setattr(m, "self_source_headers", lambda wsid: {"X-Workspace-ID": wsid})

    port = _free_port()
    received: dict[str, bytes] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — stdlib signature
            n = int(self.headers.get("Content-Length", "0"))
            received["body"] = self.rfile.read(n)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # silence the test server
            return

    # Fire the self-POST while NOTHING is listening on `port`.
    post_task = asyncio.create_task(
        m._post_polled_message_to_local_executor(port, "ws-pre", "staged-prebind-msg")
    )
    # Simulate the pre-bind window. The post MUST still be retrying (not dropped,
    # not yet resolved) — proving the staged message survives the window.
    await asyncio.sleep(0.1)
    assert not post_task.done()

    # Now bring the local executor up (uvicorn-bind analogue).
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        status = await asyncio.wait_for(post_task, timeout=5.0)
    finally:
        server.shutdown()
        t.join(timeout=2)

    assert status == 200
    # The staged text reached the now-listening executor — delivered, not lost.
    assert b"staged-prebind-msg" in received["body"]


# ---------------------------------------------------------------------------
# start_poll_delivery_when_bound — gate the poller on uvicorn bind.
# ---------------------------------------------------------------------------
class _FakeServer:
    def __init__(self, started: bool = False):
        self.started = started


@pytest.mark.asyncio
async def test_poll_delivery_when_bound_push_is_noop():
    started = await m.start_poll_delivery_when_bound(
        _FakeServer(started=True), "push", "http://p", "ws", 8000, object(),
    )
    assert started is False


@pytest.mark.asyncio
async def test_poll_delivery_does_not_start_before_bind():
    server = _FakeServer(started=False)
    calls = {"pollers": 0}

    task = asyncio.create_task(
        m.start_poll_delivery_when_bound(
            server, "poll", "http://p", "ws", 8000, object(),
            poll_interval=0.01,
            start_pollers=lambda *a, **k: calls.__setitem__("pollers", calls["pollers"] + 1),
            set_callback=lambda cb: None,
        )
    )
    # While the server reports unbound, the poller MUST NOT start.
    await asyncio.sleep(0.05)
    assert calls["pollers"] == 0
    assert not task.done()

    # Flip "bound" — the gate releases and the poller starts exactly once.
    server.started = True
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result is True
    assert calls["pollers"] == 1


@pytest.mark.asyncio
async def test_poll_delivery_starts_immediately_when_already_bound():
    server = _FakeServer(started=True)
    captured = {}
    result = await m.start_poll_delivery_when_bound(
        server, "poll", "http://plat", "ws-1", 9000, object(),
        poll_interval=0.01,
        start_pollers=lambda url, wsids: captured.__setitem__("args", (url, wsids)),
        set_callback=lambda cb: captured.__setitem__("cb", cb),
    )
    assert result is True
    assert captured["args"] == ("http://plat", ["ws-1"])
    assert callable(captured["cb"])


@pytest.mark.asyncio
async def test_poll_delivery_max_wait_proceeds_as_backstop(monkeypatch):
    # Defensive bound: if startup never reports bound, the poller still starts
    # (the consumer's connection-refused retry is the delivery backstop) rather
    # than the workspace never receiving inbound at all.
    server = _FakeServer(started=False)
    calls = {"pollers": 0}
    started = await m.start_poll_delivery_when_bound(
        server, "poll", "http://p", "ws", 8000, object(),
        poll_interval=0.01,
        max_wait_seconds=0.03,
        start_pollers=lambda *a, **k: calls.__setitem__("pollers", calls["pollers"] + 1),
        set_callback=lambda cb: None,
    )
    assert started is True
    assert calls["pollers"] == 1
