"""Vendored idle-prompt contract instance + Policy loading (task #219, PR-4).

Guards that (a) Policy.default loads the operator-ruled values from the vendored
contract instance, (b) the hardcoded Policy() mirror never silently drifts from
that instance, (c) env overrides still win, and (d) a missing instance
loud-degrades to the mirror instead of crashing.
"""
from __future__ import annotations

import json
from importlib import resources

import pytest

from molecule_runtime.idle_digest import contract as ct
from molecule_runtime.idle_digest.contract import Policy


def _vendored_instance() -> dict:
    raw = (
        resources.files("molecule_runtime")
        .joinpath("contracts/idle-prompt.contract.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def test_vendored_instance_ships_and_parses():
    data = _vendored_instance()
    assert data["schema_version"] == "idle-prompt/v1"
    assert data["contract_layer"] == "idle-prompt"


def test_policy_loads_from_vendored_instance():
    data = _vendored_instance()
    p = Policy.from_contract_instance()
    assert p is not None
    assert p.idle_fire_after_seconds == data["wake"]["idle_fire_after_seconds"]
    assert p.max_summary_bytes == data["limits"]["max_summary_bytes"]
    assert p.max_digest_bytes == data["limits"]["max_digest_bytes"]
    assert p.provider_timeout_seconds == data["failure"]["provider_timeout_seconds"]
    assert p.max_consecutive_failures == data["failure"]["max_consecutive_failures"]
    assert p.third_party_default_tier == data["trust"]["third_party_default_tier"]
    assert list(p.stale_escalation_thresholds_seconds) == data["delta"][
        "stale_escalation_thresholds_seconds"
    ]


def test_hardcoded_mirror_matches_vendored_instance():
    """The hardcoded Policy() defaults are the fail-safe fallback and MUST equal
    the vendored instance values — otherwise a missing-instance fallback would
    silently apply different production values than the SSOT."""
    from_instance = Policy.from_contract_instance()
    mirror = Policy()
    assert from_instance == mirror, (
        "hardcoded Policy() mirror drifted from the vendored contract instance; "
        "update the Policy field defaults to match idle-prompt.contract.json"
    )


def test_default_uses_instance_values_by_default(monkeypatch):
    # An ambient MOLECULE_IDLE_FIRE_SECONDS (dev shell, canary env) must not
    # bleed into the vendored-default assertion.
    monkeypatch.delenv("MOLECULE_IDLE_FIRE_SECONDS", raising=False)
    p = Policy.default()
    assert p.idle_fire_after_seconds == 300  # the ruled production value


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("MOLECULE_IDLE_FIRE_SECONDS", "600")
    monkeypatch.setenv("MOLECULE_IDLE_MAX_SUMMARY_BYTES", "128")
    p = Policy.default()
    assert p.idle_fire_after_seconds == 600
    assert p.max_summary_bytes == 128


def test_missing_instance_degrades_to_mirror(monkeypatch, caplog):
    """A missing/unreadable vendored instance logs once and falls back to the
    hardcoded mirror — never crashes."""
    monkeypatch.setattr(ct, "_contract_load_failed_logged", False)
    monkeypatch.setattr(
        ct, "_CONTRACT_INSTANCE_RESOURCE", "contracts/does-not-exist.json"
    )
    with caplog.at_level("ERROR"):
        p = Policy.from_contract_instance()
    assert p is None
    assert any("vendored contract instance" in r.message for r in caplog.records)
    # default() still yields a usable policy (the mirror) with no exception
    p2 = Policy.default()
    assert p2.idle_fire_after_seconds == 300


def test_missing_instance_logs_only_once(monkeypatch, caplog):
    monkeypatch.setattr(ct, "_contract_load_failed_logged", False)
    monkeypatch.setattr(ct, "_CONTRACT_INSTANCE_RESOURCE", "contracts/nope.json")
    with caplog.at_level("ERROR"):
        Policy.from_contract_instance()
        Policy.from_contract_instance()
    errors = [r for r in caplog.records if "vendored contract instance" in r.message]
    assert len(errors) == 1


def test_vendored_schema_validates_vendored_instance():
    """Offline: the vendored schema accepts the vendored instance (they are a
    matched byte-for-byte pair from sdk main)."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("molecule_runtime")
        .joinpath("contracts/idle-prompt.schema.json")
        .read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = list(
        jsonschema.Draft202012Validator(schema).iter_errors(_vendored_instance())
    )
    assert not errors, [e.message for e in errors]
