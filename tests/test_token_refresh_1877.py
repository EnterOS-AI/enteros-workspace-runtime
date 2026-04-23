"""Tests for #1877 fix — runtime re-reads /configs/.auth_token on 401.

Covers two surfaces:

1. ``platform_auth.refresh_from_disk()`` — pure helper that clears the
   in-memory cache and re-reads the file.
2. The HeartbeatLoop 401-then-retry pattern (verified by replaying it
   against an httpx MockTransport).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# WORKSPACE_ID must be set BEFORE importing platform_auth — the module
# validates the env var at import time.
os.environ.setdefault("WORKSPACE_ID", "00000000-0000-0000-0000-000000000001")

import httpx
import pytest

import molecule_runtime.platform_auth as pa
from molecule_runtime.platform_auth import (
    auth_headers,
    clear_cache,
    get_token,
    refresh_from_disk,
    save_token,
)


# ---------- platform_auth.refresh_from_disk ----------


def test_refresh_picks_up_rotated_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    clear_cache()

    save_token("token-v1")
    assert get_token() == "token-v1"

    # Simulate platform rotating the token on disk while runtime had it cached
    (tmp_path / ".auth_token").write_text("token-v2")
    assert auth_headers().get("Authorization") == "Bearer token-v1"  # cache stale

    fresh = refresh_from_disk()
    assert fresh == "token-v2"
    assert auth_headers().get("Authorization") == "Bearer token-v2"


def test_refresh_returns_none_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    clear_cache()
    assert refresh_from_disk() is None
    assert "Authorization" not in auth_headers()


def test_refresh_clears_stale_cache_when_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    clear_cache()
    save_token("token-v1")
    assert get_token() == "token-v1"

    (tmp_path / ".auth_token").unlink()
    assert refresh_from_disk() is None


def test_refresh_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    clear_cache()
    (tmp_path / ".auth_token").write_text("stable-token")

    a = refresh_from_disk()
    b = refresh_from_disk()
    assert a == b == "stable-token"


# ---------- 401 retry pattern (replayed manually against MockTransport) ----------


def test_401_retry_pattern_uses_refreshed_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Models the #1877 fix path: 401 -> refresh_from_disk -> retry succeeds.

    Uses httpx sync Client + MockTransport so the test doesn't require
    pytest-asyncio in CI (the production code is async, but the retry
    *logic* — refresh-on-401 — is identical sync or async).
    """
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    clear_cache()

    save_token("token-v1")
    (tmp_path / ".auth_token").write_text("token-v2")
    pa._cached_token = "token-v1"  # explicit stale cache

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"auth": request.headers.get("authorization", "")})
        if "token-v1" in request.headers.get("authorization", ""):
            return httpx.Response(401, json={"error": "invalid token"})
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0) as client:
        payload = {"workspace_id": "ws-test", "active_tasks": 0}
        url = "http://platform:8080/registry/heartbeat"

        # Mirror exactly what heartbeat.py now does:
        resp = client.post(url, json=payload, headers=auth_headers())
        if resp.status_code == 401 and refresh_from_disk() is not None:
            resp = client.post(url, json=payload, headers=auth_headers())

        assert resp.status_code == 200
        assert len(calls) == 2
        assert calls[0]["auth"] == "Bearer token-v1"  # stale, rejected
        assert calls[1]["auth"] == "Bearer token-v2"  # fresh, accepted


def test_401_retry_no_loop_when_disk_token_also_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If both cached AND disk tokens are stale, the retry uses the same value
    as the original — and the loop must NOT retry forever. The production code
    only retries ONCE."""
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
    clear_cache()

    save_token("token-everywhere-stale")  # disk + cache match, both invalid

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("authorization", ""))
        return httpx.Response(401, json={"error": "invalid token"})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0) as client:
        payload = {"workspace_id": "ws-test"}
        url = "http://platform:8080/registry/heartbeat"

        resp = client.post(url, json=payload, headers=auth_headers())
        if resp.status_code == 401 and refresh_from_disk() is not None:
            resp = client.post(url, json=payload, headers=auth_headers())

        # Both attempts 401, no third call — bounded retry budget
        assert resp.status_code == 401
        assert len(calls) == 2
