"""Cross-language equivalence gate for the cron contract.

Asserts ``molecule_runtime.cronspec.compute_next_run`` reproduces every row of
the SDK cron contract's fixture set (vendored byte-for-byte at
``molecule_runtime/contracts/cron.fixtures.json``, generated from robfig/cron v3
— the shipping Go scheduler). A drift between this and the Go
``internal/cronspec`` conformance test is a real fire-time bug, not a rounding
nit, so both ends assert the SAME fixtures.

Run:
    python3 -m pytest tests/test_cronspec_contract.py -v
"""
from __future__ import annotations

import datetime as dt
import json
from importlib import resources

import pytest

from molecule_runtime.cronspec import CronError, compute_next_run, validate


def _load_fixtures() -> list[dict]:
    raw = (
        resources.files("molecule_runtime")
        .joinpath("contracts/cron.fixtures.json")
        .read_text(encoding="utf-8")
    )
    data = json.loads(raw)
    assert data, "no fixtures loaded"
    return data


def _iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


FIXTURES = _load_fixtures()


@pytest.mark.parametrize("f", FIXTURES, ids=[f["desc"] for f in FIXTURES])
def test_compute_next_run_conforms_to_contract(f: dict) -> None:
    got = compute_next_run(f["expr"], f["tz"], _iso(f["after"]))
    want = _iso(f["expect"])
    assert got == want, (
        f"{f['desc']}: {f['expr']!r} @ {f['tz']} after {f['after']}\n"
        f"  got  {got.isoformat()}\n  want {want.isoformat()}"
    )


def test_fixture_set_covers_the_sharp_edges() -> None:
    # Guard against a fixture file that silently shrank to only-easy cases: the
    # DST + OR + rollover rows are the whole point of pinning cron behaviour.
    descs = " ".join(f["desc"] for f in FIXTURES)
    for needle in ("spring-forward", "fall-back", "OR semantics", "leap", "half-hour"):
        assert needle in descs, f"fixture set lost its {needle!r} coverage"


def test_next_is_strictly_after() -> None:
    # A schedule sitting exactly on its fire minute returns the NEXT occurrence.
    after = _iso("2026-07-14T09:00:00Z")
    got = compute_next_run("0 9 * * *", "UTC", after)
    assert got == _iso("2026-07-15T09:00:00Z")


def test_validate_rejects_bad_input() -> None:
    validate("*/15 * * * *", "UTC")  # ok
    validate("0 9 * * MON", "America/New_York")  # ok
    for expr in ("* * * *", "0 9 * * 7", "60 * * * *", "0 25 * * *"):
        with pytest.raises(CronError):
            validate(expr, "UTC")
    with pytest.raises(CronError):
        validate("* * * * *", "Mars/Phobos")


def test_negative_control_the_conformance_assertion() -> None:
    # Prove the conformance test isn't vacuous: a deliberately wrong expectation
    # must NOT match what the evaluator returns.
    f = next(x for x in FIXTURES if x["desc"] == "daily 09:00 UTC")
    got = compute_next_run(f["expr"], f["tz"], _iso(f["after"]))
    wrong = _iso(f["expect"]) + dt.timedelta(minutes=1)
    assert got != wrong
