"""Persistence + classification for the auto-disable/stale engine (RFC #8).

Auto-wired: the runtime unit lane globs tests/test_*.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from molecule_runtime import schedule_outcome as so
from molecule_runtime.schedule_engine import EMPTY, NEUTRAL, OK, SDK_ERROR


# ── classification ─────────────────────────────────────────────────────────

def test_classify_final_text_empty_vs_ok():
    assert so.classify_final_text("(no response generated)") == EMPTY
    assert so.classify_final_text("   ") == EMPTY
    assert so.classify_final_text("") == EMPTY
    assert so.classify_final_text("here is the sweep result") == OK


def test_classify_error_provider_errors_are_sdk_error():
    assert so.classify_error_text("Error: rate limit exceeded") == SDK_ERROR
    assert so.classify_error_text("HTTP 429 Too Many Requests") == SDK_ERROR
    assert so.classify_error_text("model is Overloaded, try again") == SDK_ERROR
    assert so.classify_error_text("insufficient credits remaining") == SDK_ERROR
    assert so.classify_error_text("monthly quota exhausted") == SDK_ERROR


def test_classify_error_internal_bugs_are_neutral():
    # An internal crash must NEVER advance the disable streak (fail-safe).
    assert so.classify_error_text("KeyError: 'foo'") == NEUTRAL
    assert so.classify_error_text("Traceback: NoneType has no attribute") == NEUTRAL
    assert so.classify_error_text("") == NEUTRAL
    # negative control: 'generate'/'author' must not trip the word-boundaried rate re
    assert so.classify_error_text("failed to generate the authored report") == NEUTRAL


# ── persistence roundtrip ──────────────────────────────────────────────────

def test_record_and_persist_roundtrip(tmp_path: Path):
    so.record_and_persist(tmp_path, "nightly", SDK_ERROR, error="rate")
    so.record_and_persist(tmp_path, "nightly", SDK_ERROR, error="rate")
    hm = so.load_health_map(tmp_path)
    assert hm["nightly"].consecutive_sdk_errors == 2
    assert hm["nightly"].last_error == "rate"
    # third error disables
    action = so.record_and_persist(tmp_path, "nightly", SDK_ERROR, error="rate")
    assert action.disable is True
    assert so.load_health_map(tmp_path)["nightly"].last_status == "disabled"


def test_two_schedules_are_independent(tmp_path: Path):
    so.record_and_persist(tmp_path, "a", SDK_ERROR)
    so.record_and_persist(tmp_path, "a", SDK_ERROR)
    so.record_and_persist(tmp_path, "b", EMPTY)
    hm = so.load_health_map(tmp_path)
    assert hm["a"].consecutive_sdk_errors == 2
    assert hm["b"].consecutive_empty_runs == 1
    assert hm["b"].consecutive_sdk_errors == 0


def test_missing_file_loads_clean(tmp_path: Path):
    assert so.load_health_map(tmp_path) == {}


def test_corrupt_file_degrades_to_clean_never_raises(tmp_path: Path):
    (tmp_path / so.HEALTH_FILENAME).write_text("{ not json", encoding="utf-8")
    assert so.load_health_map(tmp_path) == {}
    # and a subsequent record still works (overwrites the corrupt file)
    so.record_and_persist(tmp_path, "x", OK)
    assert "x" in so.load_health_map(tmp_path)


def test_saved_file_is_valid_json_with_expected_shape(tmp_path: Path):
    so.record_and_persist(tmp_path, "s", EMPTY)
    doc = json.loads((tmp_path / so.HEALTH_FILENAME).read_text())
    assert doc["schedules"]["s"]["consecutive_empty_runs"] == 1
    assert set(doc["schedules"]["s"]) == {
        "consecutive_sdk_errors",
        "consecutive_empty_runs",
        "last_status",
        "last_error",
    }
