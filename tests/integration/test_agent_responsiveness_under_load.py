"""Integration e2e — agent responsiveness under load (over the real wire).

This is the regression gate for the 2026-06-10 "busy agent becomes
unreachable / blocks ~300s" class of incident. It drives a **real**
``RuntimeA2AExecutor`` mounted in a **real** a2a-sdk JSON-RPC Starlette app
served by a **real** uvicorn socket, and talks to it over **real HTTP**
(httpx) — no executor mocks, no in-process TestClient short-circuit. The
only fakes are (a) the LLM agent, replaced by a *deterministic* fake whose
``astream_events`` runs a real ``bash sleep`` so "busy" is a known wall-clock
window with **zero LLM variability**, and (b) a tiny in-process platform
``/registry/heartbeat`` sink so we can observe the workspace's liveness the
same way the real platform healthsweep does.

Behaviours locked in (the dispatch contract):

* **(a) non-blocking fast-ack** — while message A holds the agent busy for a
  known window that EXCEEDS the old 45 s stale threshold, a concurrent
  message B on the SAME ``context_id`` is fast-acked well under the SLA
  (default 5 s CI-slack ceiling over the <100 ms wire contract) instead of
  head-of-line-blocking behind A. Locks in the non-blocking flip (#116) and
  PR #112's queue-don't-break path.

* **(b) not falsely stale** — a real heartbeat thread keeps beating during
  A's whole busy window; the platform liveness probe never flips to
  ``stale`` across A's ~90 s turn even though that window is >2× the old
  45 s stale ceiling. Locks in the stale-window fix.

* **recovery** — both A and B ultimately complete and the workspace returns
  to ``active_tasks == 0`` (idle). Proves the busy turn drains its queue and
  releases.

* **A1 tool-timeout** — a turn whose tool hangs past the per-turn idle cap
  (``A2A_COMPLETION_IDLE_TIMEOUT_SECONDS``) is bounded: the turn returns a
  ``tool_timeout``-class failure instead of wedging the single-threaded
  executor forever. Locks in the A1 idle-cap.

Determinism / flake mitigation:

* No assertion depends on LLM output — the fake agent is fully scripted.
* "Busy" is a real ``bash sleep`` of a known length, not an LLM task.
* Every status assertion uses **poll-with-deadline**, never a fixed sleep,
  so a slow CI box widens latency but never flips a verdict.
* All timings are env-tunable (``RESP_E2E_*``) so CI slack is adjustable
  without touching code.
* The whole module **loud-skips** (never silent-passes) when the real
  a2a-sdk wheel is absent — see ``conftest.py`` in this directory.
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


pytestmark = pytest.mark.asyncio


# ─── env-tunable timings (CI slack adjustable without code edits) ─────────

# How long message A keeps the agent busy. Default 90 s mirrors the dispatch
# brief and is deliberately >2× the old 45 s stale window. CI can shrink it
# (still must exceed STALE_WINDOW to be a real test).
A_BUSY_SECONDS = float(os.environ.get("RESP_E2E_A_BUSY_SECONDS", "90"))

# The platform liveness stale window we assert the agent never trips. Set
# generously above the heartbeat interval. The point is that across A's full
# busy window the agent keeps beating, so it never goes stale even though the
# window is comfortably larger than the historical 45 s bug threshold.
STALE_WINDOW_SECONDS = float(os.environ.get("RESP_E2E_STALE_WINDOW_SECONDS", "45"))

# Heartbeat cadence used by the real heartbeat thread under test. Tight so we
# get several beats inside A's window. Must be < STALE_WINDOW with margin.
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("RESP_E2E_HEARTBEAT_INTERVAL", "2"))

# Fast-ack SLA ceiling for message B. The wire contract is <100 ms; we assert
# a generous CI-slack ceiling so a loaded runner never false-fails while still
# being ~18× tighter than A's busy window (so blocking would be unmissable).
FAST_ACK_CEILING_SECONDS = float(os.environ.get("RESP_E2E_FAST_ACK_CEILING", "5"))

# Per-turn idle cap for the A1 tool-timeout case. Small so the hung-tool test
# is fast. The executor reads A2A_COMPLETION_IDLE_TIMEOUT_SECONDS.
IDLE_CAP_SECONDS = float(os.environ.get("RESP_E2E_IDLE_CAP", "3"))

# Generic deadline for poll-with-deadline status assertions.
POLL_DEADLINE_SECONDS = float(os.environ.get("RESP_E2E_POLL_DEADLINE", "20"))
POLL_INTERVAL_SECONDS = 0.1


# ─── helpers ──────────────────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _poll_until(predicate, *, deadline: float, interval: float = POLL_INTERVAL_SECONDS):
    """Poll ``predicate`` (sync callable -> bool) until true or deadline.

    Returns True if the predicate became true, False if the deadline passed.
    Poll-with-deadline, NOT fixed sleep, so a slow box widens the window
    rather than flipping the verdict.
    """
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class _UvicornInThread:
    """Run a uvicorn server for a Starlette app on a background thread.

    Yields once the socket is actually accepting connections (polled), so
    callers never race the bind. Clean shutdown on ``stop()``.
    """

    def __init__(self, app, port: int):
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._port = port

    def start(self, wait_deadline: float = 10.0) -> None:
        self._thread.start()
        end = time.monotonic() + wait_deadline
        while time.monotonic() < end:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"uvicorn did not bind 127.0.0.1:{self._port} in time")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


# ─── fake platform registry (the liveness sink) ──────────────────────────


class _FakeRegistry:
    """In-process stand-in for the platform's /registry/heartbeat sink +
    healthsweep staleness, with the SAME semantics the real platform uses:
    a workspace is ``online`` while its last heartbeat is within the stale
    window, else ``stale``.

    ``set_current_task`` (the executor's busy/idle push) AND the runtime's
    periodic heartbeat thread both POST here, so this observes liveness over
    the real wire exactly like production.

    Busy-state is read ONLY from the executor's ``set_current_task`` pushes,
    distinguished from the periodic heartbeat-thread beats by the presence of
    the ``current_task`` field (the thread's body never carries it and always
    hardcodes ``active_tasks: 0``). Liveness/staleness counts EVERY beat. This
    separation is essential: otherwise a thread beat (active_tasks:0) lands
    mid-turn and would falsely read the busy agent as idle.
    """

    def __init__(self, stale_window: float):
        self.stale_window = stale_window
        self._lock = threading.Lock()
        self.last_heartbeat_monotonic: float | None = None
        # Busy state from set_current_task pushes only (see class docstring).
        self.last_active_tasks: int = 0
        self.task_push_count: int = 0
        # Every beat (thread + task pushes) for liveness.
        self.heartbeat_count: int = 0

    async def _heartbeat(self, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        with self._lock:
            self.last_heartbeat_monotonic = time.monotonic()
            self.heartbeat_count += 1
            # Only set_current_task pushes carry current_task — use those for
            # the authoritative busy/idle count; ignore the thread's hardcoded
            # active_tasks:0 so a mid-turn thread beat can't read as idle.
            if "current_task" in body:
                self.task_push_count += 1
                at = body.get("active_tasks")
                if isinstance(at, int):
                    self.last_active_tasks = at
        # Mirror the real handler: 200 + (optionally) inbound secret. We
        # return an empty JSON object — persist_inbound_secret tolerates it.
        return JSONResponse({"ok": True})

    async def _register(self, request: Request) -> JSONResponse:
        with self._lock:
            self.last_heartbeat_monotonic = time.monotonic()
            self.heartbeat_count += 1
        return JSONResponse({"ok": True})

    def status(self) -> str:
        """online if a heartbeat landed within the stale window, else stale."""
        with self._lock:
            if self.last_heartbeat_monotonic is None:
                return "never"
            age = time.monotonic() - self.last_heartbeat_monotonic
            return "online" if age <= self.stale_window else "stale"

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/registry/heartbeat", self._heartbeat, methods=["POST"]),
                Route("/registry/register", self._register, methods=["POST"]),
            ]
        )


# ─── deterministic fake agent (zero LLM variability) ─────────────────────


class _ScriptedAgent:
    """A fake compiled runtime graph exposing ``astream_events`` — the exact
    surface ``RuntimeA2AExecutor`` drives. Behaviour branches on the human
    message text so a single agent serves all cases deterministically.

    * text contains "SLEEP:<n>"  → run a REAL ``bash -c 'sleep n'`` and
      register it on the active inbox entry (so it's a genuine busy window,
      and so an A2A cancel/Stop could SIGTERM it). Yields a couple of model
      events around the sleep, then a clean ``on_chat_model_end``.

    * text contains "PING"       → yield a single tiny chunk and end fast.

    * text contains "HANG:<n>"   → ``await asyncio.sleep(n)`` BETWEEN events
      with no event emitted, so the executor's per-event idle cap
      (A2A_COMPLETION_IDLE_TIMEOUT_SECONDS) trips and the turn times out.

    Single-flight contention (the real blocking mechanism)
    ------------------------------------------------------
    A production runtime agent graph is single-flight per ``context_id``
    (the runtime's graph/session state is keyed by ``thread_id == context_id``);
    two turns for the same context cannot drive the graph concurrently. We model this
    deterministically with a per-context ``asyncio.Lock`` acquired for the
    WHOLE turn: while A holds it (busy 90 s), any *other* turn that actually
    enters the agent loop for the same context (i.e. the legacy BLOCKING
    path, ``MOLECULE_A2A_NONBLOCKING=false``) head-of-line-blocks on the lock
    — reproducing the ~300 s "second POST queues behind the live turn" wedge.

    The non-blocking fast-path (#116, default-ON) fast-acks + defers the
    second message in ``runtime_inbox`` *before* ``_core_execute`` ever calls
    ``astream_events``, so B never touches the lock and returns in ms. That
    is exactly the contract this gate locks in: with the flag ON the negative
    control (a busy lock) is invisible to B; with it OFF, B blocks. The CI
    job runs ON (production default); the in-repo negative-control assertion
    proves the gate would catch a regression to the blocking path.

    No LLM, no network, no randomness — the only nondeterminism is wall
    clock, which only the poll-with-deadline assertions touch.
    """

    def __init__(self):
        # Per-context single-flight locks (thread_id == context_id).
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, context_id: str) -> asyncio.Lock:
        return self._locks.setdefault(context_id, asyncio.Lock())

    @staticmethod
    def _context_of(config) -> str:
        if isinstance(config, dict):
            return (config.get("configurable") or {}).get("thread_id", "") or ""
        return ""

    @staticmethod
    def _text_of(payload) -> str:
        msgs = payload.get("messages", [])
        for role, content in reversed(msgs):
            if role == "human":
                if isinstance(content, str):
                    return content
                # multimodal content list -> first text block
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")
        return ""

    async def astream_events(self, payload, *, config=None, version=None):
        text = self._text_of(payload)
        context_id = self._context_of(config)
        lock = self._lock_for(context_id)

        if "PING" in text:
            # Single-flight: a PING that reaches the agent loop while another
            # turn holds the same-context lock BLOCKS here. The non-blocking
            # fast-path means this code is never reached for a mid-turn PING.
            async with lock:
                yield {
                    "event": "on_chat_model_stream",
                    "run_id": "ping-run",
                    "data": {"chunk": _Chunk("pong")},
                }
                yield {
                    "event": "on_chat_model_end",
                    "run_id": "ping-run",
                    "data": {"output": None},
                }
            return

        if "HANG:" in text:
            secs = float(text.split("HANG:", 1)[1].split()[0])
            # Emit one event so the turn is genuinely "working", then stall
            # with NO event for longer than the idle cap. The executor must
            # bound this with asyncio.wait_for and raise rather than wedge.
            yield {"event": "on_chat_model_start", "run_id": "hang-run", "data": {}}
            await asyncio.sleep(secs)
            yield {"event": "on_chat_model_end", "run_id": "hang-run", "data": {"output": None}}
            return

        if "SLEEP:" in text:
            secs = text.split("SLEEP:", 1)[1].split()[0]
            # Hold the per-context single-flight lock for the WHOLE busy
            # window — this is what a concurrent same-context turn would
            # contend on under the legacy blocking path.
            async with lock:
                yield {"event": "on_chat_model_start", "run_id": "busy-run", "data": {}}
                # Real subprocess so "busy" is a true OS-level sleep and
                # registers on the inbox entry, mirroring a real bash tool.
                from molecule_runtime.runtime_inbox import register_active_subprocess

                proc = await asyncio.create_subprocess_exec(
                    "bash", "-c", f"sleep {secs}",
                )
                register_active_subprocess(proc)
                await proc.wait()
                yield {
                    "event": "on_chat_model_stream",
                    "run_id": "busy-run",
                    "data": {"chunk": _Chunk("done")},
                }
                yield {
                    "event": "on_chat_model_end",
                    "run_id": "busy-run",
                    "data": {"output": None},
                }
            return

        # default: trivial completion
        yield {
            "event": "on_chat_model_stream",
            "run_id": "default-run",
            "data": {"chunk": _Chunk("ok")},
        }
        yield {"event": "on_chat_model_end", "run_id": "default-run", "data": {"output": None}}


class _Chunk:
    """Minimal AIMessageChunk-shaped object: the executor reads .content."""

    def __init__(self, content: str):
        self.content = content


class _Heartbeat:
    """Minimal stand-in for HeartbeatLoop — set_current_task only touches
    ``.active_tasks`` and ``.current_task``."""

    def __init__(self):
        self.active_tasks = 0
        self.current_task = ""


# ─── runtime server under test ────────────────────────────────────────────


def _build_runtime_app(agent, heartbeat):
    """Mount the REAL a2a-sdk JSON-RPC routes around the REAL executor.

    Mirrors molecule_runtime.main.build_routes (DefaultRequestHandler +
    create_jsonrpc_routes + agent-card route) — the production wire — but
    with the scripted agent injected.
    """
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
    )
    from molecule_runtime.a2a_executor import RuntimeA2AExecutor
    from molecule_runtime.boot_routes import build_routes

    card = AgentCard(
        name="responsiveness-e2e-agent",
        description="deterministic scripted agent for the responsiveness gate",
        version="0.0.0",
        supported_interfaces=[
            AgentInterface(protocol_binding="https://a2a.g/v1", url="http://127.0.0.1")
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
    )
    executor = RuntimeA2AExecutor(agent, heartbeat=heartbeat, model="scripted:test")
    return Starlette(routes=build_routes(card, executor, adapter_error=None))


async def _send_message(base_url: str, text: str, context_id: str, *, timeout: float):
    """POST a real JSON-RPC message/send over httpx. Returns (elapsed_s, resp)."""
    from molecule_runtime.a2a_client import build_message_send_params

    params = build_message_send_params(text, role="user")
    params["message"]["contextId"] = context_id
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": params,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        start = time.monotonic()
        resp = await client.post(base_url + "/", json=body)
        elapsed = time.monotonic() - start
    return elapsed, resp


@pytest.fixture
def _nonblocking_env(monkeypatch):
    """Mirror the production default (non-blocking ON since #116) explicitly,
    and point set_current_task's busy/idle push at the fake registry."""
    monkeypatch.setenv("MOLECULE_A2A_NONBLOCKING", "true")
    # Reset the process-wide inbox singleton so in-flight flags from a prior
    # test never leak into this one.
    from molecule_runtime.runtime_inbox import get_inbox

    get_inbox().reset_for_tests()
    yield


# ─── Test 1+2+recovery — busy agent stays reachable, fast-acks, recovers ──


async def test_busy_agent_fast_acks_stays_online_and_recovers(_nonblocking_env, monkeypatch):
    """End-to-end over the wire:

    1. Send A (SLEEP:<A_BUSY_SECONDS>) — agent genuinely busy for a window
       that exceeds the old 45 s stale threshold.
    2. While A is in flight, send B (PING) on the SAME context_id and measure
       ack latency over the real socket.
    3. Assert B fast-acks (<= FAST_ACK_CEILING), the registry stays ``online``
       across A's whole busy window, and after A completes the workspace
       returns to idle (active_tasks == 0) — recovery.
    """
    registry = _FakeRegistry(stale_window=STALE_WINDOW_SECONDS)
    reg_port = _free_port()
    reg_server = _UvicornInThread(registry.app(), reg_port)
    reg_server.start()
    reg_url = f"http://127.0.0.1:{reg_port}"

    # set_current_task reads WORKSPACE_ID + PLATFORM_URL to push busy/idle.
    monkeypatch.setenv("WORKSPACE_ID", "resp-e2e-ws")
    monkeypatch.setenv("PLATFORM_URL", reg_url)

    heartbeat = _Heartbeat()
    rt_port = _free_port()
    rt_app = _build_runtime_app(_ScriptedAgent(), heartbeat)
    rt_server = _UvicornInThread(rt_app, rt_port)
    rt_server.start()
    rt_url = f"http://127.0.0.1:{rt_port}"

    # Start the REAL heartbeat thread so beats keep landing during A's turn.
    from molecule_runtime import mcp_heartbeat

    hb_thread = threading.Thread(
        target=mcp_heartbeat.heartbeat_loop,
        args=(reg_url, "resp-e2e-ws", "test-token"),
        kwargs={"interval": HEARTBEAT_INTERVAL_SECONDS},
        daemon=True,
    )
    hb_thread.start()

    context_id = "ctx-busy-recover"
    try:
        # ── 1. fire A (long) without awaiting completion ──────────────────
        a_task = asyncio.create_task(
            _send_message(
                rt_url,
                f"SLEEP:{A_BUSY_SECONDS}",
                context_id,
                timeout=A_BUSY_SECONDS + 60,
            )
        )

        # Wait until the agent is genuinely busy: the busy push sets
        # active_tasks >= 1 on the registry. Poll-with-deadline, not sleep.
        became_busy = await _poll_until(
            lambda: registry.last_active_tasks >= 1,
            deadline=POLL_DEADLINE_SECONDS,
        )
        assert became_busy, (
            "agent never reported busy (active_tasks>=1) — the long turn did "
            "not start over the wire"
        )

        # ── 2. fire B (ping) on the SAME context_id; measure ack latency ──
        # Give B an httpx read timeout pinned to the fast-ack SLA: if B
        # head-of-line-blocks behind A (the regression), httpx times out at
        # the SLA and we convert it into a precise fast-ack assertion rather
        # than letting a raw ReadTimeout escape.
        try:
            b_elapsed, b_resp = await _send_message(
                rt_url, "PING", context_id, timeout=FAST_ACK_CEILING_SECONDS
            )
        except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
            pytest.fail(
                f"B did NOT fast-ack within {FAST_ACK_CEILING_SECONDS}s — it "
                f"head-of-line-blocked behind the busy turn (httpx {type(exc).__name__}). "
                f"This is exactly the 'busy agent blocks ~300s on a concurrent "
                f"POST' regression (non-blocking flip #116). A is busy for "
                f"~{A_BUSY_SECONDS}s, so the block is unmissable."
            )

        # ── 3a. B fast-acked, NOT blocked behind A ────────────────────────
        assert b_resp.status_code == 200, f"B HTTP {b_resp.status_code}: {b_resp.text[:300]}"
        assert b_elapsed <= FAST_ACK_CEILING_SECONDS, (
            f"B fast-ack took {b_elapsed:.2f}s — SLA ceiling {FAST_ACK_CEILING_SECONDS}s. "
            f"This is the head-of-line-blocking regression (busy agent blocks "
            f"~300s on a concurrent POST). A is still busy for ~{A_BUSY_SECONDS}s, "
            f"so blocking would be unmissable."
        )
        # The fast-ack is a terminal JSON-RPC result (the queued-ack message),
        # not an error envelope.
        b_json = b_resp.json()
        assert "result" in b_json, f"B was not a terminal result: {b_json}"

        # ── 3b. registry stays online across A's remaining busy window ────
        # A started at most POLL_DEADLINE+epsilon ago; assert online for the
        # rest of A_BUSY_SECONDS. We sample repeatedly; any single 'stale'
        # sample fails (the 45 s-window regression would trip here because A
        # is busy > 45 s but the heartbeat thread must keep it online).
        observe_until = time.monotonic() + min(A_BUSY_SECONDS, 30) * 0.9
        saw_stale = False
        while time.monotonic() < observe_until and not a_task.done():
            if registry.status() == "stale":
                saw_stale = True
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS / 2)
        assert not saw_stale, (
            "registry flipped to 'stale' while the agent was mid-turn — the "
            "busy turn starved the heartbeat (stale-window regression). A busy "
            "turn must NOT mark the agent unreachable."
        )
        assert registry.heartbeat_count >= 2, (
            f"expected multiple heartbeats during the busy window, saw "
            f"{registry.heartbeat_count} — heartbeat thread not beating"
        )

        # ── 3c. A ultimately completes; agent recovers to idle ────────────
        a_elapsed, a_resp = await a_task
        assert a_resp.status_code == 200, f"A HTTP {a_resp.status_code}: {a_resp.text[:300]}"
        assert a_elapsed >= A_BUSY_SECONDS * 0.5, (
            f"A returned in {a_elapsed:.1f}s but was supposed to sleep "
            f"{A_BUSY_SECONDS}s — the busy window was not real"
        )

        # Recovery: set_current_task decrements active_tasks to 0 at turn end
        # and pushes; poll the registry until it observes idle.
        recovered = await _poll_until(
            lambda: registry.last_active_tasks == 0,
            deadline=POLL_DEADLINE_SECONDS,
        )
        assert recovered, (
            f"workspace did not recover to idle (active_tasks==0); last seen "
            f"{registry.last_active_tasks} — turn did not release"
        )
        assert heartbeat.active_tasks == 0, (
            f"executor left active_tasks={heartbeat.active_tasks} after the "
            f"turn — busy state leaked"
        )
    finally:
        rt_server.stop()
        reg_server.stop()


# ─── A1 — hung tool is bounded; turn times out instead of wedging ─────────


async def test_hung_tool_is_bounded_and_turn_times_out(_nonblocking_env, monkeypatch):
    """A turn whose tool stalls past the per-turn idle cap must be bounded:
    the executor's ``asyncio.wait_for(..., idle_cap)`` raises and the turn
    returns a failure envelope rather than wedging the single-threaded
    executor. Locks in A1 (tool-timeout)."""
    monkeypatch.setenv("A2A_COMPLETION_IDLE_TIMEOUT_SECONDS", str(IDLE_CAP_SECONDS))

    heartbeat = _Heartbeat()
    rt_port = _free_port()
    rt_app = _build_runtime_app(_ScriptedAgent(), heartbeat)
    rt_server = _UvicornInThread(rt_app, rt_port)
    rt_server.start()
    rt_url = f"http://127.0.0.1:{rt_port}"

    try:
        # Tool hangs for ~3× the idle cap; the turn MUST return (bounded) in
        # roughly idle_cap, not hang for the full hang window.
        hang_for = IDLE_CAP_SECONDS * 3
        start = time.monotonic()
        elapsed, resp = await _send_message(
            rt_url,
            f"HANG:{hang_for}",
            "ctx-hang",
            timeout=hang_for + 30,
        )
        wall = time.monotonic() - start

        # The turn was BOUNDED by the idle cap, not the full hang window.
        assert wall < hang_for, (
            f"turn took {wall:.1f}s for a {hang_for:.1f}s hang — the idle cap "
            f"({IDLE_CAP_SECONDS}s) did not bound it; the executor wedged"
        )
        # The runtime still served the request to completion (no socket hang)
        # and returned a terminal envelope — the executor recovered and is
        # ready for the next request.
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:300]}"
        # active_tasks released even though the turn failed — the finally
        # block decrements, so a timed-out turn doesn't leak busy state.
        recovered = heartbeat.active_tasks == 0
        assert recovered, (
            f"active_tasks={heartbeat.active_tasks} after a timed-out turn — "
            f"the idle-cap path must still release busy state"
        )

        # Follow-up request is served promptly — proves the executor did NOT
        # wedge: a wedged single-threaded executor would block this too.
        elapsed2, resp2 = await _send_message(
            rt_url, "PING", "ctx-hang-followup", timeout=15
        )
        assert resp2.status_code == 200, f"follow-up HTTP {resp2.status_code}"
        assert elapsed2 < IDLE_CAP_SECONDS + 5, (
            f"follow-up after a hung tool took {elapsed2:.1f}s — the executor "
            f"is still wedged from the hung turn"
        )
    finally:
        rt_server.stop()
