"""Tests for the autonomous-loop replay guard.

Reproduces the 2026-06-29 RUNAWAY autonomous re-post incident: a platform
agent's idle/cron/harvester self-wake re-emitted the SAME A2A delegation-
result replay on ~every turn (counter-bumping "Idempotency now N records",
a STALE "PR #195 not merged" verdict, "no new info, no state changes") until
the session hit ~130 messages and the workspace flipped to DEGRADED.

Three defects are covered:

  1. IDEMPOTENCY ENFORCED (not observe-only): an already-delivered result —
     keyed by idempotency_key or recognised as a byte-/near-identical
     re-narration — is SUPPRESSED, not re-emitted.
  2. CIRCUIT BREAKER: N consecutive identical / no-new-info self-fired
     outputs trip a breaker that HALTS the loop and marks the runtime
     degraded (via runtime_wedge) with a concrete reason.
  3. NO STALE REPLAY: the loop halts instead of re-announcing the cached
     stale verdict forever.

The pure-guard tests have no third-party dependencies. The real-path test
drives the actual ``RuntimeA2AExecutor`` and asserts a routine self-ping's
duplicate output is suppressed + the breaker trips — while a USER turn with
the same text is never suppressed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from molecule_runtime import autonomous_loop_guard as guard
from molecule_runtime import runtime_wedge


# The actual message the looping concierge re-emitted (lightly trimmed). It
# carries BOTH the near-identical counter ("Idempotency now 15 records") and
# the explicit no-op markers ("Replay of", "No new info, no state changes",
# "still pending your decision") — the signals the guard keys on.
INCIDENT_TEXT = (
    "A2A delegation result from peer 7ca64ad4 (agents-team ingest) - completed. "
    "Replay of the prior molecule-ai-workspace-runtime PR #195 review result: "
    "APPROVED, not merged. Idempotency now 15 records. No new info, no state "
    "changes. Approval b1c53508 (org-wide expansion across all 49 molecule-ai "
    "repos) still pending your decision."
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Guard + wedge are process-scoped singletons — reset around each test."""
    guard.reset_for_test()
    runtime_wedge.reset_for_test()
    yield
    guard.reset_for_test()
    runtime_wedge.reset_for_test()


# ── Signature / no-op detection ─────────────────────────────────────────────


def test_signature_collapses_volatile_counters_and_ids():
    """'Idempotency now 15 records' and '... 16 records' must hash equal so a
    counter-bumping loop can't evade the breaker."""
    a = "Idempotency now 15 records for peer 7ca64ad4"
    b = "Idempotency now 16 records for peer 9ff01bd2"
    assert guard.output_signature(a) == guard.output_signature(b)


def test_distinct_substantive_outputs_have_distinct_signatures():
    assert guard.output_signature("Deployed staging and ran the e2e gate") != (
        guard.output_signature("Opened PR to fix the reaper crash")
    )


def test_incident_text_recognised_as_noop():
    assert guard.looks_like_noop(INCIDENT_TEXT) is True


def test_plain_progress_text_is_not_noop():
    assert guard.looks_like_noop("Merged PR #195 and kicked off the deploy") is False


# ── Idempotency: enforce, not observe ───────────────────────────────────────


def test_first_meaningful_keyed_output_emits_then_redelivery_suppressed():
    """The core idempotency contract: a completed delegation whose result was
    ALREADY delivered must NOT be re-emitted."""
    first = guard.evaluate_autonomous_output(
        "Reviewed PR #195: APPROVED and merged; deploy unblocked.",
        idempotency_key="deleg-195",
    )
    assert first == guard.EMIT
    assert guard.is_result_delivered("deleg-195") is True

    # Re-post of the SAME delegation result (even with different prose) is a
    # replay of already-delivered work → suppressed.
    again = guard.evaluate_autonomous_output(
        "Re-reporting delegation result for PR #195 (approved).",
        idempotency_key="deleg-195",
    )
    assert again == guard.SUPPRESS


def test_mark_and_query_delivered_helpers():
    assert guard.is_result_delivered("k") is False
    guard.mark_result_delivered("k")
    assert guard.is_result_delivered("k") is True


# ── Byte / near-identical replay suppression ────────────────────────────────


def test_byte_identical_repeat_is_suppressed():
    out = "Status unchanged; awaiting your approval."
    # First emit is meaningful (not a no-op marker).
    assert guard.evaluate_autonomous_output(out) == guard.EMIT
    # Exact repeat is a replay.
    assert guard.evaluate_autonomous_output(out) == guard.SUPPRESS


def test_near_identical_counter_bump_is_suppressed():
    assert guard.evaluate_autonomous_output("Heartbeat ok, 1 record processed.") == guard.EMIT
    # Only the digit changes → same signature → suppressed.
    assert guard.evaluate_autonomous_output("Heartbeat ok, 2 record processed.") == guard.SUPPRESS


# ── Circuit breaker ─────────────────────────────────────────────────────────


def test_circuit_breaker_trips_after_threshold_and_degrades_runtime():
    """N consecutive identical / no-op outputs → HALT + runtime marked
    degraded with a concrete reason. (Default threshold = 3.)"""
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.HALT

    assert guard.should_halt() is True
    assert "autonomous loop halted" in guard.halt_reason()
    # The breaker flips the workspace to degraded via runtime_wedge — this is
    # how the heartbeat surfaces it to the platform/canvas.
    assert runtime_wedge.is_wedged() is True
    assert "autonomous loop halted" in runtime_wedge.wedge_reason()


def test_halt_is_sticky():
    for _ in range(3):
        guard.evaluate_autonomous_output(INCIDENT_TEXT)
    assert guard.should_halt() is True
    # Any further autonomous output keeps returning HALT (loop must stop).
    assert guard.evaluate_autonomous_output("anything at all") == guard.HALT
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.HALT


def test_meaningful_output_resets_the_breaker():
    """A genuinely productive interleaved turn resets the consecutive counter,
    so the breaker never false-trips on an agent that is actually doing work."""
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    # Real work happens → counter resets.
    assert guard.evaluate_autonomous_output("Provisioned workspace foo and ran the smoke test.") == guard.EMIT
    # Need the full threshold again before halting.
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    assert guard.should_halt() is False
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.HALT
    assert guard.should_halt() is True


def test_three_distinct_productive_outputs_do_not_halt():
    """Busy, productive autonomous agent (all-different outputs) is never
    halted or degraded."""
    for out in (
        "Deployed CP to staging; e2e gate green.",
        "Opened PR #321 to fix the reaper typed-nil crash.",
        "Backfilled LLM keys for tenant reno-stars.",
    ):
        assert guard.evaluate_autonomous_output(out) == guard.EMIT
    assert guard.should_halt() is False
    assert runtime_wedge.is_wedged() is False


def test_threshold_is_env_overridable(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_LOOP_MAX_IDENTICAL", "2")
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.SUPPRESS
    assert guard.evaluate_autonomous_output(INCIDENT_TEXT) == guard.HALT


def test_malformed_threshold_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_LOOP_MAX_IDENTICAL", "not-an-int")
    assert guard.max_identical() == 3


# ── Real-path reproduction: drive the actual RuntimeA2AExecutor ─────────────

a2a = pytest.importorskip("a2a", reason="a2a-sdk required for executor path")
try:
    from molecule_runtime.a2a_executor import (  # noqa: E402
        RuntimeA2AExecutor,
        A2A_MESSAGE_SOURCE_TYPE,
        A2A_SOURCE_SELF_IDLE,
        _ROUTINE_SELF_SOURCE_TYPES,
        _is_routine_self_message,
    )
    _EXECUTOR_IMPORTABLE = True
except Exception:  # pragma: no cover - environment without full runtime deps
    _EXECUTOR_IMPORTABLE = False

pytestmark_executor = pytest.mark.skipif(
    not _EXECUTOR_IMPORTABLE, reason="runtime executor deps unavailable"
)


class _FakeUpdater:
    """Records TaskUpdater calls so the test can inspect the final message."""

    def __init__(self):
        self.completed_message = None
        self.events: list[str] = []

    async def start_work(self):
        self.events.append("start_work")

    async def add_artifact(self, **_kwargs):
        self.events.append("add_artifact")

    async def complete(self, message=None):
        self.events.append("complete")
        self.completed_message = message

    async def failed(self, message=None):
        self.events.append("failed")
        self.completed_message = message


def _routine_self_ping_context(text: str, context_id: str):
    """A2A RequestContext stub carrying the self-idle routine marker."""
    part = SimpleNamespace(text=text, root=None)
    msg = SimpleNamespace(
        parts=[part],
        metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_IDLE},
    )
    return SimpleNamespace(
        message=msg,
        task_id=f"task-{context_id}",
        context_id=context_id,
        current_task=SimpleNamespace(),  # non-None → skip v1 Task pre-enqueue
        metadata=None,
    )


def _user_context(text: str, context_id: str):
    part = SimpleNamespace(text=text, root=None)
    msg = SimpleNamespace(parts=[part], metadata=None)
    return SimpleNamespace(
        message=msg,
        task_id=f"task-{context_id}",
        context_id=context_id,
        current_task=SimpleNamespace(),
        metadata=None,
    )


def _agent_streaming(text: str):
    async def _astream(payload, **_kwargs):
        yield {
            "event": "on_chat_model_stream",
            "run_id": "run-1",
            "data": {"chunk": SimpleNamespace(content=text)},
        }
    agent = type("FakeAgent", (), {})()
    agent.astream_events = _astream
    return agent


def _message_text(message) -> str:
    """Pull the text out of whatever new_text_message produced."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    parts = getattr(message, "parts", None) or []
    for p in parts:
        t = getattr(p, "text", None) or getattr(getattr(p, "root", None), "text", None)
        if t:
            return t
    return str(message)


def test_self_idle_marker_is_a_routine_self_ping():
    assert A2A_SOURCE_SELF_IDLE in _ROUTINE_SELF_SOURCE_TYPES
    ctx = _routine_self_ping_context("anything", "c1")
    assert _is_routine_self_message(ctx, "anything") is True
    # A user turn is NOT a routine self-ping.
    assert _is_routine_self_message(_user_context("hi", "c2"), "hi") is False


@pytest.mark.asyncio
async def test_executor_suppresses_then_halts_runaway_replay(monkeypatch):
    """End-to-end on the real executor: a routine self-ping that keeps
    producing the same delegation-result replay is suppressed, then trips the
    breaker — and the runtime is marked degraded."""
    from unittest.mock import AsyncMock
    import molecule_runtime.a2a_executor as ax

    monkeypatch.setattr(ax, "set_current_task", AsyncMock())
    fake_updater = _FakeUpdater()
    monkeypatch.setattr(ax, "TaskUpdater", lambda *a, **kw: fake_updater)

    executor = RuntimeA2AExecutor(_agent_streaming(INCIDENT_TEXT), heartbeat=None, model="test")

    results = []
    for i in range(3):
        eq = SimpleNamespace(enqueue_event=AsyncMock())
        res = await executor._core_execute(
            _routine_self_ping_context(INCIDENT_TEXT, f"idle-{i}"), eq
        )
        results.append(res)

    # First two self-pings: the duplicate replay is suppressed (NOT the raw
    # incident text the agent tried to re-broadcast).
    assert "suppressed" in results[0]
    assert "suppressed" in results[1]
    assert "7ca64ad4" not in results[0]  # the stale replay was not re-emitted
    # Third: breaker trips, loop halts.
    assert "halted" in results[2]

    # The final message the executor actually emitted carries the halt notice,
    # not the stale replay.
    assert "halted" in _message_text(fake_updater.completed_message)

    # Loop is halted + runtime degraded so the idle loop will stop firing.
    assert guard.should_halt() is True
    assert runtime_wedge.is_wedged() is True
    assert "autonomous loop halted" in runtime_wedge.wedge_reason()


@pytest.mark.asyncio
async def test_executor_never_suppresses_a_user_turn(monkeypatch):
    """The SAME text on a user-directed turn must be answered, never gated."""
    from unittest.mock import AsyncMock
    import molecule_runtime.a2a_executor as ax

    monkeypatch.setattr(ax, "set_current_task", AsyncMock())
    fake_updater = _FakeUpdater()
    monkeypatch.setattr(ax, "TaskUpdater", lambda *a, **kw: fake_updater)

    executor = RuntimeA2AExecutor(_agent_streaming(INCIDENT_TEXT), heartbeat=None, model="test")
    eq = SimpleNamespace(enqueue_event=AsyncMock())
    res = await executor._core_execute(_user_context(INCIDENT_TEXT, "user-1"), eq)

    # User turn returns the real answer — no suppression, no halt, no degrade.
    assert res == INCIDENT_TEXT
    assert guard.should_halt() is False
    assert runtime_wedge.is_wedged() is False


# Apply the executor-availability skip to the executor-path tests.
for _name in (
    "test_self_idle_marker_is_a_routine_self_ping",
    "test_executor_suppresses_then_halts_runaway_replay",
    "test_executor_never_suppresses_a_user_turn",
):
    _fn = globals().get(_name)
    if _fn is not None:
        globals()[_name] = pytestmark_executor(_fn)
