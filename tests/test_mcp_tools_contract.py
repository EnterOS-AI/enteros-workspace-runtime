"""Contract tests for the shared Molecule MCP tool surface."""

import os

os.environ.setdefault("WORKSPACE_ID", "00000000-0000-0000-0000-000000000001")


def test_mcp_tools_export_matches_runtime_server():
    from molecule_runtime import a2a_mcp_server
    from molecule_runtime.mcp_tools import MOLECULE_MCP_TOOLS

    assert a2a_mcp_server.TOOLS is MOLECULE_MCP_TOOLS
    assert [tool["name"] for tool in MOLECULE_MCP_TOOLS] == [
        "delegate_task",
        "delegate_task_async",
        "check_task_status",
        "list_peers",
        "get_workspace_info",
        "get_runtime_identity",
        "update_agent_card",
        "broadcast_message",
        "send_message_to_user",
        "create_request",
        "create_approval",
        "install_plugin",
        "desktop_status",
        "desktop_screenshot",
        "desktop_click",
        "desktop_type",
        "desktop_key",
        "desktop_open_url",
        "desktop_wait_for_control",
        "wait_for_message",
        "inbox_peek",
        "inbox_pop",
        "chat_history",
        "commit_memory",
        "recall_memory",
        "goal_get",
        "goal_set",
        "goal_clear",
        "task_list",
        "task_add",
        "task_update",
        "task_complete",
    ]


def test_install_plugin_is_a_default_self_scoped_tool():
    """install_plugin is a DEFAULT tool for EVERY workspace (self-scoped
    plugin install), so it must:
      * appear in the SSOT tool list (offered to all workspaces), and
      * NOT be gated by a runtime RBAC action in PERMISSION_MAP — the
        server's org plugin allowlist + per-workspace-token auth are the
        only gates. This mirrors broadcast_message (also ungated at the
        runtime layer, enforced server-side), keeping the "install an app
        on your own phone" default intact.
    """
    from molecule_runtime.mcp_tools import MOLECULE_MCP_TOOLS, PERMISSION_MAP

    names = [tool["name"] for tool in MOLECULE_MCP_TOOLS]
    assert "install_plugin" in names, (
        "install_plugin must be in the SSOT tool list so every workspace "
        "sees it by default (self-scoped install)."
    )
    assert "install_plugin" not in PERMISSION_MAP, (
        "install_plugin must NOT be gated by a runtime RBAC action — it is a "
        "self-scoped default; the org allowlist governs WHICH plugins, and the "
        "server's per-workspace-token auth governs WHOSE workspace."
    )


def test_openai_function_tools_are_derived_from_mcp_schema():
    from molecule_runtime.mcp_tools import MOLECULE_MCP_TOOLS, openai_function_tools

    by_name = {tool["name"]: tool for tool in MOLECULE_MCP_TOOLS}
    functions = openai_function_tools()

    assert len(functions) == len(MOLECULE_MCP_TOOLS)
    delegate = next(
        tool for tool in functions
        if tool["function"]["name"] == "delegate_task"
    )
    assert delegate["type"] == "function"
    assert delegate["function"]["description"] == by_name["delegate_task"]["description"]
    assert delegate["function"]["parameters"] == by_name["delegate_task"]["inputSchema"]


def test_dispatch_reuses_shared_permission_gate(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import molecule_runtime.mcp_tools as mcp_tools

    async def fake_delegate(workspace_id: str, task: str, source_workspace_id=None) -> str:
        return f"{workspace_id}:{task}:{source_workspace_id}"

    monkeypatch.setattr(mcp_tools, "_tool_permission_check", lambda _name, _args: "denied")
    monkeypatch.setattr(
        mcp_tools,
        "by_name",
        lambda _name: SimpleNamespace(name="delegate_task", impl=fake_delegate),
    )

    result = asyncio.run(
        mcp_tools.handle_molecule_tool_call(
            "delegate_task",
            {"workspace_id": "ws-target", "task": "do it"},
        )
    )

    assert result == "PERMISSION DENIED: denied"


def test_dispatch_calls_canonical_tool_impl(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import molecule_runtime.mcp_tools as mcp_tools

    calls = []

    async def fake_delegate(workspace_id: str, task: str, source_workspace_id=None) -> str:
        calls.append((workspace_id, task, source_workspace_id))
        return "ok"

    monkeypatch.setattr(mcp_tools, "_tool_permission_check", lambda _name, _args: None)
    monkeypatch.setattr(
        mcp_tools,
        "by_name",
        lambda _name: SimpleNamespace(name="delegate_task", impl=fake_delegate),
    )

    result = asyncio.run(
        mcp_tools.handle_molecule_tool_call(
            "delegate_task",
            {
                "workspace_id": "ws-target",
                "task": "do it",
                "source_workspace_id": "ws-source",
                "ignored": "not forwarded",
            },
        )
    )

    assert result == "ok"
    assert calls == [("ws-target", "do it", "ws-source")]
