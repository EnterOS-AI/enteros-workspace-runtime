#!/usr/bin/env python3
"""Tests for propagate_runtime_version.py (runtime#91 dual-pin prevention).

Verifies that templates carrying BOTH .runtime-version AND a
molecule-ai-workspace-runtime== pin in requirements.txt get BOTH files bumped
atomically, preventing publish-image cross-check failures.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

# Import the module under test from the sibling scripts directory.
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "scripts"))
import propagate_runtime_version as prv


def test_update_requirements_content_bumps_runtime_pin():
    """A requirements.txt with a runtime pin is rewritten to the target version."""
    original = (
        "# runtime adapter\n"
        "molecule-ai-workspace-runtime==0.3.23\n"
        "a2a-sdk==1.0.3\n"
    )
    updated = prv._update_requirements_content(original, "0.3.26")
    assert updated is not None
    assert "molecule-ai-workspace-runtime==0.3.26" in updated
    assert "a2a-sdk==1.0.3" in updated
    # Only the runtime line changed.
    assert updated.splitlines() == [
        "# runtime adapter",
        "molecule-ai-workspace-runtime==0.3.26",
        "a2a-sdk==1.0.3",
    ]


def test_update_requirements_content_noop_when_no_runtime_pin():
    """A requirements.txt without a runtime pin is left untouched."""
    original = "a2a-sdk==1.0.3\npython-multipart>=0.0.27\n"
    assert prv._update_requirements_content(original, "0.3.26") is None


def test_plan_consumer_reports_requirements_pin(monkeypatch):
    """plan_consumer detects a dual-pin template and reports the requirements pin."""
    calls: list[tuple[str, str]] = []

    def fake_http(url: str, *, token=None, method="GET", payload=None, timeout=30):
        # Dispatch based on the requested path/URL.
        if url.endswith("/raw/.runtime-version"):
            calls.append(("runtime", url))
            return 200, "0.3.23\n"
        if url.endswith("/raw/requirements.txt"):
            calls.append(("requirements", url))
            return 200, "molecule-ai-workspace-runtime==0.3.23\na2a-sdk==1.0.3\n"
        if "/branches/" in url:
            return 404, "{}"
        if "/pulls?state=open" in url:
            return 200, "[]"
        return 404, "{}"

    monkeypatch.setattr(prv, "_http", fake_http)

    plan = prv.plan_consumer("molecule-ai-workspace-template-codex", "0.3.26", gitea_url="https://example.com", token="tok")
    assert plan.action == "open-pr"
    assert plan.pinned == "0.3.23"
    assert plan.req_pin == "0.3.23"
    assert "requirements.txt pin 0.3.23 -> 0.3.26" in plan.detail


def test_open_bump_pr_commits_both_files(monkeypatch):
    """open_bump_pr commits .runtime-version and requirements.txt on the same branch."""
    committed: dict[str, tuple[str, str, str]] = {}  # path -> (branch, create_branch, decoded_content)

    def fake_http(url: str, *, token=None, method="GET", payload=None, timeout=30):
        if method == "GET":
            if url.endswith("/repos/molecule-ai/test-repo"):
                return 200, '{"default_branch": "main"}'
            if "/contents/.runtime-version" in url:
                return 200, '{"sha": "runtime-sha"}'
            if "/contents/requirements.txt" in url:
                return 200, '{"sha": "req-sha"}'
            if url.endswith("/raw/requirements.txt?ref=main"):
                return 200, "molecule-ai-workspace-runtime==0.3.23\na2a-sdk==1.0.3\n"
            if "/branches/" in url:
                return 404, "{}"
            if "/pulls?state=open" in url:
                return 200, "[]"
            return 404, "{}"

        if method == "PUT" and "/contents/" in url:
            path = url.split("/contents/")[-1]
            decoded = base64.b64decode(payload["content"]).decode()
            committed[path] = (
                payload.get("new_branch", payload["branch"]),
                "new_branch" in payload,
                decoded,
            )
            return 201, '{"content": {"path": "' + path + '"}}'

        if method == "POST" and url.endswith("/pulls"):
            return 201, '{"html_url": "https://example.com/pulls/42", "number": 42}'

        return 404, "{}"

    monkeypatch.setattr(prv, "_http", fake_http)

    plan = prv.ConsumerPlan(
        repo="test-repo",
        pinned="0.3.23",
        action="open-pr",
        branch="bump/runtime-0.3.26",
        detail="test",
        req_pin="0.3.23",
    )
    url = prv.open_bump_pr(plan, "0.3.26", gitea_url="https://git.example.com", token="tok")
    assert url == "https://example.com/pulls/42"

    # .runtime-version creates the branch.
    assert ".runtime-version" in committed
    branch, created, content = committed[".runtime-version"]
    assert created is True
    assert content == "0.3.26\n"
    assert branch == "bump/runtime-0.3.26"

    # requirements.txt is committed to the same branch, not creating a new one.
    assert "requirements.txt" in committed
    branch2, created2, content2 = committed["requirements.txt"]
    assert created2 is False
    assert branch2 == "bump/runtime-0.3.26"
    assert "molecule-ai-workspace-runtime==0.3.26" in content2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
