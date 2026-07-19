"""Unit tests for the pure per-schedule health engine (RFC invariant #8).

These pin the auto-disable(3 SDK)/stale(3 empty) semantics recovered from the
retired core scheduler. The load-bearing test is ``test_interleaved_sdk_and_empty_
never_disables`` — the negative control proving the engine is NOT more aggressive
than old core (an empty run resets the SDK-error streak, so an agent that
alternates a transient error with an empty run is never silently disabled).

Auto-wired to CI: the runtime unit lane globs ``tests/test_*.py`` (ci.yml, pytest
-q --ignore=tests/integration), so no per-file registration is required.
"""

from __future__ import annotations

from molecule_runtime.schedule_engine import (
    DISABLE_AFTER_SDK_ERRORS,
    STALE_AFTER_EMPTY_RUNS,
    OutcomeAction,
    ScheduleHealth,
    record_outcome,
)


def _run(seq):
    """Replay an outcome sequence; return (health, list_of_actions)."""
    h = ScheduleHealth()
    actions = []
    for o in seq:
        actions.append(record_outcome(h, o))
    return h, actions


# ── auto-disable arm ───────────────────────────────────────────────────────

def test_three_consecutive_sdk_errors_disable():
    h, actions = _run(["sdk_error"] * DISABLE_AFTER_SDK_ERRORS)
    assert actions[0] == OutcomeAction(disable=False, status="error")
    assert actions[1] == OutcomeAction(disable=False, status="error")
    assert actions[-1] == OutcomeAction(disable=True, status="disabled")
    assert h.consecutive_sdk_errors == DISABLE_AFTER_SDK_ERRORS
    assert h.last_status == "disabled"


def test_two_sdk_errors_then_ok_does_not_disable():
    h, actions = _run(["sdk_error", "sdk_error", "ok"])
    assert not any(a.disable for a in actions)
    assert h.consecutive_sdk_errors == 0
    assert h.last_status == "ok"


def test_sdk_error_records_last_error():
    h = ScheduleHealth()
    record_outcome(h, "sdk_error", error="rate_limited")
    assert h.last_error == "rate_limited"
    # a clean run clears it
    record_outcome(h, "ok")
    assert h.last_error == ""


# ── stale arm (empty) — never disables ─────────────────────────────────────

def test_three_consecutive_empty_marks_stale_never_disables():
    h, actions = _run(["empty"] * STALE_AFTER_EMPTY_RUNS)
    assert not any(a.disable for a in actions)
    assert actions[-1].status == "stale"
    assert h.last_status == "stale"
    assert h.consecutive_empty_runs == STALE_AFTER_EMPTY_RUNS


def test_empty_below_threshold_stays_ok():
    h, actions = _run(["empty", "empty"])
    assert h.last_status == "ok"
    assert all(a.status == "ok" and not a.disable for a in actions)


def test_ok_resets_empty_streak():
    h, _ = _run(["empty", "empty", "ok", "empty"])
    assert h.consecutive_empty_runs == 1
    assert h.last_status == "ok"


# ── THE negative control: interleaved error/empty must NEVER disable ────────

def test_interleaved_sdk_and_empty_never_disables():
    # old core reset the SDK streak on every ok-status (incl. empty) run, so this
    # sequence NEVER reaches 3 consecutive SDK errors. The pre-fix engine (empty
    # not resetting the SDK streak) would auto-disable at the final sdk_error —
    # silently killing a partially-working schedule (the #976 data-loss class).
    seq = ["sdk_error", "empty", "sdk_error", "empty", "sdk_error"]
    h, actions = _run(seq)
    assert not any(a.disable for a in actions), "interleaved error/empty must not disable"
    assert h.consecutive_sdk_errors == 1  # only the trailing error survives
    # a longer alternating tail is still safe
    h2, actions2 = _run(seq * 4)
    assert not any(a.disable for a in actions2)
    assert h2.consecutive_sdk_errors == 1


def test_empty_resets_sdk_streak_explicitly():
    h = ScheduleHealth()
    record_outcome(h, "sdk_error")
    record_outcome(h, "sdk_error")
    assert h.consecutive_sdk_errors == 2
    record_outcome(h, "empty")  # the ok/empty else-branch resets the SDK streak
    assert h.consecutive_sdk_errors == 0
    # so a following single error is only #1, not the disabling #3
    a = record_outcome(h, "sdk_error")
    assert not a.disable


# ── neutral (unobserved) moves no counter ──────────────────────────────────

def test_neutral_never_moves_a_counter():
    h = ScheduleHealth(consecutive_sdk_errors=2, consecutive_empty_runs=1, last_status="error")
    a = record_outcome(h, "neutral")
    assert a == OutcomeAction(disable=False, status="error")
    assert h.consecutive_sdk_errors == 2
    assert h.consecutive_empty_runs == 1
    assert h.last_status == "error"


def test_two_sdk_then_two_neutral_then_sdk_does_not_disable():
    # neutral outcomes neither advance nor reset — a real 3rd error still disables,
    # but interposed neutrals must not manufacture a disable on their own.
    h, actions = _run(["sdk_error", "sdk_error", "neutral", "neutral"])
    assert not any(a.disable for a in actions)
    assert h.consecutive_sdk_errors == 2  # neutrals left it untouched
    a = record_outcome(h, "sdk_error")
    assert a.disable  # the genuine 3rd consecutive error


def test_unknown_outcome_is_treated_as_neutral():
    h = ScheduleHealth(consecutive_sdk_errors=2)
    a = record_outcome(h, "garbage")
    assert not a.disable
    assert h.consecutive_sdk_errors == 2
