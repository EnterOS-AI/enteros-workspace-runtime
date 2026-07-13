"""MUST-FIX 1 end-to-end: the turn lease's ``expired()`` is CONSUMED by the
executor to END a stalled turn, and it COMPLEMENTS (does not fight) the native
``A2A_COMPLETION_IDLE_TIMEOUT`` idle-cap.

These drive the real ``RuntimeA2AExecutor._core_execute`` astream loop with a
hanging mock agent (no runtime event ever) so the per-event idle-cap fires, and
assert:

* kernel ON + no subprocess tool activity  -> lease expires -> turn ENDS (the
  stall is surfaced as a terminal ``updater.failed``);
* kernel OFF (no lease installed)           -> same terminal failure on the
  idle-cap alone — byte-identical to the pre-kernel behavior.

The "live subprocess keeps the turn going" half is pinned at unit granularity in
``test_turn_lease.py::test_turn_is_alive_despite_idle_live_subprocess`` (driving
a >=idle-cap hang to completion here would just re-test asyncio timing).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime import kernel  # noqa: E402
from molecule_runtime import turn_lease as tl  # noqa: E402


class _FakeUpdater:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    async def start_work(self):
        self.events.append(("start_work", None))

    async def add_artifact(self, **kwargs):
        self.events.append(("add_artifact", kwargs))

    async def complete(self, message=None):
        self.events.append(("complete", message))

    async def failed(self, message=None):
        self.events.append(("failed", message))


def _build_context(text: str, context_id: str):
    part = SimpleNamespace(text=text, root=None)
    msg = SimpleNamespace(parts=[part], metadata=None)
    return SimpleNamespace(
        message=msg,
        task_id="task-1",
        context_id=context_id,
        current_task=SimpleNamespace(),
    )


@pytest.fixture(autouse=True)
def _fast_idle_cap(monkeypatch, tmp_path):
    # Tiny idle-cap so the stall is declared in a fraction of a second.
    monkeypatch.setenv("A2A_COMPLETION_IDLE_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("MOLECULE_A2A_NONBLOCKING", "true")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / ".molecule"))
    from molecule_runtime.runtime_inbox import get_inbox

    get_inbox().reset_for_tests()
    tl.install(None)
    yield
    tl.install(None)


def _hang_executor(monkeypatch):
    from molecule_runtime.a2a_executor import RuntimeA2AExecutor

    async def _hang_astream(*_a, **_k):
        import asyncio

        await asyncio.sleep(60)  # never yields — forces the idle-cap
        yield  # pragma: no cover

    agent = MagicMock()
    agent.astream_events = _hang_astream
    executor = RuntimeA2AExecutor(agent, heartbeat=None, model="test-model")
    monkeypatch.setattr("molecule_runtime.a2a_executor.set_current_task", AsyncMock())
    fake_updater = _FakeUpdater()
    monkeypatch.setattr(
        "molecule_runtime.a2a_executor.TaskUpdater", lambda *a, **kw: fake_updater
    )
    return executor, fake_updater


@pytest.mark.asyncio
async def test_kernel_on_stalled_turn_is_ended_by_lease(monkeypatch):
    """Kernel ON, no subprocess activity file: the lease expires within TTL and
    the idle-cap handler ends the turn (terminal failure) instead of hanging."""
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    # Install a short-TTL lease directly (the constructor floor is 1e-6, so this
    # avoids the 1.0s default-TTL floor and keeps the test sub-second). This is
    # exactly what kernel.install() would publish, just with a test TTL.
    tl.install(tl.TurnLease(ttl_seconds=0.02))
    try:
        assert tl.current() is not None, "precondition: lease installed"
        executor, updater = _hang_executor(monkeypatch)
        ctx = _build_context("do something", "ctx-stall-on")
        event_queue = MagicMock()
        event_queue.enqueue_event = AsyncMock()
        await executor._core_execute(ctx, event_queue)
        assert any(name == "failed" for name, _ in updater.events), (
            f"stalled turn must end with a terminal failure; saw {updater.events!r}"
        )
    finally:
        tl.install(None)


@pytest.mark.asyncio
async def test_kernel_off_idle_cap_still_ends_turn(monkeypatch):
    """Kernel OFF (no lease): the turn still ends on the native idle-cap alone —
    the lease wiring changed NOTHING in the default flow (byte-identical)."""
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    kernel.install()  # no-op when disabled -> no lease
    assert tl.current() is None, "kernel off -> no lease installed"
    executor, updater = _hang_executor(monkeypatch)
    ctx = _build_context("do something", "ctx-stall-off")
    event_queue = MagicMock()
    event_queue.enqueue_event = AsyncMock()
    await executor._core_execute(ctx, event_queue)
    assert any(name == "failed" for name, _ in updater.events), (
        f"idle-cap must still end the turn with kernel off; saw {updater.events!r}"
    )
