from __future__ import annotations

import base64
import importlib.util
import json
import sys
import urllib.error
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
    # plan_consumer now also reads requirements.txt for dual-pin templates; keep
    # these plan tests isolated from the network.
    monkeypatch.setattr(prop, "read_requirements_pin", lambda *a, **k: None)
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


def test_open_bump_pr_contents_payload_uses_default_branch_as_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """CP#752/RCA: creating a NEW bump branch requires branch=default (source) and
    new_branch=plan.branch. Using plan.branch for both makes Gitea 404 because the
    bump branch does not exist yet."""
    calls: list[tuple[str, str, dict]] = []

    def fake_http(url: str, *, token=None, method: str = "GET", payload=None, timeout=30):
        calls.append((method, url, payload or {}))
        if method == "GET" and "/repos/molecule-ai/tpl" in url and url.endswith("/tpl"):
            return 200, json.dumps({"default_branch": "staging"})
        if method == "GET" and "/contents/.runtime-version" in url:
            return 200, json.dumps({"sha": "abc123"})
        if method == "PUT" and "/contents/.runtime-version" in url:
            return 201, json.dumps({"content": {"html_url": "https://x/tpl/blob/bump/runtime-0.3.9/.runtime-version"}})
        if method == "POST" and "/pulls" in url:
            return 201, json.dumps({"html_url": "https://x/pulls/42"})
        return 404, "{}"

    monkeypatch.setattr(prop, "_http", fake_http)
    plan = prop.ConsumerPlan(
        repo="tpl",
        pinned="0.3.8",
        action="open-pr",
        branch="bump/runtime-0.3.9",
        detail="would bump 0.3.8 -> 0.3.9",
    )
    url = prop.open_bump_pr(plan, "0.3.9", gitea_url="https://x", token="t")
    assert url == "https://x/pulls/42"

    put_calls = [c for c in calls if c[0] == "PUT" and "/contents/.runtime-version" in c[1]]
    assert len(put_calls) == 1
    payload = put_calls[0][2]
    assert payload["branch"] == "staging", f"expected source branch 'staging', got {payload['branch']!r}"
    assert payload["new_branch"] == "bump/runtime-0.3.9", f"expected new branch 'bump/runtime-0.3.9', got {payload['new_branch']!r}"
    assert payload["sha"] == "abc123"
    decoded = base64.b64decode(payload["content"]).decode()
    assert decoded == "0.3.9\n"


# --- runtime#52: bounded retry/backoff on transient PR POST failures ---------


def _fake_http_sequence(responses: list):
    """Build a fake ``_http`` that walks ``responses`` one entry per call.

    Each entry is either a ``(status, body)`` tuple or an Exception instance
    to raise (simulating a connection / timeout error). Once the list is
    exhausted, subsequent calls return a sentinel so the test can assert it
    was called the right number of times.
    """
    call_count = {"n": 0}

    def fake(url, *, token=None, method="GET", payload=None, timeout=30):
        n = call_count["n"]
        call_count["n"] += 1
        if n < len(responses):
            entry = responses[n]
            if isinstance(entry, Exception):
                raise entry
            return entry
        # Test mistake: ran out of responses. Make it loud.
        raise AssertionError(
            f"_http called {n + 1} times; only {len(responses)} responses scripted"
        )

    return fake, call_count


def test_http_with_retry_succeeds_on_first_5xx_then_201(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single 503 followed by a 201 must succeed; backoff sleeps 1 attempt."""
    sleeps: list[float] = []
    fake, call_count = _fake_http_sequence([(503, "transient"), (201, '{"html_url": "x"}')])
    monkeypatch.setattr(prop, "_http", fake)
    status, body = prop._http_with_retry(
        "https://x/api", method="POST", payload={}, sleep=sleeps.append
    )
    assert status == 201
    assert call_count["n"] == 2
    # First failure slept 2**0 = 1s; second succeeded → no more sleep.
    assert sleeps == [1.0]


def test_http_with_retry_does_not_retry_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client-side errors (4xx) are returned immediately; retry would be wasted."""
    sleeps: list[float] = []
    fake, call_count = _fake_http_sequence([(422, "validation failed")])
    monkeypatch.setattr(prop, "_http", fake)
    status, body = prop._http_with_retry(
        "https://x/api", method="POST", payload={}, sleep=sleeps.append
    )
    assert status == 422
    assert call_count["n"] == 1, "must NOT retry on 4xx"
    assert sleeps == []


def test_http_with_retry_exhausts_and_returns_5xx_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent 5xx: after the last attempt returns the final 5xx, the
    helper returns it (NOT raises) so the caller's existing error path sees
    a normal (status, body) tuple. The whole point of bounded retry is that
    the failure is still observable, not swallowed."""
    sleeps: list[float] = []
    fake, call_count = _fake_http_sequence([(503, "x")] * 4)  # initial + 3 retries
    monkeypatch.setattr(prop, "_http", fake)
    status, body = prop._http_with_retry(
        "https://x/api", method="POST", payload={}, max_retries=3, sleep=sleeps.append
    )
    assert status == 503
    assert body == "x"
    assert call_count["n"] == 4, "expected 1 initial + 3 retries = 4 calls"
    # Backoff doubles each retry: 1, 2, 4 (then no sleep on the final attempt).
    assert sleeps == [1.0, 2.0, 4.0]


def test_http_with_retry_raises_on_persistent_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent URLError: the final attempt re-raises so the caller's
    error handler can decide. Intermediate attempts get a backoff sleep."""
    sleeps: list[float] = []
    fake, call_count = _fake_http_sequence(
        [urllib.error.URLError("net down")] * 4
    )
    monkeypatch.setattr(prop, "_http", fake)
    with pytest.raises(urllib.error.URLError):
        prop._http_with_retry(
            "https://x/api", method="POST", payload={}, max_retries=3, sleep=sleeps.append
        )
    assert call_count["n"] == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_http_with_retry_exponential_backoff_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backoff schedule is 2**attempt seconds between attempts (attempt 0 → 1s,
    1 → 2s, 2 → 4s, 3 → 8s, etc.). Tested via the explicit sleep list captured
    from a real retry storm — guards against off-by-one or wrong-base bugs."""
    sleeps: list[float] = []
    fake, _ = _fake_http_sequence([(502, "x")] * 6)  # exhaust
    monkeypatch.setattr(prop, "_http", fake)
    prop._http_with_retry(
        "https://x/api", method="POST", payload={}, max_retries=5, sleep=sleeps.append
    )
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_http_with_retry_returns_immediately_on_first_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path must not incur any backoff sleeps (operators don't pay
    for retry they didn't need)."""
    sleeps: list[float] = []
    fake, call_count = _fake_http_sequence([(201, "ok")])
    monkeypatch.setattr(prop, "_http", fake)
    status, body = prop._http_with_retry(
        "https://x/api", method="POST", payload={}, sleep=sleeps.append
    )
    assert status == 201
    assert call_count["n"] == 1
    assert sleeps == []


def test_open_bump_pr_uses_http_with_retry_for_pr_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR POST must be the only operation wrapped in retry (the audit's
    explicit ask). Pre-POST operations keep their single-shot semantics
    so a real client-side error (e.g. 422 on contents PUT) surfaces
    immediately rather than being masked by 3 retries.

    We exercise the REAL ``_http_with_retry`` (which loops over ``_http``)
    by patching only ``_http``. The pre-POST operations (default-branch
    read, contents read, contents PUT) all hit ``_http`` directly with
    their normal single-shot contract, so the test also confirms the
    PR POST is the ONLY operation that goes through the retry helper —
    pre-POST operations never incur a retry sleep.
    """
    all_calls: list[tuple[str, str]] = []  # every HTTP call
    sleeps: list[float] = []  # backoff sleeps (should be exactly 1: between 503 and 201)
    pr_post_attempts: list[int] = []

    def fake_http(url, *, token=None, method="GET", payload=None, timeout=30):
        all_calls.append((method, url))
        if method == "GET" and url.endswith("/tpl"):
            return 200, json.dumps({"default_branch": "staging"})
        if method == "GET" and "/contents/.runtime-version" in url:
            return 200, json.dumps({"sha": "abc"})
        if method == "PUT" and "/contents/.runtime-version" in url:
            return 201, json.dumps({"content": {"html_url": "x"}})
        if method == "POST" and "/pulls" in url:
            pr_post_attempts.append(1)
            # First attempt 503 (retriable), second 201.
            if len(pr_post_attempts) == 1:
                return 503, "transient"
            return 201, json.dumps({"html_url": "https://x/pulls/42"})
        return 404, "{}"

    # Patch only _http — let the real _http_with_retry do its loop.
    monkeypatch.setattr(prop, "_http", fake_http)
    # And capture the backoff sleep without actually sleeping.
    monkeypatch.setattr(prop.time, "sleep", sleeps.append)

    plan = prop.ConsumerPlan(
        repo="tpl", pinned="0.3.8", action="open-pr",
        branch="bump/runtime-0.3.9", detail="would bump 0.3.8 -> 0.3.9",
    )
    url = prop.open_bump_pr(plan, "0.3.9", gitea_url="https://x", token="t")
    assert url == "https://x/pulls/42"
    # PR POST happened twice (503 → 201) — exactly one retry.
    assert len(pr_post_attempts) == 2, (
        f"expected PR POST to retry once (1 initial + 1 retry), got "
        f"{len(pr_post_attempts)} calls"
    )
    # Exactly one backoff sleep: between the 503 and the 201.
    assert sleeps == [1.0], (
        f"expected exactly one 1s backoff sleep between 503 and 201, got {sleeps}"
    )
    # And the retry was the *last* call (not interleaved with anything).
    assert all_calls[-1] == ("POST", "https://x/api/v1/repos/molecule-ai/tpl/pulls")
    # Pre-POST operations: the first 3 calls are GET default-branch, GET
    # contents, PUT contents. None of them should be retried (each appears
    # exactly once). Exact URLs vary by requirements-pin branch, so check
    # the (method, …) shape rather than enumerating query strings.
    pre_post_calls = all_calls[:3]
    assert len(pre_post_calls) == 3, (
        f"pre-POST should be 3 calls, got {len(pre_post_calls)}: {pre_post_calls}"
    )
    assert pre_post_calls[0][0] == "GET", pre_post_calls[0]
    assert pre_post_calls[1][0] == "GET", pre_post_calls[1]
    assert pre_post_calls[2][0] == "PUT", pre_post_calls[2]
    # And none of the pre-POST calls was retried (each appears exactly once).
    assert len(set(pre_post_calls)) == 3, (
        f"pre-POST calls were retried (not expected): {pre_post_calls}"
    )


def test_requirements_pin_regex_matches_canonical_dist_name() -> None:
    """RC 2026-07-05: the dist rename (`molecule-ai-workspace-runtime` ->
    `molecules-workspace-runtime`, dependency-confusion fix) left the
    requirements-pin regex matching only the LEGACY name, so dual-pin
    templates (e.g. codex) received .runtime-version-only bumps
    while requirements.txt stayed frozen on registry-purged versions and
    their template validation failed on 'No matching distribution found'.
    The regex must bump the canonical name."""
    content = (
        "# Molecule AI workspace runtime\n"
        "molecules-workspace-runtime==0.3.70\n"
        "a2a-sdk==1.0.3\n"
    )
    updated = prop._update_requirements_content(content, "0.3.85")
    assert updated is not None, "canonical dist-name pin not recognized"
    assert "molecules-workspace-runtime==0.3.85" in updated
    assert "a2a-sdk==1.0.3" in updated, "unrelated pins must be untouched"


def test_requirements_pin_regex_still_matches_legacy_dist_name() -> None:
    """Straggler templates still on the pre-rename name keep receiving
    atomic dual-pin bumps."""
    content = "molecule-ai-workspace-runtime==0.3.26\n"
    updated = prop._update_requirements_content(content, "0.3.85")
    assert updated is not None
    assert updated.strip() == "molecule-ai-workspace-runtime==0.3.85"


def test_requirements_pin_regex_returns_none_when_no_pin() -> None:
    """No runtime pin present -> None (nothing to update), unchanged from
    the historical contract."""
    assert prop._update_requirements_content("a2a-sdk==1.0.3\n", "0.3.85") is None
