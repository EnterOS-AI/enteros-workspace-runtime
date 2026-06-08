"""Test-env stubs for workspace-container deps.

`claude_agent_sdk` and `a2a` are installed inside the claude-code workspace
image, not in this package's test environment. Rather than pulling them as
dev deps (they're heavy + change frequently), we stub them before any test
module imports `molecule_runtime.*` — the stubs just need to be importable
so the package source doesn't crash at collect time.

Every known submodule + symbol used anywhere in `molecule_runtime/` is
stubbed here, so test_imports.py (walking every module) + test_session_
resume_gate.py (ClaudeSDKExecutor methods) + future tests all share one
consistent stub set.

NOTE: a2a-sdk 1.x (KI-009 migration) changed the module layout:
- REMOVED: a2a.server.apps (A2AStarletteApplication)
- REMOVED: a2a.utils
- ADDED: a2a.server.routes (create_agent_card_routes, create_jsonrpc_routes)
- ADDED: AgentInterface in a2a.types
"""
from __future__ import annotations

import os
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _reset_a2a_client_workspace_id_cache():
    """Per-test reset of `molecule_runtime.a2a_client._WORKSPACE_ID_cache`.

    The lazy-validation design (PEP 562 module __getattr__ in
    a2a_client.py, per PR#99) caches the validated WORKSPACE_ID on
    first access. Between tests, monkeypatch.setenv("WORKSPACE_ID",
    ...) changes the env, but the cached value would otherwise stick.
    The targeted test_a2a_multi_workspace.py fixture also clears
    this cache directly, but session-wide autouse here catches
    cross-file test order leakage.
    """
    try:
        import molecule_runtime.a2a_client as a2a_client
        a2a_client._WORKSPACE_ID_cache = None
    except ImportError:
        # a2a_client may not be importable in every test context (e.g.
        # when only collecting-imports tests); skip silently.
        pass
    yield
    try:
        import molecule_runtime.a2a_client as a2a_client
        a2a_client._WORKSPACE_ID_cache = None
    except ImportError:
        pass

# Rest of the conftest.py is the stub setup from the original
