"""Autonomous-loop replay guard — circuit breaker + idempotency enforcement.

Why this exists (production incident, 2026-06-29)
-------------------------------------------------
The agents-team concierge (a platform agent) entered a RUNAWAY autonomous
re-post loop: on ~every self-fired turn it re-emitted the byte-identical A2A
delegation-result status —

    "A2A delegation result from peer 7ca64ad4 (agents-team ingest) —
     completed. Replay of the prior PR #195 review result: APPROVED ...
     not merged ... Idempotency now 15 records. No new info, no state
     changes ..."

— until the session grew to ~130 messages, the workspace flipped to
DEGRADED, and it burned LLM tokens for zero new work. Forensics showed
NOTHING durable was driving it: the `delegations` table was EMPTY and
`a2a_queue` held only completed rows. The driver was the agent's OWN
autonomous self-fire loop (the idle self-wake / cron tick / delegation
harvester re-poking the same turn), re-narrating a CACHED, STALE verdict
("PR #195 not merged" — #195 was in fact merged) every cycle.

Two defects this module closes:

  1. IDEMPOTENCY WAS OBSERVE-ONLY. The dedup store *recorded* the duplicate
     replays ("Idempotency now 15 records") but never *suppressed* the
     re-emission. ``evaluate_autonomous_output`` makes the idempotency check
     actually GATE the send: an already-delivered result (or a byte-/near-
     identical re-narration) returns ``SUPPRESS`` so the caller drops the
     re-post and ends the turn instead of re-broadcasting.

  2. NO CIRCUIT BREAKER. Nothing detected "I have produced the same no-op
     output N times in a row" and stopped. After ``max_identical()``
     consecutive identical / near-identical / explicit-"no new info"
     autonomous outputs, ``evaluate_autonomous_output`` returns ``HALT`` and
     marks the runtime degraded (via ``runtime_wedge``) with a concrete
     reason. The idle self-wake loop reads :func:`should_halt` and STOPS
     firing — a bounded stop, not an infinite re-announce.

Scope discipline
----------------
* Process-scoped, lock-guarded module state (mirrors ``runtime_wedge`` —
  the wedge is a property of the Python process, one platform agent per
  process today). The class wrap is the seam for a future per-scope variant.
* Applies ONLY to the agent's own routine self-pings (idle / cron /
  harvester) — NEVER to user-directed turns. A real user answer is never
  suppressed; the caller is responsible for that gating (see
  ``a2a_executor._is_routine_self_message``).
* No third-party imports — stdlib + ``runtime_wedge`` only — so the guard
  loads even on a partially-installed image and is unit-testable in
  isolation.

Public API: evaluate_autonomous_output(text, idempotency_key=None),
mark_result_delivered(key), is_result_delivered(key), should_halt(),
halt_reason(), reset_for_test(). Thresholds derive from env (no hardcoded
fiat values) — see ``max_identical()``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

# ── Decisions returned by evaluate_autonomous_output ────────────────────────
# EMIT     — fresh, meaningful autonomous output; send it normally.
# SUPPRESS — duplicate / already-delivered / "no new info" replay; the caller
#            MUST NOT re-broadcast it and should end the turn.
# HALT      — circuit breaker tripped (N consecutive no-op replays). The
#            runtime has been marked degraded; the caller should stop the
#            autonomous loop. Implies SUPPRESS for this output too.
EMIT = "emit"
SUPPRESS = "suppress"
HALT = "halt"

# Default number of consecutive identical / near-identical / no-op autonomous
# outputs tolerated before the breaker trips. Three is deliberately low: a
# legitimate autonomous agent that has genuinely nothing new to do should go
# quiet, not re-announce the same status — so a third identical re-post is
# already pathological. Env-overridable per the no-hardcoding principle.
_DEFAULT_MAX_IDENTICAL = 3

# Cap on remembered idempotency keys so a long-lived process can't grow the
# set unbounded. Evicts oldest-inserted half on overflow (insertion order is
# preserved by dict).
_MAX_DELIVERED_KEYS = 512


def _int_env(name: str, default: int) -> int:
    """Read a positive-int env override, falling back (with a WARNING on a
    malformed value) to ``default``. Mirrors the runtime's other env-tunable
    helpers — nothing is hardcoded by fiat."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default
    if n <= 0:
        logger.warning("%s=%d is not positive; using default %d", name, n, default)
        return default
    return n


def max_identical() -> int:
    """Consecutive no-op autonomous outputs before the breaker trips.
    Override via ``AUTONOMOUS_LOOP_MAX_IDENTICAL``."""
    return _int_env("AUTONOMOUS_LOOP_MAX_IDENTICAL", _DEFAULT_MAX_IDENTICAL)


# ── Output normalisation + no-op detection ──────────────────────────────────
# Collapse volatile tokens so "Idempotency now 15 records" and "... 16
# records" (and timestamps, ids) hash to the SAME signature — that is the
# "near-identical" the incident report calls out. Without this the loop would
# evade a byte-exact equality check by bumping a counter each turn.
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")
# Short hex/uuid-ish runs (peer ids like "7ca64ad4", approval "b1c53508").
_HEXY_RE = re.compile(r"\b[0-9a-f]{6,}\b", re.IGNORECASE)

# Explicit "nothing changed" phrases the looping agent narrates. Matching any
# of these flags the output as a no-op replay even if the surrounding wording
# drifts turn to turn (so the breaker is robust to LLM paraphrasing).
_NOOP_MARKERS = (
    "no new info",
    "no new information",
    "no state change",
    "no state changes",
    "no changes",
    "nothing new",
    "nothing to report",
    "already delivered",
    "already reported",
    "already completed",
    "already merged",
    "replay of",
    "no action needed",
    "no further action",
    "still pending your decision",
)


def _normalize(text: str) -> str:
    """Lowercase, strip volatile counters / ids / whitespace so near-identical
    re-narrations collapse to one signature."""
    t = (text or "").strip().lower()
    t = _HEXY_RE.sub("#", t)
    t = _DIGITS_RE.sub("#", t)
    t = _WS_RE.sub(" ", t)
    return t


def output_signature(text: str) -> str:
    """Stable signature of an autonomous output, robust to volatile tokens.
    Two outputs that differ only in counts / ids / timestamps share a
    signature — the property that lets the breaker see a counter-bumping loop
    as 'the same output again'."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def looks_like_noop(text: str) -> bool:
    """True when the output explicitly narrates 'no new info / no state
    change' (or is a 'replay of' a prior result)."""
    n = _normalize(text)
    return any(marker in n for marker in _NOOP_MARKERS)


class _GuardState:
    """Process-scoped guard state. Lock-guarded so the heartbeat thread, the
    async executor turn, and the idle loop can all touch it safely."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Idempotency keys whose result has already been delivered once. dict
        # (not set) so insertion order drives FIFO eviction on overflow.
        self._delivered: dict[str, None] = {}
        self._last_signature: str | None = None
        self._consecutive_noop = 0
        self._halted = False
        self._halt_reason = ""

    # ---- idempotency store (enforce, not observe) -------------------------
    def is_delivered(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            return key in self._delivered

    def mark_delivered(self, key: str) -> None:
        if not key:
            return
        with self._lock:
            self._remember_delivered_locked(key)

    def _remember_delivered_locked(self, key: str) -> None:
        self._delivered[key] = None
        if len(self._delivered) > _MAX_DELIVERED_KEYS:
            # Drop the oldest-inserted half.
            drop = len(self._delivered) - _MAX_DELIVERED_KEYS // 2
            for k in list(self._delivered)[:drop]:
                self._delivered.pop(k, None)

    # ---- circuit breaker + replay decision --------------------------------
    def evaluate(self, text: str, idempotency_key: str | None) -> str:
        """Decide EMIT / SUPPRESS / HALT for one autonomous-turn output and
        update the breaker counters. See module docstring for semantics."""
        with self._lock:
            if self._halted:
                return HALT

            sig = output_signature(text)

            already_delivered = bool(idempotency_key) and idempotency_key in self._delivered
            same_as_last = self._last_signature is not None and sig == self._last_signature
            noop = looks_like_noop(text)
            is_replay = already_delivered or same_as_last or noop

            if is_replay:
                self._consecutive_noop += 1
                self._last_signature = sig
                if self._consecutive_noop >= max_identical():
                    self._halted = True
                    self._halt_reason = (
                        "autonomous loop halted: "
                        f"{self._consecutive_noop} consecutive identical / no-new-info "
                        "self-fired outputs (runaway delegation-result replay) — "
                        "stopping self-fire to halt the loop and stop burning tokens; "
                        "restart the workspace to resume"
                    )
                    logger.error(
                        "autonomous-loop circuit breaker OPEN — %s", self._halt_reason
                    )
                    # Flip the workspace to `degraded` with a concrete reason
                    # (heartbeat reads runtime_wedge on its next tick). Best-
                    # effort: a missing runtime_wedge must not crash the turn.
                    try:
                        from molecule_runtime.runtime_wedge import mark_wedged

                        mark_wedged(self._halt_reason)
                    except Exception:  # pragma: no cover - defensive
                        logger.warning(
                            "autonomous-loop guard: could not mark runtime wedged "
                            "(continuing; loop still halted)"
                        )
                    return HALT
                logger.info(
                    "autonomous-loop guard: suppressing duplicate replay "
                    "(consecutive=%d, already_delivered=%s, noop=%s)",
                    self._consecutive_noop, already_delivered, noop,
                )
                return SUPPRESS

            # Fresh, meaningful output — reset the breaker and (when keyed)
            # record the delivery so a later re-post of the SAME result is
            # caught as an already-delivered replay.
            self._consecutive_noop = 0
            self._last_signature = sig
            if idempotency_key:
                self._remember_delivered_locked(idempotency_key)
            return EMIT

    def should_halt(self) -> bool:
        with self._lock:
            return self._halted

    def halt_reason(self) -> str:
        with self._lock:
            return self._halt_reason

    def reset(self) -> None:
        with self._lock:
            self._delivered.clear()
            self._last_signature = None
            self._consecutive_noop = 0
            self._halted = False
            self._halt_reason = ""


# Single shared instance backing the module-level helpers (one platform agent
# per process today — same posture as runtime_wedge).
_DEFAULT = _GuardState()


def evaluate_autonomous_output(text: str, idempotency_key: str | None = None) -> str:
    """Decide whether an autonomous self-fired turn's output should be emitted,
    suppressed (duplicate / already-delivered replay), or halt the loop.

    Call this ONLY for the agent's own routine self-pings (idle / cron /
    harvester) — never for user-directed turns. Returns one of EMIT /
    SUPPRESS / HALT.
    """
    return _DEFAULT.evaluate(text, idempotency_key)


def mark_result_delivered(key: str) -> None:
    """Record that a delegation-result with this idempotency key has been
    delivered once, so a later re-post of the same key is suppressed."""
    _DEFAULT.mark_delivered(key)


def is_result_delivered(key: str) -> bool:
    """True if a delegation-result with this idempotency key was already
    delivered."""
    return _DEFAULT.is_delivered(key)


def should_halt() -> bool:
    """True once the circuit breaker has tripped. The idle self-wake loop
    polls this and stops firing when True."""
    return _DEFAULT.should_halt()


def halt_reason() -> str:
    """Operator-readable reason the loop was halted, or empty string."""
    return _DEFAULT.halt_reason()


def reset_for_test() -> None:
    """Test-only: clear all guard state between cases."""
    _DEFAULT.reset()
