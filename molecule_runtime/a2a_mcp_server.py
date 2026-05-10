#!/usr/bin/env python3
"""A2A MCP Server — runs inside each workspace container.

Exposes A2A delegation, peer discovery, and workspace info as MCP tools
so CLI-based runtimes (Claude Code, Codex) can communicate with other workspaces.

Two transports:
  stdio  — default; JSON-RPC over stdin/stdout. Used by Claude Code CLI.
  http   — MCP over HTTP/SSE. Used by Hermes runtime which is MCP-native.

Launched automatically by main.py for CLI runtimes (stdio) or by the Hermes
template's start.sh (http on :9100). Configured as a local MCP server for
the claude --print invocation.

Environment variables (set by the workspace container):
  WORKSPACE_ID  — this workspace's ID
  PLATFORM_URL  — platform API base URL (e.g. http://platform:8080)
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid

# Absolute imports so the installed-package location works too. Previously
# the script relied on `/app` being on sys.path (legacy template layout),
# which broke silently when the current template dropped that copy —
# claude-code then initialised with zero MCP tools and every agent
# reported "search_memory / commit_memory / list_peers / delegate_task
# not available" (second half of #507). The /app launch path is still
# supported via a sys.path shim below for anyone running the script
# with `python /app/a2a_mcp_server.py`.
import os as _os
if __package__ in (None, ""):
    # Running as a script (python path/to/a2a_mcp_server.py) — put the
    # package root on sys.path so the absolute imports below resolve.
    _pkg_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)

from molecule_runtime.a2a_tools import (
    tool_check_task_status,
    tool_commit_memory,
    tool_delegate_task,
    tool_delegate_task_async,
    tool_get_workspace_info,
    tool_list_peers,
    tool_recall_memory,
    tool_send_message_to_user,
)

logger = logging.getLogger(__name__)

# --- RBAC gate ---
# Lazy import avoids circular dep at startup (audit.py imports config.py, which
# imports this module). _load_workspace_roles is cached inside audit.py.
_PERMISSION_MAP: dict[str, str] = {
    "delegate_task": "delegate",
    "delegate_task_async": "delegate",
    "check_task_status": "delegate",
    "send_message_to_user": "approve",
    "commit_memory": "memory.write",
    # recall_memory, list_peers, get_workspace_info are always permitted
}


def _check_permission(action: str) -> None:
    """Raise PermissionError if the caller lacks ``action`` permission."""
    from molecule_runtime.builtin_tools.audit import (
        check_permission,
        get_workspace_roles,
    )

    roles, custom = get_workspace_roles()
    if not check_permission(action, roles, custom):
        raise PermissionError(f"RBAC: action '{action}' denied for roles {roles}")


def _tool_permission_check(name: str, arguments: dict) -> str | None:
    """Run RBAC check for ``name``; return error string or None if blocked."""
    action = _PERMISSION_MAP.get(name)
    if action is None:
        return None  # No RBAC gate for this tool
    try:
        _check_permission(action)
    except PermissionError as exc:
        return str(exc)
    return None

# Re-export constants and client functions so existing imports
# (e.g. tests that do `import a2a_mcp_server`) still work.
from molecule_runtime.a2a_client import (  # noqa: F401, E402
    PLATFORM_URL,
    WORKSPACE_ID,
    _A2A_ERROR_PREFIX,
    _peer_names,
    discover_peer,
    get_peers,
    get_workspace_info,
    send_a2a_message,
)
from molecule_runtime.a2a_tools import report_activity  # noqa: F401, E402

# --- Tool definitions (schemas) ---

TOOLS = [
    {
        "name": "delegate_task",
        "description": "Delegate a task to another workspace via A2A protocol and WAIT for the response. Use for quick tasks. The target must be a peer (sibling or parent/child). Use list_peers to find available targets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Target workspace ID (from list_peers)",
                },
                "task": {
                    "type": "string",
                    "description": "The task description to send to the target workspace",
                },
            },
            "required": ["workspace_id", "task"],
        },
    },
    {
        "name": "delegate_task_async",
        "description": "Send a task to another workspace with a short timeout (fire-and-forget). Returns immediately — the target continues processing. Best when you don't need the result right away. Note: check_task_status may not work with all workspace implementations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Target workspace ID (from list_peers)",
                },
                "task": {
                    "type": "string",
                    "description": "The task description to send to the target workspace",
                },
            },
            "required": ["workspace_id", "task"],
        },
    },
    {
        "name": "check_task_status",
        "description": "Check the status of a previously submitted async task via tasks/get. Note: only works if the target workspace's A2A implementation supports task persistence. May return 'not found' for completed tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "The workspace ID the task was sent to",
                },
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by delegate_task_async",
                },
            },
            "required": ["workspace_id", "task_id"],
        },
    },
    {
        "name": "list_peers",
        "description": "List all workspaces this agent can communicate with (siblings and parent/children). Returns name, ID, status, and role for each peer.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_workspace_info",
        "description": "Get this workspace's own info — ID, name, role, tier, parent, status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message_to_user",
        "description": "Send a message directly to the user's canvas chat — pushed instantly via WebSocket. Use this to: (1) acknowledge a task immediately ('Got it, I'll start working on this'), (2) send interim progress updates while doing long work, (3) deliver follow-up results after delegation completes. The message appears in the user's chat as if you're proactively reaching out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to send to the user",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "commit_memory",
        "description": "Save important information to persistent memory. Use this to remember decisions, conversation context, task results, and anything that should survive a restart. Scope: LOCAL (this workspace only), TEAM (parent + siblings), GLOBAL (entire org).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to remember — be detailed and specific",
                },
                "scope": {
                    "type": "string",
                    "enum": ["LOCAL", "TEAM", "GLOBAL"],
                    "description": "Memory scope (default: LOCAL)",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall_memory",
        "description": "Search persistent memory for previously saved information. Returns all matching memories. Use this at the start of conversations to recall prior context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (empty returns all memories)",
                },
                "scope": {
                    "type": "string",
                    "enum": ["LOCAL", "TEAM", "GLOBAL", ""],
                    "description": "Filter by scope (empty returns all accessible)",
                },
            },
        },
    },
]


# --- Tool dispatch ---

async def handle_tool_call(name: str, arguments: dict) -> str:
    """Handle a tool call and return the result as text."""
    # RBAC gate — block tools that require elevated permissions
    rbac_error = _tool_permission_check(name, arguments)
    if rbac_error is not None:
        return f"PERMISSION DENIED: {rbac_error}"

    if name == "delegate_task":
        return await tool_delegate_task(
            arguments.get("workspace_id", ""),
            arguments.get("task", ""),
        )
    elif name == "delegate_task_async":
        return await tool_delegate_task_async(
            arguments.get("workspace_id", ""),
            arguments.get("task", ""),
        )
    elif name == "check_task_status":
        return await tool_check_task_status(
            arguments.get("workspace_id", ""),
            arguments.get("task_id", ""),
        )
    elif name == "send_message_to_user":
        return await tool_send_message_to_user(arguments.get("message", ""))
    elif name == "list_peers":
        return await tool_list_peers()
    elif name == "get_workspace_info":
        return await tool_get_workspace_info()
    elif name == "commit_memory":
        return await tool_commit_memory(
            arguments.get("content", ""),
            arguments.get("scope", "LOCAL"),
        )
    elif name == "recall_memory":
        return await tool_recall_memory(
            arguments.get("query", ""),
            arguments.get("scope", ""),
        )
    return f"Unknown tool: {name}"


# --- MCP Server (JSON-RPC over stdio) ---

async def _handle_stdio():
    """Run MCP server on stdio — reads JSON-RPC requests, writes responses."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, asyncio.get_event_loop())

    async def write_response(response: dict):
        data = json.dumps(response) + "\n"
        writer.write(data.encode())
        await writer.drain()

    buffer = ""
    while True:
        try:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buffer += chunk.decode(errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue

                req_id = request.get("id")
                method = request.get("method", "")

                if method == "initialize":
                    await write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "molecule", "version": "1.0.0"},
                        },
                    })

                elif method == "notifications/initialized":
                    pass  # No response needed

                elif method == "tools/list":
                    await write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"tools": TOOLS},
                    })

                elif method == "tools/call":
                    params = request.get("params", {})
                    tool_name = params.get("name", "")
                    tool_args = params.get("arguments", {})
                    result_text = await handle_tool_call(tool_name, tool_args)
                    await write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": result_text}],
                        },
                    })

                else:
                    await write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    })

        except Exception as e:
            logger.error(f"MCP server error: {e}")
            break


# --- HTTP/SSE Transport (for Hermes runtime) ---

# Per-connection pending request queue.
# Maps (connection-id) → asyncio.Queue of JSON-RPC responses.
_connection_queues: dict[str, asyncio.Queue] = {}
_connection_lock = asyncio.Lock()


async def _handle_http_mcp(request) -> dict | None:
    """Handle an incoming JSON-RPC request over HTTP. Returns the JSON-RPC response."""
    try:
        body = await request.json()
    except Exception:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

    req_id = body.get("id")
    method = body.get("method", "")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "molecule", "version": "1.0.0"},
            },
        }

    elif method == "notifications/initialized":
        return None  # No response needed

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        result_text = await handle_tool_call(tool_name, tool_args)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": result_text}]},
        }

    else:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


async def _run_http_server(port: int):
    """Run MCP server over HTTP/SSE — compatible with Hermes MCP-native agents."""
    try:
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.responses import JSONResponse, Response
    except ImportError:
        logger.error("HTTP transport requires starlette — already in molecule-runtime deps")
        return

    _connection_queues.clear()

    async def mcp_handler(request):
        """POST endpoint — receive and process JSON-RPC requests."""
        conn_id = request.headers.get("x-mcp-conn-id", "default")
        response = await _handle_http_mcp(request)
        if response is None:
            return Response(status_code=202)
        # Try to push via SSE; fall back to direct JSON response
        async with _connection_lock:
            queue = _connection_queues.get(conn_id)
        if queue is not None and not queue.full():
            await queue.put(response)
            return Response(status_code=202)
        # No SSE connection — return JSON directly (simpler clients)
        return JSONResponse(response)

    async def sse_handler(request):
        """GET endpoint — SSE stream for push-based responses."""
        conn_id = str(uuid.uuid4())  # full UUID to avoid collision across connections
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        async with _connection_lock:
            _connection_queues[conn_id] = queue

        async def event_stream():
            yield "event: connected\ndata: {}\n\n".format(json.dumps({"conn_id": conn_id}))
            try:
                while True:
                    response = await asyncio.wait_for(queue.get(), timeout=300)
                    yield f"event: message\ndata: {json.dumps(response)}\n\n"
                    # Emit a heartbeat when the queue is drained (connection alive but idle)
                    if queue.empty():
                        yield "event: heartbeat\ndata: null\n\n"
            except asyncio.TimeoutError:
                pass
            finally:
                async with _connection_lock:
                    _connection_queues.pop(conn_id, None)

        return Response(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def health_handler(_request):
        return JSONResponse({"ok": True, "transport": "http+sse", "port": port})

    app = Starlette(
        routes=[
            Route("/mcp", mcp_handler, methods=["POST"]),
            Route("/mcp/stream", sse_handler, methods=["GET"]),
            Route("/health", health_handler),
        ]
    )

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info(f"A2A MCP HTTP server listening on http://127.0.0.1:{port}/mcp")
    await server.serve()


async def main():  # pragma: no cover
    """Entry point — select transport from CLI args or default to stdio."""
    parser = argparse.ArgumentParser(description="A2A MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http"],
        help="Transport mode: stdio (default) or http (HTTP+SSE for Hermes)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9100,
        help="TCP port for HTTP transport (default 9100)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        await _run_http_server(args.port)
    else:
        await _handle_stdio()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
