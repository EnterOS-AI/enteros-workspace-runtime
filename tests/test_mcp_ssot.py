"""SSOT drift tests for the universal Molecule MCP tool + target surface (issue #38).

These tests pin the contract from the issue's acceptance criteria:

  * Base runtime has the reusable schema/client/target APIs adapters need.
  * Tests prove canonical schema export and backward-compatible env parsing.
  * Docs state adapters are shims and base MCP/runtime is SSOT.

The drift-prevention contract: any future adapter that re-implements a
universal Molecule tool schema, drops a universal tool, or re-parses
the env-driven workspace resolution will fail THIS test at CI time,
NOT silently at production runtime.

The test suite covers three pillars:

  1. The SSOT public surface (mcp_schemas / mcp_target_resolution)
     re-exports the lower-level modules without diverging. Drift =
     import error, attribute mismatch, or re-defined symbol.
  2. Adapters that consume the SSOT must declare exactly the same tool
     set. Drift = a tool added or removed from either side.
  3. The env-driven workspace resolution contract covers all the shapes
     the platform has shipped (legacy single-workspace, multi-workspace
     JSON, token-file fallback). Drift = a shape that doesn't round-trip
     through the resolver.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# ==================== Pillar 1: SSOT public surface ====================

class TestSsotPublicSurface:
    """The SSOT modules re-export the lower-level modules. Drift =
    missing symbol, attribute mismatch, or a re-definition that
    diverges from the underlying implementation.

    The SSOT public surface is the contract adapters consume; if it
    silently falls out of sync with the implementation (e.g. someone
    re-implements get_tool_schema on the SSOT side and the result
    drifts from the registry), the contract is broken even if the
    import path still works.
    """

    def test_mcp_schemas_reexports_canonical_list(self):
        """The SSOT MOLECULE_MCP_TOOLS list IS the same object as the
        lower-level mcp_tools.MOLECULE_MCP_TOOLS (identity, not just
        equality). A copy would silently fork the contract on the
        next schema change."""
        from molecule_runtime import mcp_schemas
        from molecule_runtime import mcp_tools

        assert mcp_schemas.MOLECULE_MCP_TOOLS is mcp_tools.MOLECULE_MCP_TOOLS, (
            "mcp_schemas.MOLECULE_MCP_TOOLS must be the SAME list as "
            "mcp_tools.MOLECULE_MCP_TOOLS (identity, not a copy). A copy "
            "silently forks the SSOT — drift would surface at adapter "
            "test time, not here."
        )

    def test_mcp_schemas_reexports_permission_map(self):
        """PERMISSION_MAP is the SSOT RBAC gate. Same identity
        contract as MOLECULE_MCP_TOOLS."""
        from molecule_runtime import mcp_schemas
        from molecule_runtime import mcp_tools

        assert mcp_schemas.PERMISSION_MAP is mcp_tools.PERMISSION_MAP

    def test_mcp_schemas_reexports_openai_function_tools(self):
        """openai_function_tools is the SSOT OpenAI-shape adapter."""
        from molecule_runtime import mcp_schemas
        from molecule_runtime import mcp_tools

        assert mcp_schemas.openai_function_tools is mcp_tools.openai_function_tools

    def test_mcp_schemas_get_tool_schema_returns_registry_input_schema(self):
        """get_tool_schema(name) returns the same inputSchema dict the
        platform registry owns. An adapter that uses get_tool_schema
        and another that reaches into platform_tools.registry directly
        MUST see the same dict for the same name (drift test)."""
        from molecule_runtime import mcp_schemas
        from molecule_runtime.platform_tools.registry import TOOLS

        # A representative sample: delegate_task (universal Molecule tool).
        ssot_schema = mcp_schemas.get_tool_schema("delegate_task")
        registry_schema = next(
            spec.input_schema for spec in TOOLS if spec.name == "delegate_task"
        )
        assert ssot_schema is registry_schema, (
            "get_tool_schema(name) must return the SAME dict as the "
            "registry's input_schema. A copy silently forks the contract."
        )

    def test_mcp_schemas_get_tool_schema_returns_none_for_unknown(self):
        """Unknown tool name returns None — never invents a default
        shape. The caller decides raise-vs-skip; the SSOT stays
        strict."""
        from molecule_runtime import mcp_schemas

        assert mcp_schemas.get_tool_schema("not_a_real_tool") is None

    def test_mcp_schemas_exports_stable(self):
        """The ``__all__`` list is the public surface — adapters import
        from this name. A future change that renames an export without
        updating __all__ will fail this test at import time, not at
        adapter-runtime."""
        from molecule_runtime import mcp_schemas

        expected = {
            "MOLECULE_MCP_TOOLS",
            "PERMISSION_MAP",
            "openai_function_tools",
            "get_tool_schema",
            "validate_adapter_schemas",
        }
        assert set(mcp_schemas.__all__) == expected

    def test_mcp_target_resolution_reexports(self):
        """Same identity contract as mcp_schemas — the SSOT for env
        parsing must be the same callable as the lower-level
        implementation."""
        from molecule_runtime import mcp_target_resolution
        from molecule_runtime import mcp_workspace_resolver

        assert mcp_target_resolution.resolve_workspaces is mcp_workspace_resolver.resolve_workspaces
        assert mcp_target_resolution.read_token_file is mcp_workspace_resolver.read_token_file
        assert mcp_target_resolution.print_missing_env_help is mcp_workspace_resolver.print_missing_env_help

    def test_mcp_target_resolution_exports_stable(self):
        from molecule_runtime import mcp_target_resolution

        expected = {
            "resolve_workspaces",
            "read_token_file",
            "print_missing_env_help",
            "resolve_target_for_adapter",
        }
        assert set(mcp_target_resolution.__all__) == expected


# ==================== Pillar 2: Adapter SSOT conformance ====================

class TestAdapterConformsToSsot:
    """Adapters that consume the SSOT must declare exactly the tool
    set the SSOT publishes. Drift = the adapter omits a universal
    Molecule tool (extra in SSOT) or invents a new one (missing in
    SSOT) — either way, the contract is broken.
    """

    def test_a2a_mcp_server_declares_full_ssot_set(self):
        """The a2a_mcp_server is the canonical adapter. It must
        declare every tool in MOLECULE_MCP_TOOLS, no more, no less.
        Anything else means a fork."""
        from molecule_runtime import a2a_mcp_server
        from molecule_runtime import mcp_schemas

        # a2a_mcp_server.TOOLS is the same object as MOLECULE_MCP_TOOLS
        # (per test_mcp_tools_contract); use it as-is.
        result = mcp_schemas.validate_adapter_schemas(a2a_mcp_server.TOOLS)
        assert not result["missing"], (
            f"a2a_mcp_server is declaring tools not in the SSOT (forks "
            f"the contract): {result['missing']}. Add them to "
            f"platform_tools.registry.TOOLS and remove the local copies."
        )
        assert not result["extra"], (
            f"a2a_mcp_server is DROPPING universal Molecule tools the "
            f"SSOT publishes: {result['extra']}. The a2a_mcp_server is "
            f"a shim — base MCP/runtime is SSOT. Update the adapter to "
            f"register the full tool list from mcp_schemas.MOLECULE_MCP_TOOLS."
        )

    def test_openai_function_tools_match_ssot(self):
        """The OpenAI-shape adapter is derived from the same SSOT list.
        A divergence means the OpenAI tool shape has drifted from the
        MCP shape — the same canonical list should back both."""
        from molecule_runtime import mcp_schemas

        oa_tools = mcp_schemas.openai_function_tools()
        result = mcp_schemas.validate_adapter_schemas(
            [{"name": t["function"]["name"]} for t in oa_tools]
        )
        assert not result["missing"] and not result["extra"], (
            f"OpenAI function tools drifted from the SSOT. Missing: "
            f"{result['missing']}, Extra: {result['extra']}. The OpenAI "
            f"adapter must derive from MOLECULE_MCP_TOOLS (the SSOT)."
        )


# ==================== Pillar 3: Env-resolution contract ====================

class TestEnvResolutionContract:
    """The env-driven workspace resolution contract covers the
    shapes the platform has shipped:

      * Single-workspace legacy: ``WORKSPACE_ID`` + ``MOLECULE_WORKSPACE_TOKEN``
      * Single-workspace token-file fallback: ``${CONFIGS_DIR}/.auth_token``
      * Multi-workspace JSON: ``MOLECULE_WORKSPACES`` (a JSON array of
        ``{"id", "token", "platform_url"}`` objects)

    Drift = a shape that doesn't round-trip through
    ``resolve_workspaces()``. An adapter that re-parses these env vars
    directly would silently fork the contract on the next platform
    change (e.g. a new field, a new shape).
    """

    def test_single_workspace_legacy_round_trip(self, monkeypatch, tmp_path):
        """Single-workspace legacy: WORKSPACE_ID + MOLECULE_WORKSPACE_TOKEN
        → resolve_workspaces returns one triple. The triple is what
        adapters register against the platform with."""
        monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
        monkeypatch.setenv("WORKSPACE_ID", "ws-legacy-test-001")
        monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "tok-legacy-test")
        monkeypatch.delenv("MOLECULE_WORKSPACES", raising=False)

        from molecule_runtime.mcp_target_resolution import resolve_workspaces
        workspaces, errors = resolve_workspaces()

        assert errors == [], f"unexpected errors: {errors}"
        assert len(workspaces) == 1
        wid, tok, platform = workspaces[0]
        assert wid == "ws-legacy-test-001"
        assert tok == "tok-legacy-test"
        # platform_url falls back to the module-level PLATFORM_URL; we
        # don't pin it here (it's env-driven and tested elsewhere).

    def test_single_workspace_token_file_fallback(self, monkeypatch, tmp_path):
        """Single-workspace token-file fallback: no
        MOLECULE_WORKSPACE_TOKEN env, but ${CONFIGS_DIR}/.auth_token
        exists → resolve_workspaces picks it up."""
        configs = tmp_path / "configs"
        configs.mkdir()
        auth_token = configs / ".auth_token"
        auth_token.write_text("tok-from-file-test")

        monkeypatch.setenv("CONFIGS_DIR", str(configs))
        monkeypatch.setenv("WORKSPACE_ID", "ws-tokfile-test-002")
        monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)
        monkeypatch.delenv("MOLECULE_WORKSPACES", raising=False)

        from molecule_runtime.mcp_target_resolution import resolve_workspaces
        workspaces, errors = resolve_workspaces()

        assert errors == [], f"unexpected errors: {errors}"
        assert len(workspaces) == 1
        assert workspaces[0][0] == "ws-tokfile-test-002"
        assert workspaces[0][1] == "tok-from-file-test"

    def test_multi_workspace_json_round_trip(self, monkeypatch, tmp_path):
        """Multi-workspace JSON: ``MOLECULE_WORKSPACES`` carries an
        array of {id, token, platform_url} objects → resolve_workspaces
        returns one triple per entry, in order. The legacy single-workspace
        env is IGNORED when the JSON is set (the JSON is the source
        of truth)."""
        monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
        monkeypatch.setenv("WORKSPACE_ID", "ws-legacy-IGNORED")
        monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "tok-legacy-IGNORED")
        monkeypatch.setenv(
            "MOLECULE_WORKSPACES",
            json.dumps([
                {"id": "ws-multi-a", "token": "tok-a", "platform_url": "https://a.test"},
                {"id": "ws-multi-b", "token": "tok-b", "platform_url": "https://b.test"},
            ]),
        )
        monkeypatch.delenv("MOLECULE_WORKSPACE_PLATFORMS", raising=False)

        from molecule_runtime.mcp_target_resolution import resolve_workspaces
        workspaces, errors = resolve_workspaces()

        assert errors == [], f"unexpected errors: {errors}"
        assert len(workspaces) == 2
        assert workspaces[0][0] == "ws-multi-a"
        assert workspaces[0][1] == "tok-a"
        assert workspaces[0][2] == "https://a.test"
        assert workspaces[1][0] == "ws-multi-b"
        assert workspaces[1][1] == "tok-b"
        assert workspaces[1][2] == "https://b.test"

    def test_resolve_target_for_adapter_returns_first_target(self, monkeypatch, tmp_path):
        """The single-target convenience wrapper returns the first
        resolved target as a dict. Adapters that operate on a single
        workspace call this instead of indexing into the multi-workspace
        list themselves."""
        monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
        monkeypatch.setenv(
            "MOLECULE_WORKSPACES",
            json.dumps([
                {"id": "ws-first", "token": "tok-first", "platform_url": "https://first.test"},
                {"id": "ws-second", "token": "tok-second", "platform_url": "https://second.test"},
            ]),
        )
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)

        from molecule_runtime.mcp_target_resolution import resolve_target_for_adapter
        target = resolve_target_for_adapter()
        assert target == {
            "id": "ws-first",
            "token": "tok-first",
            "platform_url": "https://first.test",
        }

    def test_resolve_target_for_adapter_returns_none_when_no_workspaces(self, monkeypatch, tmp_path):
        """No workspaces resolvable → None. Caller decides fail-closed
        vs fall-through; the SSOT never invents a default target."""
        monkeypatch.setenv("CONFIGS_DIR", str(tmp_path))
        monkeypatch.delenv("MOLECULE_WORKSPACES", raising=False)
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)

        from molecule_runtime.mcp_target_resolution import resolve_target_for_adapter
        assert resolve_target_for_adapter() is None
