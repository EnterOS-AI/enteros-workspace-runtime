# Platform tool registry

Single source of truth for every tool the platform exposes to agents
(A2A delegation, hierarchical memory, broadcast, introspection).

## Why this exists

Before the standalone runtime became the source of truth, three monorepo
surfaces independently declared each tool:

1. **MCP server** — the JSON tool list
2. **Runtime-specific wrappers** — framework-native tool declarations
3. **Agent-facing system-prompt docs** — names and usage guidance

Adding a tool to one and forgetting the others happened repeatedly. The
canonical case: `send_message_to_user` was registered in MCP TOOLS but
the executor_helpers doc string never mentioned it, so agents saw the
tool as available but had no usage guidance — a silent capability
regression.

## What the registry does

`registry.py` defines each tool ONCE as a frozen `ToolSpec`:

```python
ToolSpec(
    name="delegate_task",
    short="Delegate a task to a peer workspace via A2A and WAIT for the response.",
    when_to_use="Use for QUICK questions and small sub-tasks where you can afford to wait inline...",
    input_schema={...},          # JSON Schema, consumed by MCP server
    impl=tool_delegate_task,     # the actual coroutine
    section="a2a",               # which prompt section it belongs to
)
```

The current shared surfaces consume the registry directly:

- **MCP/OpenAI contract and dispatcher** (`molecule_runtime/mcp_tools.py`)
  derives schemas and dispatch from `TOOLS`.
- **MCP server** (`molecule_runtime/a2a_mcp_server.py`) re-exports that shared
  contract rather than declaring a second list.
- **Doc generator** (`molecule_runtime/executor_helpers.py`) produces the
  system-prompt block from `spec.short` (bullet) + `spec.when_to_use`
  (heading + paragraph).

Framework-specific wrappers under `molecule_runtime/builtin_tools/` may expose
a narrower native surface. They delegate implementation to the shared handlers,
but they are not a second source for the universal MCP schema.

## CLI subprocess block — special case

Non-MCP custom subprocess adapters use a separate
hand-maintained block in `executor_helpers._A2A_INSTRUCTIONS_CLI` because
the CLI subcommand vocabulary (`peers`, `delegate`, `status`, `info`)
differs from the MCP tool names (`list_peers`, `delegate_task`, etc.).
Auto-generation would lose the readable invocation syntax.

Alignment is enforced via `_CLI_A2A_COMMAND_KEYWORDS` (in
`executor_helpers.py`): every a2a-section spec must be keyed there with
either a CLI subcommand keyword OR an explicit `None` if the tool is
intentionally not exposed via subprocess (e.g.
`send_message_to_user` because its structured `attachments` field
doesn't survive positional-arg shell invocation).

## Tests that catch drift

The active drift gates are:

| File | What it catches |
|---|---|
| `tests/test_mcp_ssot.py` | MCP and OpenAI adapter schemas diverging from the registry |
| `tests/test_executor_helpers.py` | generated A2A/display guidance missing a registered tool |
| `tests/test_current_operator_guidance.py` | contributor guidance pointing at retired paths or fixed tool counts |

## Adding a new tool

1. Append a `ToolSpec(...)` to `TOOLS` in `registry.py`.
2. Update `_CLI_A2A_COMMAND_KEYWORDS` in `molecule_runtime/executor_helpers.py` — set the
   value to the CLI subcommand keyword, or to `None` if the tool isn't
   exposed via the subprocess interface.
3. Add or update a framework-specific wrapper only when that framework exposes
   the capability outside the universal MCP server.
4. Run `pytest -q tests/test_mcp_ssot.py tests/test_executor_helpers.py`.

## Renaming a tool

Edit `name` in `registry.py` only. Then:

1. The MCP TOOLS list rebuilds automatically.
2. The doc generator regenerates automatically (snapshots will fail
   the diff — regenerate them).
3. Search `molecule_runtime/` and `tests/` for the old literal in case a non-adapter
   consumer (tests, plugin code) hardcoded the old name; update those.
4. Update any `_CLI_A2A_COMMAND_KEYWORDS` key + the literal substring
   in `_A2A_INSTRUCTIONS_CLI` if applicable.

## Removing a tool

Delete the `ToolSpec` and the `_CLI_A2A_COMMAND_KEYWORDS` key. Adapters
and doc generators stop registering it automatically; the structural
tests prevent stale references from surviving.
