"""RFC molecule-core#4402 B2-runtime — the heartbeat emits ``is_busy``.

Core's B2 heartbeat UPDATE derives busy state as
``is_busy = COALESCE($runtime_is_busy, active_tasks > 0)`` — it honours a
runtime-sent ``is_busy`` when present, else falls back to ``active_tasks > 0``.
That honour-branch is DEAD until the runtime actually sends the field. These
tests pin that the runtime now does, sourced from the live ``turn_in_flight``
state, and keeps dual-writing ``active_tasks`` for the migration window (§5).

The proof drives the REAL heartbeat body builder (``HeartbeatLoop._send_heartbeat``)
with a capturing httpx-shaped client — so it validates the ACTUAL bytes put on
the wire, not a mock that could silently omit the field — and drives the REAL
A2A executor turn so the ``turn_in_flight`` transition that feeds ``is_busy`` is
the one production code sets/clears, not a hand-flipped flag.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── httpx.Client-shaped capturing stub (mirrors the conformance gate) ────


class _FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return {"status": "ok"}


class _CapturingSyncClient:
    """Records the json= body of each POST — the real wire payload."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": kwargs.get("json")})
        return _FakeResponse()


def _build_context(text: str, context_id: str, *, task_id: str = "task-1"):
    """RequestContext-shaped stub (matches tests/test_a2a_nonblocking_inbox)."""
    part = SimpleNamespace(text=text, root=None)
    msg = SimpleNamespace(parts=[part], metadata=None)
    return SimpleNamespace(
        message=msg,
        task_id=task_id,
        context_id=context_id,
        current_task=SimpleNamespace(),  # non-None → skips v1 Task pre-enqueue
    )


@pytest.fixture(autouse=True)
def _fresh_inbox(monkeypatch):
    monkeypatch.setenv("MOLECULE_A2A_NONBLOCKING", "true")
    from molecule_runtime.runtime_inbox import get_inbox

    get_inbox().reset_for_tests()
    yield
    get_inbox().reset_for_tests()


def _capture_heartbeat_body() -> dict[str, Any]:
    """Send one real heartbeat through the real builder and return the body."""
    from molecule_runtime.heartbeat import HeartbeatLoop

    loop = HeartbeatLoop(platform_url="http://platform.test", workspace_id="ws-busy")
    client = _CapturingSyncClient()
    loop._send_heartbeat(client)
    assert client.calls and "/registry/heartbeat" in client.calls[-1]["url"]
    return client.calls[-1]["json"]


def test_heartbeat_is_busy_false_when_idle() -> None:
    """No turn in flight → the heartbeat body carries is_busy=False, and still
    dual-writes active_tasks (RFC §5 migration window)."""
    body = _capture_heartbeat_body()
    assert body["is_busy"] is False
    assert "active_tasks" in body  # dual-write preserved


@pytest.mark.asyncio
async def test_heartbeat_is_busy_tracks_real_executor_turn(monkeypatch) -> None:
    """is_busy==True DURING a real in-flight executor turn, ==False after it
    completes — sourced from the executor-managed turn_in_flight state, read
    through the same runtime_inbox accessor the heartbeat sender uses.
    """
    from molecule_runtime.a2a_executor import RuntimeA2AExecutor

    context_id = "ctx-isbusy"
    mid_turn: dict[str, Any] = {}

    async def _agent_astream(payload, **_kwargs):
        # A real turn is in flight here: the executor set turn_in_flight=True
        # before entering astream. Capture the REAL heartbeat body at this seam.
        mid_turn["body"] = _capture_heartbeat_body()
        yield {"event": "on_chat_model_end", "run_id": "r1", "data": {"output": None}}

    agent = MagicMock()
    agent.astream_events = _agent_astream
    executor = RuntimeA2AExecutor(agent, heartbeat=None, model="test")

    # Isolate from the network side effects of set_current_task's push.
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.set_current_task", AsyncMock()
    )
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.TaskUpdater",
        lambda *a, **kw: SimpleNamespace(
            start_work=AsyncMock(),
            add_artifact=AsyncMock(),
            complete=AsyncMock(),
            failed=AsyncMock(),
        ),
    )

    ctx = _build_context("hello", context_id)
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()

    await executor._core_execute(ctx, event_queue)

    # DURING the turn: busy True, active_tasks still emitted (dual-write).
    assert mid_turn["body"]["is_busy"] is True, (
        "heartbeat did not report is_busy=True while a turn was in flight — "
        "core's COALESCE honour-branch stays dead"
    )
    assert "active_tasks" in mid_turn["body"]

    # AFTER completion: the executor's finally cleared turn_in_flight, so a
    # fresh heartbeat reports not-busy.
    from molecule_runtime.runtime_inbox import get_inbox

    assert get_inbox().peek(context_id).turn_in_flight is False
    assert _capture_heartbeat_body()["is_busy"] is False


@pytest.mark.asyncio
async def test_is_busy_sourced_from_turn_in_flight_flag() -> None:
    """Direct wiring pin: the heartbeat's is_busy reflects the inbox's
    turn_in_flight flag — the single busy source of truth — with no other
    turn-tracking. Flip the flag the executor owns and watch is_busy follow.
    """
    from molecule_runtime.runtime_inbox import get_inbox

    assert _capture_heartbeat_body()["is_busy"] is False

    entry = await get_inbox().get_or_create("ctx-direct")
    entry.turn_in_flight = True  # exactly what a2a_executor sets at turn start
    assert _capture_heartbeat_body()["is_busy"] is True

    entry.turn_in_flight = False  # exactly what its finally clears
    assert _capture_heartbeat_body()["is_busy"] is False
