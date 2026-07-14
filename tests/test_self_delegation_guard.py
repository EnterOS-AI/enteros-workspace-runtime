"""Regression tests for task #190 — self-delegation echo bug.

Symptom (pre-fix): when a workspace's agent called ``delegate_task`` with
its OWN workspace_id, the request was dispatched to the platform's A2A
proxy. The receiving end was the same workspace, whose ``_run_lock`` was
already held by the sending turn, so the request blocked until the 600s
``message/send`` ceiling fired. The platform then surfaced the timeout
in the workspace's own inbox as a fresh row — and because the legacy
inbox classifier mapped any ``peer_id``-bearing row to
``kind=peer_agent``, the timeout error came back to the agent **as if a
peer was instructing it**. The agent obediently replied… by calling
``delegate_task`` to itself again, producing the #190 loop.

The fix is fail-closed at every entry point that could schedule the
dispatch:

  * ``molecule_runtime.a2a_tools_delegation.tool_delegate_task`` — sync
    MCP/external-runtime path. Computes ``effective_src`` the same way
    routing does, then rejects when ``workspace_id == effective_src``.
  * ``molecule_runtime.a2a_tools_delegation.tool_delegate_task_async`` —
    async MCP path. Same check before scheduling the background task.
  * ``molecule_runtime.builtin_tools.delegation.delegate_task_async`` —
    LangChain ``@tool`` path used by the in-container runtime. Rejects
    before ``asyncio.create_task(_execute_delegation(...))``.
  * ``molecule_runtime.builtin_tools.a2a_tools.delegate_task`` —
    framework-agnostic helper.

These tests assert each guard:

  1. Returns an actionable rejection string (or ``success=False`` dict)
     mentioning self-delegation.
  2. Does so synchronously — no HTTP dispatch, no background task
     scheduled, and well under 100ms (the original timeout was 30-600s,
     so a fast-fail is the key correctness property).
  3. ``handle_tool_call`` in the MCP server propagates the rejection
     verbatim (no swallowing into a generic error).

Upstream a2a-protocol spec is silent on self-addressed messages
(checked 2026-05-20 against https://a2a-protocol.org/latest/specification/),
so the rejection is a Molecule-side policy decision, not a spec
violation.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))


_SELF_WS = "00000000-0000-0000-0000-000000000190"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Pin WORKSPACE_ID + PLATFORM_URL and clear per-workspace caches so
    tests don't leak peer→source mappings between cases.

    Subtle: ``a2a_client`` reads ``WORKSPACE_ID`` at import time and
    other modules ``from-import`` it as a module-level constant. When
    these tests run as part of the full suite, ``a2a_client`` is
    already imported (with conftest's sentinel) by the time our env
    var is set, so just calling ``monkeypatch.setenv`` is NOT enough.
    We patch the resolved ``WORKSPACE_ID`` binding on every module
    that consults it for the self-delegation guard.
    """
    monkeypatch.setenv("WORKSPACE_ID", _SELF_WS)
    monkeypatch.setenv("PLATFORM_URL", "http://test-platform")

    import molecule_runtime.platform_auth as platform_auth
    platform_auth.clear_cache()

    import molecule_runtime.a2a_client as a2a_client
    import molecule_runtime.a2a_tools_delegation as a2a_tools_delegation
    import molecule_runtime.builtin_tools.delegation as builtin_delegation
    import molecule_runtime.builtin_tools.a2a_tools as builtin_a2a_tools

    monkeypatch.setattr(a2a_tools_delegation, "WORKSPACE_ID", _SELF_WS, raising=True)
    monkeypatch.setattr(builtin_delegation, "WORKSPACE_ID", _SELF_WS, raising=True)
    monkeypatch.setattr(builtin_a2a_tools, "WORKSPACE_ID", _SELF_WS, raising=True)

    a2a_client._peer_to_source.clear()
    a2a_client._peer_names.clear()

    yield

    platform_auth.clear_cache()
    a2a_client._peer_to_source.clear()
    a2a_client._peer_names.clear()


# ---------------------------------------------------------------------------
# Layer 1: a2a_tools_delegation.tool_delegate_task (sync MCP path)
# ---------------------------------------------------------------------------


class TestToolDelegateTaskSelfGuard:
    """Sync MCP path — used by Claude Code / Codex external runtimes."""

    @pytest.mark.asyncio
    async def test_rejects_self_workspace_id_without_dispatch(self):
        """workspace_id == module WORKSPACE_ID → rejected, no HTTP call."""
        from molecule_runtime import a2a_tools_delegation as mod

        # If the guard fails open, the call would reach discover_peer +
        # send_a2a_message. Patch both to AsyncMock so we can ASSERT they
        # were not awaited (and so the test fails fast if the guard
        # regresses, instead of hanging on a real network call).
        with patch.object(mod, "discover_peer", new=AsyncMock()) as p_disc, \
             patch.object(mod, "send_a2a_message", new=AsyncMock()) as p_send:
            start = time.monotonic()
            result = await mod.tool_delegate_task(_SELF_WS, "ping myself")
            elapsed_ms = (time.monotonic() - start) * 1000

        assert "self-delegation" in result.lower() or "your own workspace" in result.lower(), \
            f"expected self-delegation rejection, got: {result!r}"
        assert result.startswith("Error"), f"expected Error: prefix, got: {result!r}"
        p_disc.assert_not_awaited()
        p_send.assert_not_awaited()
        # Original bug took 30-600s before producing the echo; the guard
        # must fail synchronously well under 100ms.
        assert elapsed_ms < 100, f"self-guard took {elapsed_ms:.1f}ms — should be near-instant"

    @pytest.mark.asyncio
    async def test_rejects_self_when_source_workspace_id_explicit(self):
        """Multi-workspace: explicit source_workspace_id == target is
        still self-delegation — the routing fallback is irrelevant when
        the caller is explicit."""
        from molecule_runtime import a2a_tools_delegation as mod

        other_ws = "00000000-0000-0000-0000-0000000000aa"
        with patch.object(mod, "discover_peer", new=AsyncMock()) as p_disc, \
             patch.object(mod, "send_a2a_message", new=AsyncMock()) as p_send:
            result = await mod.tool_delegate_task(
                other_ws, "ping", source_workspace_id=other_ws,
            )

        assert result.startswith("Error")
        assert "your own workspace" in result or "self-delegation" in result
        p_disc.assert_not_awaited()
        p_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allows_legitimate_peer_delegation(self):
        """Sanity check: a different workspace_id is NOT blocked by the
        self-guard — the test catches an over-eager fix that would
        break every legitimate delegation."""
        from molecule_runtime import a2a_tools_delegation as mod

        peer_ws = "00000000-0000-0000-0000-0000000000bb"
        with patch.object(mod, "discover_peer", new=AsyncMock(return_value={
                "name": "peer-bb", "status": "online", "url": "ignored",
             })) as p_disc, \
             patch.object(mod, "send_a2a_message", new=AsyncMock(return_value="ok")) as p_send, \
             patch("molecule_runtime.a2a_tools.report_activity", new=AsyncMock()):
            result = await mod.tool_delegate_task(peer_ws, "real task")

        # Peer-route was actually taken — guard did not over-block.
        p_disc.assert_awaited_once()
        p_send.assert_awaited_once()
        # Result is the wrapped peer response, NOT an Error.
        assert not result.startswith("Error")


# ---------------------------------------------------------------------------
# Layer 2: a2a_tools_delegation.tool_delegate_task_async (async MCP path)
# ---------------------------------------------------------------------------


class TestToolDelegateTaskAsyncSelfGuard:
    """Async MCP path — fire-and-forget via platform /delegate endpoint."""

    @pytest.mark.asyncio
    async def test_rejects_self_without_http_post(self):
        """No POST to /delegate when target == self."""
        from molecule_runtime import a2a_tools_delegation as mod

        with patch("httpx.AsyncClient") as p_client:
            start = time.monotonic()
            result = await mod.tool_delegate_task_async(_SELF_WS, "ping myself")
            elapsed_ms = (time.monotonic() - start) * 1000

        assert "your own workspace" in result or "self-delegation" in result.lower()
        assert result.startswith("Error")
        # httpx.AsyncClient must not even be constructed.
        p_client.assert_not_called()
        assert elapsed_ms < 100


# ---------------------------------------------------------------------------
# Layer 3: builtin_tools.delegation.delegate_task_async (LangChain @tool)
# ---------------------------------------------------------------------------


class TestBuiltinDelegationAsyncSelfGuard:
    """In-container LangChain runtime path. The original #190 chain ran
    here — _execute_delegation would fire the A2A POST back at our own
    URL, time out, and the result row would surface as peer_agent."""

    @pytest.mark.asyncio
    async def test_rejects_self_no_background_task_scheduled(self):
        """asyncio.create_task(_execute_delegation(...)) must NOT fire."""
        import molecule_runtime.builtin_tools.delegation as deleg

        # Pin the module-level WORKSPACE_ID — it's read at import time
        # via get_workspace_id(), and the conftest sentinel may have
        # been cached by an earlier test before our isolate_env fixture
        # set the real one.
        with patch.object(deleg, "WORKSPACE_ID", _SELF_WS), \
             patch.object(deleg, "_execute_delegation", new=AsyncMock()) as p_exec, \
             patch.object(deleg, "check_permission", return_value=True), \
             patch.object(deleg, "get_workspace_roles", return_value=(["admin"], {})):
            # langchain @tool wraps the callable in a StructuredTool;
            # `.coroutine` is the underlying async function. (`.func` is
            # None for async-only tools.) Calling it directly bypasses
            # the wrapper's input-schema coercion and lets us assert
            # the guard at the function body.
            tool_coro = deleg.delegate_task_async.coroutine
            assert tool_coro is not None, "delegate_task_async should expose .coroutine"
            start = time.monotonic()
            result = await tool_coro(workspace_id=_SELF_WS, task="ping myself")
            elapsed_ms = (time.monotonic() - start) * 1000

        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "self-delegation" in result.get("error", "").lower()
        p_exec.assert_not_called()
        assert elapsed_ms < 100


# ---------------------------------------------------------------------------
# Layer 4: builtin_tools.a2a_tools.delegate_task (framework-agnostic)
# ---------------------------------------------------------------------------


class TestBuiltinA2AToolsSelfGuard:
    """Smoke-test the slim helper that other in-container code paths
    occasionally import directly."""

    @pytest.mark.asyncio
    async def test_rejects_self(self):
        import molecule_runtime.builtin_tools.a2a_tools as a2a_mod

        with patch.object(a2a_mod, "WORKSPACE_ID", _SELF_WS), \
             patch("httpx.AsyncClient") as p_client:
            result = await a2a_mod.delegate_task(_SELF_WS, "ping myself")

        assert "self-delegation" in result.lower()
        p_client.assert_not_called()


# ---------------------------------------------------------------------------
# Layer 5: MCP server dispatch propagates the rejection
# ---------------------------------------------------------------------------


class TestMcpServerSelfDelegationPropagation:
    """handle_tool_call must surface the rejection string verbatim — it
    must not swallow it into a 'tool failed' generic, or the agent loses
    the actionable explanation in its result envelope."""

    @pytest.mark.asyncio
    async def test_mcp_dispatch_returns_self_delegation_error(self):
        from molecule_runtime import a2a_mcp_server as mcp

        # Bypass RBAC so we exercise the delegate path specifically.
        with patch.object(mcp, "_tool_permission_check", return_value=None), \
             patch("molecule_runtime.mcp_tools._tool_permission_check",
                   return_value=None), \
             patch("molecule_runtime.a2a_tools_delegation.discover_peer",
                   new=AsyncMock()) as p_disc, \
             patch("molecule_runtime.a2a_tools_delegation.send_a2a_message",
                   new=AsyncMock()) as p_send:
            result = await mcp.handle_tool_call(
                "delegate_task",
                {"workspace_id": _SELF_WS, "task": "echo loop"},
            )

        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "your own workspace" in result or "self-delegation" in result.lower()
        p_disc.assert_not_awaited()
        p_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mcp_dispatch_async_returns_self_delegation_error(self):
        from molecule_runtime import a2a_mcp_server as mcp

        with patch.object(mcp, "_tool_permission_check", return_value=None), \
             patch("molecule_runtime.mcp_tools._tool_permission_check",
                   return_value=None), \
             patch("httpx.AsyncClient") as p_client:
            result = await mcp.handle_tool_call(
                "delegate_task_async",
                {"workspace_id": _SELF_WS, "task": "echo loop"},
            )

        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "your own workspace" in result or "self-delegation" in result.lower()
        p_client.assert_not_called()
