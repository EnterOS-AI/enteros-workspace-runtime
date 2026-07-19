"""Persist + attribute per-schedule health outcomes for the auto-disable/stale
engine (RFC invariant #8).

Bridges the pure :mod:`molecule_runtime.schedule_engine` state machine to the
durable ``schedule-engine.json`` in the trigger state dir and the
source-preserving :meth:`ScheduleStore.set_enabled` disable. Kept out of the hot
``a2a_executor`` path as a small, fully-unit-testable module: everything here is
pure-plus-file-IO and exercised directly by ``tests/test_schedule_outcome.py``.

Fidelity note: a self-scheduled turn's outcome is classified exactly like old
core's ``detectResultKind`` — an empty response marks progress toward *stale*, a
recognized provider error (rate-limited / quota / overloaded, after the adapter
already exhausted its transient-retry budget) advances the *disable* streak, and
anything else the runtime cannot positively attribute to the provider is
``neutral`` (never disable a user's schedule on our own internal crash).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from molecule_runtime.schedule_engine import (
    EMPTY,
    NEUTRAL,
    OK,
    SDK_ERROR,
    OutcomeAction,
    ScheduleHealth,
    record_outcome,
)

HEALTH_FILENAME = "schedule-engine.json"
# The runtime's own empty-turn sentinel (a2a_executor builds this when no tokens
# were accumulated). Kept in sync with that call site.
EMPTY_RESPONSE_SENTINEL = "(no response generated)"

# Reuse the runtime's existing provider-error taxonomy so classification never
# drifts from the user-facing error categories.
try:  # pragma: no cover - trivial import guard
    from molecule_runtime.executor_helpers import _RATE_LIMIT_RE
except Exception:  # pragma: no cover - defensive fallback
    _RATE_LIMIT_RE = re.compile(r"\brate\b|\b429\b|\boverloaded\b", re.IGNORECASE)

# Provider quota / credit exhaustion — the other half of old core's "SDK error"
# resultKind. Word-boundaried to avoid false positives.
_QUOTA_RE = re.compile(
    r"\bquota\b|\binsufficient[ _-]?(?:credit|credits|quota|balance|funds)\b|\b402\b",
    re.IGNORECASE,
)


def classify_final_text(final_text: str) -> str:
    """A completed turn is ``empty`` (no content) or ``ok`` (produced content)."""
    stripped = (final_text or "").strip()
    if stripped == "" or stripped == EMPTY_RESPONSE_SENTINEL:
        return EMPTY
    return OK


def classify_error_text(err_text: str) -> str:
    """Map a failed turn's error text to ``sdk_error`` or ``neutral``.

    Only a recognized provider error (rate-limit / quota / overloaded) advances
    the auto-disable streak — the adapter has already exhausted its transient
    retries by the time such an error reaches the executor, so a persistent
    condition is exactly what old core disabled on. Every other failure
    (an internal runtime bug, a parse error, a cancellation) is ``neutral``:
    never silently kill a user's schedule because the *runtime* misbehaved.
    """
    text = err_text or ""
    if _RATE_LIMIT_RE.search(text) or _QUOTA_RE.search(text):
        return SDK_ERROR
    return NEUTRAL


def _health_path(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / HEALTH_FILENAME


def load_health_map(state_dir: str | os.PathLike[str]) -> dict[str, ScheduleHealth]:
    """Load the per-schedule health map; a missing/corrupt file starts clean.

    Health is non-authoritative bookkeeping — an unreadable file must never crash
    a turn, so it degrades to an empty map (the streaks simply restart).
    """
    path = _health_path(state_dir)
    if not path.is_file():
        return {}
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    out: dict[str, ScheduleHealth] = {}
    schedules = raw.get("schedules") if isinstance(raw, dict) else None
    if isinstance(schedules, dict):
        for name, h in schedules.items():
            if not isinstance(h, dict):
                continue
            out[name] = ScheduleHealth(
                consecutive_sdk_errors=int(h.get("consecutive_sdk_errors", 0) or 0),
                consecutive_empty_runs=int(h.get("consecutive_empty_runs", 0) or 0),
                last_status=str(h.get("last_status", "ok") or "ok"),
                last_error=str(h.get("last_error", "") or ""),
            )
    return out


def save_health_map(
    state_dir: str | os.PathLike[str], health_map: dict[str, ScheduleHealth]
) -> None:
    """Atomically persist the health map (temp-file + os.replace, like the store)."""
    path = _health_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schedules": {
            name: {
                "consecutive_sdk_errors": h.consecutive_sdk_errors,
                "consecutive_empty_runs": h.consecutive_empty_runs,
                "last_status": h.last_status,
                "last_error": h.last_error,
            }
            for name, h in health_map.items()
        }
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_and_persist(
    state_dir: str | os.PathLike[str],
    schedule_name: str,
    outcome: str,
    error: str = "",
) -> OutcomeAction:
    """Advance + persist one schedule's health; return the engine's action.

    The caller performs the (source-preserving) disable when
    ``action.disable`` is True.
    """
    health_map = load_health_map(state_dir)
    health = health_map.get(schedule_name) or ScheduleHealth()
    action = record_outcome(health, outcome, error=error)
    health_map[schedule_name] = health
    save_health_map(state_dir, health_map)
    return action
