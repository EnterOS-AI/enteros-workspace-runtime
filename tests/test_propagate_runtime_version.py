from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "propagate_runtime_version.py"
SPEC = importlib.util.spec_from_file_location("propagate_runtime_version", SCRIPT_PATH)
assert SPEC and SPEC.loader
prop = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prop
SPEC.loader.exec_module(prop)


def test_version_tuple_orders_releases() -> None:
    assert prop._version_tuple("0.3.8") < prop._version_tuple("0.3.9")
    assert prop._version_tuple("0.3.10") > prop._version_tuple("0.3.9")
    assert prop._version_tuple("0.2.3") < prop._version_tuple("0.3.0")
    # pre-release suffix is dropped to the numeric core
    assert prop._version_tuple("0.3.9-rc1") == prop._version_tuple("0.3.9")


def _patch_pin(monkeypatch: pytest.MonkeyPatch, pinned: str | None) -> None:
    monkeypatch.setattr(prop, "read_pinned_version", lambda *a, **k: pinned)
    # No token path is exercised in these plan tests, so branch/PR lookups are inert.


def test_plan_behind_consumer_would_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pin(monkeypatch, "0.3.8")
    plan = prop.plan_consumer("tpl", "0.3.9", gitea_url="https://x", token=None)
    assert plan.action == "open-pr"
    assert plan.branch == "bump/runtime-0.3.9"
    assert "0.3.8 -> 0.3.9" in plan.detail


def test_plan_already_pinned_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pin(monkeypatch, "0.3.9")
    plan = prop.plan_consumer("tpl", "0.3.9", gitea_url="https://x", token=None)
    assert plan.action == "already-pinned"


def test_plan_ahead_consumer_not_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pin(monkeypatch, "0.4.0")
    plan = prop.plan_consumer("tpl", "0.3.9", gitea_url="https://x", token=None)
    assert plan.action == "ahead"


def test_plan_missing_pin_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pin(monkeypatch, None)
    plan = prop.plan_consumer("tpl", "0.3.9", gitea_url="https://x", token=None)
    assert plan.action == "no-pin"


def test_plan_existing_branch_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pin(monkeypatch, "0.3.8")
    monkeypatch.setattr(prop, "_branch_exists", lambda *a, **k: True)
    plan = prop.plan_consumer("tpl", "0.3.9", gitea_url="https://x", token="t")
    assert plan.action == "pr-exists"


def test_plan_existing_open_pr_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pin(monkeypatch, "0.3.8")
    monkeypatch.setattr(prop, "_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(prop, "_open_pr_for_branch", lambda *a, **k: "https://x/pulls/7")
    plan = prop.plan_consumer("tpl", "0.3.9", gitea_url="https://x", token="t")
    assert plan.action == "pr-exists"
    assert "pulls/7" in plan.detail


def test_main_report_only_without_token_does_not_fail(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """#83 root cause: a missing DISPATCH_TOKEN must NOT fail the job."""
    monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
    monkeypatch.setattr(prop, "read_pinned_version", lambda *a, **k: "0.3.8")
    rc = prop.main(["--version", "0.3.9", "--repo", "tpl-a", "--repo", "tpl-b"])
    out = capsys.readouterr()
    assert rc == 0
    assert "report-only mode" in out.err
    assert "WOULD open PR" in out.out


def test_main_dry_run_reports_plan(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(prop, "read_pinned_version", lambda *a, **k: "0.3.8")
    rc = prop.main(["--version", "0.3.9", "--repo", "tpl-a", "--dry-run"])
    out = capsys.readouterr()
    assert rc == 0
    assert "WOULD open PR" in out.out
    assert "dry_run=True" in out.out
