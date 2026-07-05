"""Tests for scripts/merge_runtime_version_bumps.py (the runtime#131 sweeper).

Regression coverage for the two silent no-op modes found 2026-07-05, both of
which let consumer template pins drift 5+ releases while the sweeper cron
reported green:

1. IDENTITY DRIFT — the expected bump-PR author was a hardcoded username
   ("molecule-runtime-release-bot") that went stale when the Infisical
   /shared/runtime-bot credential was consolidated onto `core-devops`; the
   sweeper matched zero PRs. The author is now resolved dynamically as
   whoami(read_token), the identity that opens the PRs by construction.
2. CONTEXT-NAME DRIFT — the required commit-status contexts were exact
   names ("validate-runtime", "t4-conformance") that never matched what the
   template CIs actually emit ("CI / Template validation (runtime)
   (pull_request)", ...); matching is now by substring, anchored on the
   universal wheel-install gate, with the combined state enforcing
   everything else that posts (T4 included, where a template defines it).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "merge_runtime_version_bumps.py"
SPEC = importlib.util.spec_from_file_location("merge_runtime_version_bumps", SCRIPT_PATH)
assert SPEC and SPEC.loader
sweeper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sweeper
SPEC.loader.exec_module(sweeper)


def _pr(author: str) -> dict:
    return {"number": 7, "user": {"login": author}, "head": {"ref": "bump/runtime-0.3.85"}}


def test_bump_pr_accepts_dynamic_opener_identity() -> None:
    """The eligible author is whatever identity run() resolved from the read
    token — here 'core-devops' — not a hardcoded bot username."""
    assert sweeper.is_runtime_bump_pr(
        _pr("core-devops"), [".runtime-version"], "core-devops"
    )
    assert sweeper.is_runtime_bump_pr(
        _pr("core-devops"), [".runtime-version", "requirements.txt"], "core-devops"
    )


def test_bump_pr_rejects_other_authors() -> None:
    assert not sweeper.is_runtime_bump_pr(
        _pr("some-human"), [".runtime-version"], "core-devops"
    )
    # The historical hardcode must NOT be silently trusted when the resolved
    # opener is someone else (that asymmetry was the 2026-07-05 no-op).
    assert not sweeper.is_runtime_bump_pr(
        _pr("molecule-runtime-release-bot"), [".runtime-version"], "core-devops"
    )


def test_bump_pr_rejects_empty_expected_author() -> None:
    """An unresolved opener identity must never widen to 'any author'."""
    assert not sweeper.is_runtime_bump_pr(_pr("core-devops"), [".runtime-version"], "")


def test_bump_pr_rejects_extra_files() -> None:
    assert not sweeper.is_runtime_bump_pr(
        _pr("core-devops"), [".runtime-version", "Dockerfile"], "core-devops"
    )
    assert not sweeper.is_runtime_bump_pr(_pr("core-devops"), [], "core-devops")


def _combined(state: str, rows: list[tuple[str, str]]) -> dict:
    # Mirrors the real Gitea payload shape: combined carries `state`, child
    # rows carry the per-context result in `status` (RC 13421).
    return {
        "state": state,
        "statuses": [{"context": c, "status": s} for c, s in rows],
    }


def test_statuses_success_matches_real_emitted_context_names() -> None:
    """The context names the template CIs actually emit (verified against
    live bump-PR heads on all six consumer templates) must satisfy the
    gate — the old exact names ('validate-runtime') never did."""
    combined = _combined(
        "success",
        [
            ("CI / Template validation (static) (pull_request)", "success"),
            ("CI / Template validation (runtime) (pull_request)", "success"),
            ("CI / T4 tier-4 conformance (live) (pull_request)", "success"),
            ("CI / Adapter unit tests (pull_request)", "success"),
            ("Secret scan / Scan diff for credential-shaped strings (pull_request)", "success"),
        ],
    )
    assert sweeper.all_required_statuses_success(combined)


def test_statuses_success_matches_push_suffixed_contexts_too() -> None:
    """Some templates (hermes/openclaw-style) dual-post (push) + (pull_request)
    rows; either suffix satisfies the substring anchor."""
    combined = _combined(
        "success",
        [("CI / Template validation (runtime) (push)", "success")],
    )
    assert sweeper.all_required_statuses_success(combined)


def test_statuses_reject_when_combined_not_success() -> None:
    combined = _combined(
        "failure",
        [
            ("CI / Template validation (runtime) (pull_request)", "success"),
            ("CI / T4 tier-4 conformance (live) (pull_request)", "failure"),
        ],
    )
    assert not sweeper.all_required_statuses_success(combined)


def test_statuses_reject_when_wheel_install_gate_absent() -> None:
    """Guards the just-pushed window: combined may read success while the
    real gates have not registered their contexts yet."""
    combined = _combined(
        "success",
        [("Secret scan / Scan diff for credential-shaped strings (pull_request)", "success")],
    )
    assert not sweeper.all_required_statuses_success(combined)


def test_statuses_reject_when_gate_posted_but_not_success() -> None:
    combined = _combined(
        "success",  # defensive: even if combined lied, the gate row is pending
        [("CI / Template validation (runtime) (pull_request)", "pending")],
    )
    assert not sweeper.all_required_statuses_success(combined)


def test_whoami_returns_login_or_empty(monkeypatch) -> None:
    client = sweeper.GiteaClient("https://example.invalid", "m-tok", "r-tok", "molecule-ai")

    calls: list[bool] = []

    def fake_request(method, path, body=None, params=None, *, use_merge_token=False):
        calls.append(use_merge_token)
        assert method == "GET" and path == "/api/v1/user"
        return 200, {"login": "devops-engineer" if use_merge_token else "core-devops"}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.whoami() == "core-devops"
    assert client.whoami(use_merge_token=True) == "devops-engineer"
    assert calls == [False, True]

    monkeypatch.setattr(client, "_request", lambda *a, **k: (401, {"message": "unauthorized"}))
    assert client.whoami() == ""
