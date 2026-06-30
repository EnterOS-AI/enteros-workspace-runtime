"""Tests for poll-mode inbound delivery (E1).

Locks in:
  * ``A2AConfig.delivery_mode`` parse + default "push";
  * ``resolve_delivery_mode`` precedence (env override wins over config);
  * ``register_with_platform`` adds ``delivery_mode=poll`` ONLY in poll mode;
  * ``maybe_start_poll_delivery`` starts the SSOT poller + registers the consumer
    in poll mode, and is a NO-OP in push mode (never-run-both invariant);
  * the consumer posts to 127.0.0.1 (the LOCAL executor) — NOT the platform
    proxy — pinning the no-echo-loop contract.
"""
from __future__ import annotations

import asyncio
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
# register_with_platform — delivery_mode only added in poll
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
async def test_register_body_omits_delivery_mode_in_push(monkeypatch):
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
