"""Regression tests for issue #51.

The `[A2A_ERROR]` prefix from ``send_a2a_message`` must always carry a
diagnostic suffix. Before #51 an exception whose ``str(e)`` was empty
(bare ``TimeoutError()``, ``BrokenPipeError()``, several httpx transport
errors) produced ``"[A2A_ERROR] "`` with a trailing space and zero
context, masking the real cause of peer-delegation failures.
"""

from __future__ import annotations

import os
import sys

import pytest

# Set WORKSPACE_ID before importing molecule_runtime modules — platform_auth
# evaluates it at import time and refuses to load otherwise.
os.environ.setdefault("WORKSPACE_ID", "test-workspace")

from molecule_runtime.a2a_client import _A2A_ERROR_PREFIX  # noqa: E402
from molecule_runtime import a2a_client  # noqa: E402


class _BareException(Exception):
    """Exception whose str() is empty — mimics bare TimeoutError()."""

    def __str__(self) -> str:  # noqa: D401
        return ""


class _StubAsyncClient:
    """Async context manager that raises a supplied exception on .post()."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def post(self, *_args, **_kwargs):
        raise self._exc


@pytest.mark.asyncio
async def test_bare_exception_yields_class_name(monkeypatch):
    """When str(e) is empty the result must still include the exception class."""

    def _factory(*_a, **_kw):
        return _StubAsyncClient(_BareException())

    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(a2a_client, "PLATFORM_URL", "http://stub")
    monkeypatch.setattr(a2a_client, "auth_headers", lambda: {})

    result = await a2a_client.send_a2a_message("peer-ws-id", "hi")
    assert result.startswith(_A2A_ERROR_PREFIX)
    suffix = result[len(_A2A_ERROR_PREFIX):]
    assert suffix.strip() != "", f"expected non-empty suffix, got {result!r}"
    assert "BareException" in suffix


@pytest.mark.asyncio
async def test_exception_with_message_passes_through(monkeypatch):
    """Regular exception messages are preserved."""

    def _factory(*_a, **_kw):
        return _StubAsyncClient(RuntimeError("upstream 429"))

    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(a2a_client, "PLATFORM_URL", "http://stub")
    monkeypatch.setattr(a2a_client, "auth_headers", lambda: {})

    result = await a2a_client.send_a2a_message("peer-ws-id", "hi")
    assert result == f"{_A2A_ERROR_PREFIX}upstream 429"
