"""SSOT re-export for the universal Molecule MCP tool schemas (issue #38).

Adapters (a2a_mcp_server, langchain integrations, future SDKs) MUST
import their tool list and per-tool schemas from this module instead
of:

  * re-implementing the schema dict,
  * copying the tool list into a local file,
  * importing the lower-level ``mcp_tools`` / ``platform_tools.registry``
    modules directly.

The lower-level modules remain the implementation; this module is
the **stable public surface** that drift-tests pin adapters against.
The drift is one of the failure modes the SSOT was created to prevent
(a previous refactor split the universal Molecule contract across
``a2a_mcp_server``, ``mcp_tools``, and ``platform_tools.registry``,
which made it easy for a future adapter to re-own a schema and silently
fork the contract).

Public surface
--------------

* ``MOLECULE_MCP_TOOLS`` — the canonical MCP/OpenAI-shape tool list.
  Adapters that need to register tools with an MCP server import this
  directly.
* ``openai_function_tools()`` — the same list in OpenAI function-tool
  shape, for adapters targeting the OpenAI SDK.
* ``PERMISSION_MAP`` — tool-name -> RBAC action mapping. The MCP server
  applies this gate before dispatch.
* ``get_tool_schema(name)`` — schema-only lookup. Adapters that need
  one tool's input schema (e.g. a UI hint) call this instead of
  re-implementing the lookup.
* ``validate_adapter_schemas(adapter_tools)`` — drift check. Adapters
  call this at startup to assert their declared tool list is a strict
  subset of the SSOT. Returns the list of missing/extra tools; the
  adapter SHOULD log+fail-closed on a non-empty list (the test_mcp_ssot
  test pins this contract).

Why the re-export layer
-----------------------

The lower-level ``molecule_runtime.mcp_tools`` adapts the platform's
``platform_tools.registry.TOOLS`` into MCP shapes; the registry is
itself the SSOT for the underlying tool implementations. The drift
chain is: ``platform_tools.registry.ToolSpec`` (impl + schema source)
→ ``mcp_tools.MOLECULE_MCP_TOOLS`` (MCP-shape adapter) → this module
(SSOT public surface for adapters). Drift tests pin each step.

Without this module, an adapter that imports ``mcp_tools`` directly
bypasses the SSOT public surface — and the next refactor of
``mcp_tools`` (e.g. moving it into the MCP server module) would
silently break the adapter. This module is the contract surface that
makes that refactor loud at adapter test time, not at production
runtime.

See also: ``mcp_target_resolution`` for the env-driven workspace
resolution contract.
"""
from __future__ import annotations

from typing import Any

# Re-exports — the entire SSOT public surface adapters should use.
from molecule_runtime.mcp_tools import (  # noqa: F401
    MOLECULE_MCP_TOOLS,
    PERMISSION_MAP,
    openai_function_tools,
)


def get_tool_schema(name: str) -> dict[str, Any] | None:
    """Return the inputSchema for one Molecule tool, or None if unknown.

    Adapters that need a single tool's schema (e.g. a UI prompt
    generator, a per-tool permission gate) call this instead of
    re-implementing the lookup against the lower-level registry. None
    is returned for unknown names — the caller decides whether to
    raise or skip, but the SSOT never invents a default shape.
    """
    for tool in MOLECULE_MCP_TOOLS:
        if tool["name"] == name:
            return tool.get("inputSchema")
    return None


def validate_adapter_schemas(adapter_tools: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Drift check an adapter's declared tool list against the SSOT.

    Returns a dict with two keys:

    * ``"missing"`` — adapter-declared tools that are NOT in the SSOT
      (the adapter is offering a tool nobody owns — drift).
    * ``"extra"`` — SSOT tools that the adapter has NOT declared (the
      adapter is dropping a universal Molecule contract — drift).

    Both empty is the contract. A non-empty result indicates a
    divergence that the SSOT contract was created to prevent — the
    adapter SHOULD log+fail-closed on a non-empty result, NOT silently
    pass.

    The test_mcp_ssot.py drift test pins this contract for the
    in-tree adapters.
    """
    ssot_names = {tool["name"] for tool in MOLECULE_MCP_TOOLS}
    adapter_names = {tool.get("name") for tool in adapter_tools if tool.get("name")}
    return {
        "missing": sorted(adapter_names - ssot_names),
        "extra": sorted(ssot_names - adapter_names),
    }


__all__ = [
    "MOLECULE_MCP_TOOLS",
    "PERMISSION_MAP",
    "openai_function_tools",
    "get_tool_schema",
    "validate_adapter_schemas",
]
