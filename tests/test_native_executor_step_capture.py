"""Native-runtime (RuntimeA2AExecutor) Langfuse step capture — issue #305.

The executor already builds a per-turn ``tool_trace`` from the astream
on_tool_start/on_tool_end events; ``_tooltrace_to_trace_shapes`` maps it onto
the SSOT AgentTrace capture attributes (``_last_tool_uses`` / ``_last_tool_calls``
/ ``_last_steps``) that ``molecule_runtime.tracing.TracingExecutor`` reads off
the wrapped inner. These pin the mapping AND prove the captured shape conforms
to the vendored agent-trace schema (so a native-runtime turn's tool calls reach
the Traces tab, not empty steps).
"""
from molecule_runtime.a2a_executor import _tooltrace_to_trace_shapes


def test_maps_tooltrace_to_ordered_shapes():
    tool_trace = [
        {"tool": "list_peers", "input": "{}", "output_preview": "3 peers"},
        {"tool": "delegate_task", "input": "{'task': 'x'}", "output_preview": "queued"},
    ]
    tool_uses, tool_calls, steps = _tooltrace_to_trace_shapes(tool_trace)

    # order preserved across all three shapes
    assert tool_uses == ["list_peers", "delegate_task"]
    assert tool_calls == [
        {"name": "list_peers", "input": "{}", "output": "3 peers"},
        {"name": "delegate_task", "input": "{'task': 'x'}", "output": "queued"},
    ]
    assert steps == [
        {"kind": "tool_call", "name": "list_peers", "input": "{}", "result": "3 peers"},
        {"kind": "tool_call", "name": "delegate_task",
         "input": "{'task': 'x'}", "result": "queued"},
    ]


def test_empty_tooltrace_yields_empty_shapes():
    # A tool-less turn captures nothing (the executor resets per turn).
    assert _tooltrace_to_trace_shapes([]) == ([], [], [])


def test_missing_output_preview_becomes_empty_result():
    # on_tool_start fired but on_tool_end never paired (e.g. interrupted turn):
    # the entry has no output_preview → result is "" (not KeyError).
    tool_uses, tool_calls, steps = _tooltrace_to_trace_shapes(
        [{"tool": "Bash", "input": "ls"}]
    )
    assert tool_uses == ["Bash"]
    assert tool_calls == [{"name": "Bash", "input": "ls", "output": ""}]
    assert steps == [{"kind": "tool_call", "name": "Bash", "input": "ls", "result": ""}]


def test_captured_steps_conform_to_ssot_agent_trace_schema():
    """The captured shape must validate against the vendored SSOT schema — this
    is what guarantees the native runtime's tool calls actually render in the
    Traces tab rather than being silently dropped as a contract violation."""
    import json
    from importlib import resources

    import jsonschema

    from molecule_runtime import tracing

    schema = json.loads(
        resources.files("molecule_runtime")
        .joinpath("contracts/agent-trace.schema.json")
        .read_text(encoding="utf-8")
    )
    tool_trace = [
        {"tool": "list_peers", "input": "{}", "output_preview": "[]"},
        {"tool": "send_message", "input": "{'to': 'ws-2'}", "output_preview": "sent"},
    ]
    tool_uses, _tool_calls, steps = _tooltrace_to_trace_shapes(tool_trace)
    trace = tracing.build_agent_trace(
        workspace_id="ws-1",
        model="anthropic/claude-opus-4-8",
        user_input="who is around?",
        output="Just me.",
        tool_uses=tool_uses,
        steps=steps,
    )
    jsonschema.validate(trace, schema)
    assert [s["kind"] for s in trace["steps"]] == ["tool_call", "tool_call"]
    assert trace["tool_uses"] == ["list_peers", "send_message"]
