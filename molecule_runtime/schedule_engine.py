"""Pure per-schedule health state machine — mirrors the retired core scheduler's
auto-disable(3 SDK errors)/stale(3 empty) engine bookkeeping (RFC invariant #8).

NO I/O, NO async — exhaustively unit-testable like trigger_schedule.evaluate_tick.
The async persistence + the auto-disable write live in the caller (a2a_executor +
ScheduleStore.set_enabled).

Semantics recovered from the retired molecule-core native scheduler (scheduler.go,
deleted by P4 core-loop removal). The load-bearing detail — verified against that
file and re-proven by the negative-control test below — is that the "ok/empty"
else-branch ALWAYS resets the SDK-error streak, exactly as old core reset
consecutive_sdk_errors=0 on any run whose resultKind was "" or "ok" (an empty
"(no response generated)" run has resultKind "" and stays last_status="ok" until
it goes stale). Only a genuine SDK error (rate_limited / quota_exhausted /
sdk_error taxonomy) advances the disable streak; a neutral (unobserved) outcome
moves no counter, because old core only ever acted on an observed respBody.
"""
from __future__ import annotations

from dataclasses import dataclass

DISABLE_AFTER_SDK_ERRORS = 3
STALE_AFTER_EMPTY_RUNS = 3

# The four outcomes the runtime can attribute to a fired schedule.
OK = "ok"
EMPTY = "empty"
SDK_ERROR = "sdk_error"
NEUTRAL = "neutral"
_OUTCOMES = frozenset({OK, EMPTY, SDK_ERROR, NEUTRAL})


@dataclass
class ScheduleHealth:
    consecutive_sdk_errors: int = 0
    consecutive_empty_runs: int = 0
    last_status: str = "ok"
    last_error: str = ""


@dataclass
class OutcomeAction:
    disable: bool = False
    status: str = "ok"


def record_outcome(health: ScheduleHealth, outcome: str, error: str = "") -> OutcomeAction:
    """Advance ``health`` by one observed run outcome and report the action.

    ``disable=True`` is returned only on the 3rd consecutive SDK error; the caller
    is responsible for the (source-preserving) persisted disable. A "stale" status
    is informational only and never disables.
    """
    if outcome not in _OUTCOMES:
        # Unknown outcome is treated as neutral — never move a counter on a signal
        # we don't understand (fail-safe toward keeping the schedule enabled).
        outcome = NEUTRAL

    if outcome == NEUTRAL:
        # A delivery the runtime never observed: leave every counter and the status
        # untouched (old core only acted on an observed respBody).
        return OutcomeAction(disable=False, status=health.last_status)

    if outcome == SDK_ERROR:
        health.consecutive_sdk_errors += 1
        health.last_error = error
        if health.consecutive_sdk_errors >= DISABLE_AFTER_SDK_ERRORS:
            health.last_status = "disabled"
            return OutcomeAction(disable=True, status="disabled")
        health.last_status = "error"
        return OutcomeAction(disable=False, status="error")

    # ── ok / empty else-branch: ALWAYS reset the SDK-error streak ──
    # This is the load-bearing fidelity fix. Old core reset consecutive_sdk_errors
    # to 0 on ANY run whose resultKind was "" or "ok" — which includes an empty
    # run. Without this reset the engine is strictly more aggressive than old core
    # and would auto-disable a schedule that merely alternates transient errors with
    # empty runs (the #976 "consecutive false-failure ticks" data-loss class).
    health.consecutive_sdk_errors = 0
    health.last_error = ""

    if outcome == EMPTY:
        health.consecutive_empty_runs += 1
        if health.consecutive_empty_runs >= STALE_AFTER_EMPTY_RUNS:
            health.last_status = "stale"
            return OutcomeAction(disable=False, status="stale")  # STALE != disable
        health.last_status = "ok"
        return OutcomeAction(disable=False, status="ok")

    # clean, non-empty ok run: reset both streaks
    health.consecutive_empty_runs = 0
    health.last_status = "ok"
    return OutcomeAction(disable=False, status="ok")
