"""Shared Molecule MCP tool contract and dispatcher.

Runtime adapters should consume this module instead of copying Molecule
tool schemas or reimplementing dispatch. The platform tool registry remains
the canonical source of individual tool specs; this module adapts that
registry into MCP/OpenAI shapes and applies the shared RBAC gate.
"""

from __future__ import annotations

import inspect
from typing import Any

from molecule_runtime.platform_tools.registry import (
    TOOLS as PLATFORM_TOOL_SPECS,
    ToolSpec,
    by_name,
)

MOLECULE_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": spec.name,
        "description": spec.short,
        "inputSchema": spec.input_schema,
    }
    for spec in PLATFORM_TOOL_SPECS
]

PERMISSION_MAP: dict[str, str] = {
    "delegate_task": "delegate",
    "delegate_task_async": "delegate",
    "check_task_status": "delegate",
    "send_message_to_user": "approve",
    "commit_memory": "memory.write",
    "update_agent_card": "memory.write",
}


def openai_function_tools() -> list[dict[str, Any]]:
    """Return the shared Molecule tools in OpenAI function-tool shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
        }
        for tool in MOLECULE_MCP_TOOLS
    ]


def _check_permission(action: str) -> None:
    """Raise PermissionError if the caller lacks ``action`` permission."""
    from molecule_runtime.builtin_tools.audit import (
        check_permission,
        get_workspace_roles,
    )

    roles, custom = get_workspace_roles()
    if not check_permission(action, roles, custom):
        raise PermissionError(f"RBAC: action '{action}' denied for roles {roles}")


def _tool_permission_check(name: str, arguments: dict[str, Any]) -> str | None:
    """Run RBAC check for ``name``; return error string if blocked."""
    action = PERMISSION_MAP.get(name)
    if action is None:
        return None
    try:
        _check_permission(action)
    except PermissionError as exc:
        return str(exc)
    return None


def _kwargs_for_spec(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Forward only parameters accepted by the canonical implementation."""
    if spec.name == "send_message_to_user" and "attachments" in arguments:
        raw_attachments = arguments.get("attachments")
        if isinstance(raw_attachments, list):
            arguments = {
                **arguments,
                "attachments": [
                    p for p in raw_attachments
                    if isinstance(p, str) and p
                ],
            }

    signature = inspect.signature(spec.impl)
    kwargs: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if name in arguments:
            kwargs[name] = arguments[name]
        elif parameter.default is inspect.Parameter.empty:
            kwargs[name] = None if name == "card" else ""
    return kwargs


async def handle_molecule_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """Handle a shared Molecule tool call and return text for MCP clients."""
    rbac_error = _tool_permission_check(name, arguments)
    if rbac_error is not None:
        return f"PERMISSION DENIED: {rbac_error}"

    try:
        spec = by_name(name)
    except KeyError:
        return f"Unknown tool: {name}"

    return await spec.impl(**_kwargs_for_spec(spec, arguments))
