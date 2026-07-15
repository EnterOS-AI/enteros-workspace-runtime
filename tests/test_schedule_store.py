"""P3: the volume-authoritative schedule store (Option A).

Validation is checked against the SDK schedule contract's own fixture partition
(valid entries accepted, invalid rejected) so the store can never silently drift
from the contract, plus the byte/count caps the JSON-Schema can only approximate
and the cron-grammar gate that rejects an unschedulable expression at write time.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from molecule_runtime import schedule_store
from molecule_runtime.schedule_store import (
    MAX_ENTRIES,
    MAX_PROMPT_BYTES,
    ScheduleError,
    ScheduleStore,
    validate_entry,
    validate_grid,
)


def _fixtures() -> dict:
    text = (
        resources.files("molecule_runtime")
        .joinpath("contracts/schedule.fixtures.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


# ---------------------------------------------------------------------------
# contract fixture partition — the store agrees with the SDK contract
# ---------------------------------------------------------------------------


def test_every_valid_fixture_is_accepted() -> None:
    for case in _fixtures()["valid"]:
        norm = validate_entry(case["entry"])
        assert norm["name"] == case["entry"]["name"]
        assert norm["timezone"]  # defaulted to UTC when absent
        assert isinstance(norm["enabled"], bool)


def test_every_invalid_fixture_is_rejected() -> None:
    for case in _fixtures()["invalid"]:
        with pytest.raises(ScheduleError):
            validate_entry(case["entry"])


# ---------------------------------------------------------------------------
# caps the schema can only approximate — enforced in code
# ---------------------------------------------------------------------------


def test_prompt_byte_cap_is_enforced_on_bytes_not_codepoints() -> None:
    # A multibyte prompt at the code-point limit but over the BYTE cap is rejected.
    over = "é" * (MAX_PROMPT_BYTES // 2 + 1)  # 2 bytes each -> > MAX_PROMPT_BYTES
    assert len(over) <= MAX_PROMPT_BYTES  # would pass a code-point maxLength
    with pytest.raises(ScheduleError, match="bytes"):
        validate_entry({"name": "n", "cron": "0 * * * *", "prompt": over})


def test_entry_count_cap() -> None:
    grid = [
        {"name": f"s{i}", "cron": "0 * * * *", "prompt": "p"}
        for i in range(MAX_ENTRIES + 1)
    ]
    with pytest.raises(ScheduleError, match="too many"):
        validate_grid(grid)


def test_duplicate_names_rejected() -> None:
    grid = [
        {"name": "dup", "cron": "0 * * * *", "prompt": "p"},
        {"name": "dup", "cron": "0 9 * * *", "prompt": "q"},
    ]
    with pytest.raises(ScheduleError, match="duplicate"):
        validate_grid(grid)


def test_unschedulable_cron_rejected_at_write_time() -> None:
    # Structurally 5 fields but out of range -> cron contract rejects it.
    with pytest.raises(ScheduleError, match="cron"):
        validate_entry({"name": "n", "cron": "99 * * * *", "prompt": "p"})


# ---------------------------------------------------------------------------
# CRUD round-trip on a real volume file
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


def test_create_get_update_delete_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.list() == []

    created = store.create({"name": "sweep", "cron": "0 * * * *", "prompt": "go"})
    assert created["enabled"] is True
    assert store.get("sweep")["cron"] == "0 * * * *"

    with pytest.raises(ScheduleError, match="already exists"):
        store.create({"name": "sweep", "cron": "0 9 * * *", "prompt": "again"})

    updated = store.update("sweep", {"enabled": False, "prompt": "paused"})
    assert updated["enabled"] is False
    assert updated["prompt"] == "paused"
    assert store.get("sweep")["cron"] == "0 * * * *"  # untouched field preserved

    with pytest.raises(ScheduleError, match="renaming"):
        store.update("sweep", {"name": "other"})

    assert store.delete("sweep") is True
    assert store.delete("sweep") is False
    assert store.list() == []


def test_update_missing_schedule_raises(tmp_path: Path) -> None:
    with pytest.raises(ScheduleError, match="no such schedule"):
        _store(tmp_path).update("ghost", {"enabled": False})


def test_load_rejects_a_corrupt_grid(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ScheduleError):
        ScheduleStore(path).load()


def test_load_revalidates_persisted_entries(tmp_path: Path) -> None:
    # A grid written out-of-band with an invalid entry must not load silently.
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps({"schedules": [{"name": "n", "cron": "0 * * * *"}]}),
                    encoding="utf-8")
    with pytest.raises(ScheduleError):
        ScheduleStore(path).load()  # missing prompt


def test_replace_all_is_atomic_and_validated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create({"name": "keep", "cron": "0 * * * *", "prompt": "p"})
    with pytest.raises(ScheduleError):
        store.replace_all([{"name": "bad", "cron": "not a cron", "prompt": "p"}])
    # failed replace_all left the prior grid intact (validation before write)
    assert [e["name"] for e in store.list()] == ["keep"]

    store.replace_all([{"name": "a", "cron": "0 * * * *", "prompt": "p"},
                       {"name": "b", "cron": "0 9 * * *", "prompt": "q"}])
    assert sorted(e["name"] for e in store.list()) == ["a", "b"]
