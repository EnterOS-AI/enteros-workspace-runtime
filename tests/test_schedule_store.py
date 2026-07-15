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


# ---------------------------------------------------------------------------
# upsert_template — additive, edit-preserving reconcile-on-boot seeding
# ---------------------------------------------------------------------------


def test_upsert_template_seeds_empty_grid_stamping_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_template([
        {"name": "a", "cron": "0 * * * *", "prompt": "p"},
        {"name": "b", "cron": "0 9 * * *", "prompt": "q"},
    ])
    got = {e["name"]: e for e in store.list()}
    assert set(got) == {"a", "b"}
    assert got["a"]["source"] == "template" and got["b"]["source"] == "template"


def test_upsert_template_preserves_runtime_edits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A user-created (source='runtime') entry, plus a template-owned one.
    store.create({"name": "user", "cron": "0 * * * *", "prompt": "mine", "source": "runtime"})
    store.upsert_template([{"name": "tmpl", "cron": "0 9 * * *", "prompt": "seed"}])

    # Re-provision: template ships a DIFFERENT prompt for both names, incl. one
    # that collides with the user's own entry name.
    store.upsert_template([
        {"name": "tmpl", "cron": "0 9 * * *", "prompt": "seed-v2"},
        {"name": "user", "cron": "0 0 * * *", "prompt": "TEMPLATE-CLOBBER"},
    ])
    got = {e["name"]: e for e in store.list()}
    # user entry is source='runtime' → PRESERVED untouched (not clobbered)
    assert got["user"]["prompt"] == "mine"
    assert got["user"]["cron"] == "0 * * * *"
    assert got["user"]["source"] == "runtime"
    # template-owned entry → refreshed to the new definition
    assert got["tmpl"]["prompt"] == "seed-v2"
    assert got["tmpl"]["source"] == "template"


def test_upsert_template_is_additive_not_pruning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_template([{"name": "a", "cron": "0 * * * *", "prompt": "p"},
                           {"name": "b", "cron": "0 9 * * *", "prompt": "q"}])
    # A later delivery drops "b" — additive semantics keep the prior "b".
    store.upsert_template([{"name": "a", "cron": "0 * * * *", "prompt": "p"}])
    assert sorted(e["name"] for e in store.list()) == ["a", "b"]


def test_upsert_template_bad_entry_leaves_grid_intact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_template([{"name": "keep", "cron": "0 * * * *", "prompt": "p"}])
    with pytest.raises(ScheduleError):
        store.upsert_template([{"name": "bad", "cron": "not a cron", "prompt": "p"}])
    assert [e["name"] for e in store.list()] == ["keep"]  # atomic: prior grid intact


# ---------------------------------------------------------------------------
# source-stamping (finding #1) — API create/update take ownership
# ---------------------------------------------------------------------------


def test_create_stamps_source_runtime(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create({"name": "u", "cron": "0 * * * *", "prompt": "p"})
    assert created["source"] == "runtime"


def test_editing_a_template_schedule_survives_reseed(tmp_path: Path) -> None:
    # This is the finding #1 regression: a user edit via update() must not be
    # reverted by the next reconcile-on-boot seeding.
    store = _store(tmp_path)
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    assert store.get("nightly")["source"] == "template"

    # User edits the template-seeded schedule -> it takes ownership.
    edited = store.update("nightly", {"cron": "0 6 * * *"})
    assert edited["source"] == "runtime"

    # Re-provision ships the original template definition again.
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    got = store.get("nightly")
    assert got["cron"] == "0 6 * * *"  # user edit preserved, NOT reverted
    assert got["source"] == "runtime"


# ---------------------------------------------------------------------------
# tombstones (finding #2) — user-deleted template schedules stay deleted
# ---------------------------------------------------------------------------


def test_deleted_template_schedule_is_not_resurrected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    assert store.delete("nightly") is True

    # Re-provision must NOT bring the deleted template schedule back.
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    assert store.get("nightly") is None
    assert [e["name"] for e in store.list()] == []


def test_recreating_a_deleted_name_clears_its_tombstone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    store.delete("nightly")
    # User explicitly recreates it -> tombstone cleared, future reseeds allowed.
    store.create({"name": "nightly", "cron": "0 9 * * *", "prompt": "mine"})
    store.upsert_template([{"name": "nightly", "cron": "0 3 * * *", "prompt": "seed"}])
    got = store.get("nightly")
    assert got is not None and got["source"] == "runtime" and got["cron"] == "0 9 * * *"


# ---------------------------------------------------------------------------
# malformed YAML (finding #3) — parsed into ScheduleError, not a raw YAMLError
# ---------------------------------------------------------------------------


def test_load_wraps_malformed_yaml_as_schedule_error(tmp_path: Path) -> None:
    path = tmp_path / "schedules.yaml"
    path.write_text("schedules:\n  - name: x\n   bad: indent\n", encoding="utf-8")
    with pytest.raises(ScheduleError):
        ScheduleStore(path).load()
