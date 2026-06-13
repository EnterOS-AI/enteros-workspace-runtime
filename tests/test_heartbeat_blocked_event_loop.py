"""#2723 (300s tool-chain-lost) — the alive-signal heartbeat must survive a
BLOCKED asyncio event loop.

Root cause (confirmed by core-devops in molecule-core#2723): the canvas→agent
A2A turn is wrapped by workspace-server ``applyIdleTimeout``, which cancels the
turn after ``A2A_IDLE_TIMEOUT_SECONDS`` of broadcaster silence. ANY workspace
SSE event resets that clock — including ``WORKSPACE_HEARTBEAT``, broadcast each
time the runtime POSTs ``/registry/heartbeat``. So a healthy ~30s heartbeat
means the idle window can never trip. Hitting 300s therefore means the
HEARTBEAT STOPPED.

The pre-fix heartbeat ran as an ``asyncio.create_task`` on the agent's SHARED
event loop. A long synchronous / CPU-bound tool step (bulk file download/upload
via a sync client, big inline subprocess, image processing — the live JRS SEO
agent 102-image batch) blocks that loop, starving the heartbeat task → no
``/heartbeat`` for minutes → idle watchdog kills the turn.

The fix runs the alive-signal POST on a DEDICATED OS THREAD, independent of the
event loop. This test blocks the event loop with ``time.sleep`` (no awaits) and
asserts the heartbeat still fired several times during the block. Against the
OLD asyncio-task implementation it would fire ZERO times during the block and
fail.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402


_WS = "00000000-0000-0000-0000-000000002723"


class _CountingSyncClient:
    """Stand-in for ``httpx.Client`` that counts heartbeat POSTs in a
    thread-safe way. Construction returns ``self`` from the context the
    runtime uses it (``httpx.Client(timeout=...)`` then ``.post(...)``)."""

    def __init__(self, *args, **kwargs):
        self.post_count = 0
        self._lock = threading.Lock()

    def post(self, url, json=None, headers=None, timeout=None):
        with self._lock:
            self.post_count += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={})
        return resp

    def close(self):
        pass


@pytest.mark.asyncio
async def test_heartbeat_survives_blocked_event_loop():
    interval = 0.05  # fast cadence so the block window covers many beats

    # One shared client instance so every (re)construction counts into the
    # same tally; the runtime only builds one until a failure run anyway.
    client = _CountingSyncClient()

    with patch.object(hb_mod.httpx, "Client", return_value=client), \
         patch.object(hb_mod, "auth_headers", return_value={}), \
         patch.object(hb_mod, "_runtime_state_payload", return_value={}), \
         patch.object(hb_mod, "_runtime_metadata_payload", return_value={}), \
         patch.object(hb_mod, "_persist_inbound_secret_from_heartbeat", lambda resp: None):

        loop = HeartbeatLoop(
            "http://platform.test",
            _WS,
            interval_seconds=interval,
        )
        # Replace the async delegation loop with a no-op: this test pins the
        # alive-signal thread, not delegation polling.
        async def _noop_loop():
            while True:
                await asyncio.sleep(3600)
        loop._loop = _noop_loop  # type: ignore[assignment]

        loop.start()
        try:
            # Let the thread get going, then HARD-BLOCK the event loop with a
            # synchronous sleep (no awaits) — exactly what a long sync tool
            # step does. An asyncio-task heartbeat would be frozen here.
            await asyncio.sleep(interval * 2)
            count_before_block = client.post_count

            block_seconds = interval * 12
            time.sleep(block_seconds)  # BLOCKS the event loop, NOT the thread

            count_after_block = client.post_count
        finally:
            await loop.stop()

        beats_during_block = count_after_block - count_before_block
        # During a block of ~12 intervals the dedicated thread should have
        # POSTed many times. Be generous against CI scheduling jitter — the
        # old implementation would yield 0 here.
        assert beats_during_block >= 3, (
            f"heartbeat starved during blocked event loop: only "
            f"{beats_during_block} beats in {block_seconds:.2f}s "
            f"(interval={interval}s); thread heartbeat should be unaffected "
            f"by event-loop blocking"
        )

        # Thread must actually be stopped and joined after stop().
        assert loop._hb_thread is None


@pytest.mark.asyncio
async def test_heartbeat_thread_stops_cleanly():
    """stop() signals + joins the dedicated thread promptly (the stop Event
    interrupts the interval sleep)."""
    client = _CountingSyncClient()
    with patch.object(hb_mod.httpx, "Client", return_value=client), \
         patch.object(hb_mod, "auth_headers", return_value={}), \
         patch.object(hb_mod, "_runtime_state_payload", return_value={}), \
         patch.object(hb_mod, "_runtime_metadata_payload", return_value={}), \
         patch.object(hb_mod, "_persist_inbound_secret_from_heartbeat", lambda resp: None):

        loop = HeartbeatLoop("http://platform.test", _WS, interval_seconds=30)

        async def _noop_loop():
            while True:
                await asyncio.sleep(3600)
        loop._loop = _noop_loop  # type: ignore[assignment]

        loop.start()
        thread = loop._hb_thread
        assert thread is not None and thread.is_alive()

        t0 = time.monotonic()
        await loop.stop()
        elapsed = time.monotonic() - t0

        # Even with a 30s interval, stop returns fast because the stop Event
        # interrupts the sleep — must not block for the full interval.
        assert elapsed < 5.0, f"stop() took {elapsed:.2f}s — interval sleep not interruptible"
        assert loop._hb_thread is None
        assert not thread.is_alive()
