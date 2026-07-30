"""Unit tests for molecule_runtime.tracing — the SSOT Langfuse trace producer.

Covers the fail-open contract (no config → no-op, zero overhead) and the
enabled path (executor wrapped, turn input/output + consolidated system prompt
emitted as a Langfuse trace+generation, tagged with the workspace id).
"""
import asyncio
import sys
import types

import pytest

from molecule_runtime import tracing


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Reset ALL module-global stash (client + prompt + component maps) before
    # AND after each test, so cross-test ordering can't leak state — e.g. a
    # prior test's _system_components must not flip _emit onto the labeled path.
    tracing._client = None
    tracing._system_prompts.clear()
    tracing._system_components.clear()
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
              "MOLECULE_WORKSPACE_LANGFUSE_HOST"):
        monkeypatch.delenv(k, raising=False)
    yield
    tracing._drain(1.0)  # let any off-loop trace worker settle before next test
    tracing._client = None
    tracing._system_prompts.clear()
    tracing._system_components.clear()


class _Inner:
    """Minimal fake executor that enqueues a reply, like a real adapter."""

    def __init__(self):
        self._last_tool_uses = ["create_workspace"]
        self.cancelled = False

    async def execute(self, context, event_queue):
        await event_queue.enqueue_event(_Msg([_Part("hello "), _Part("world")]))

    async def cancel(self, *a, **k):
        self.cancelled = True


class _Part:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, parts):
        self.parts = parts


class _Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _Ctx:
    def __init__(self, text):
        self.message = _Msg([_Part(text)])


def test_disabled_by_default_is_noop():
    assert tracing.enabled() is False
    inner = _Inner()
    # wrap returns the SAME object unchanged when Langfuse is unset.
    assert tracing.wrap_executor(inner, "ws-1", "claude") is inner


def test_record_system_prompt_stashes():
    tracing.record_system_prompt("ws-1", "SYS PROMPT")
    assert tracing._system_prompts["ws-1"] == "SYS PROMPT"
    # blank workspace id / prompt are ignored, never raise.
    tracing.record_system_prompt("", "x")
    tracing.record_system_prompt("ws-2", "")
    assert "ws-2" not in tracing._system_prompts


def _install_fake_langfuse(monkeypatch):
    calls = {"trace": [], "generation": [], "span": [], "flush": 0}

    class _Gen:
        pass

    class _Trace:
        def generation(self, **kw):
            calls["generation"].append(kw)
            return _Gen()

        def span(self, **kw):
            calls["span"].append(kw)

    class _LF:
        def __init__(self, **kw):
            calls["init"] = kw

        def trace(self, **kw):
            calls["trace"].append(kw)
            return _Trace()

        def flush(self):
            calls["flush"] += 1

    fake = types.ModuleType("langfuse")
    fake.Langfuse = _LF
    monkeypatch.setitem(sys.modules, "langfuse", fake)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")
    return calls


def test_enabled_wraps_and_emits(monkeypatch):
    calls = _install_fake_langfuse(monkeypatch)
    assert tracing.enabled() is True

    tracing.record_system_prompt("ws-1", "CONSOLIDATED SYSTEM PROMPT")
    inner = _Inner()
    wrapped = tracing.wrap_executor(inner, "ws-1", "claude-opus-4-8")
    assert isinstance(wrapped, tracing.TracingExecutor)

    q = _Queue()
    asyncio.run(wrapped.execute(_Ctx("set up a marketing team"), q))

    # Inner still enqueued its reply (delegation intact).
    assert len(q.events) == 1
    assert tracing._drain(), "off-loop trace worker did not finish"
    # A trace was emitted, tagged with the workspace id (the /traces filter).
    assert calls["trace"], "expected a trace to be emitted"
    assert calls["trace"][0]["tags"] == ["ws-1"]
    assert calls["trace"][0]["input"] == "set up a marketing team"
    assert calls["trace"][0]["output"] == "hello world"
    # The generation carries the consolidated system prompt + user message + model.
    gen = calls["generation"][0]
    assert gen["model"] == "claude-opus-4-8"
    assert gen["input"][0] == {"role": "system", "content": "CONSOLIDATED SYSTEM PROMPT"}
    assert gen["input"][1] == {"role": "user", "content": "set up a marketing team"}
    assert gen["output"] == "hello world"
    assert gen["metadata"]["tool_uses"] == ["create_workspace"]
    assert calls["flush"] == 1


def test_decomposed_prompt_and_tool_spans(monkeypatch):
    calls = _install_fake_langfuse(monkeypatch)

    # Labeled prompt pieces (Enhancement A) + structured tool calls (Enhancement B).
    tracing.record_system_prompt(
        "ws-1",
        "FULL CONSOLIDATED PROMPT",
        components=[
            {"label": "base_platform_identity", "text": "You are a workspace..."},
            {"label": "role_prompt_files", "text": "You are the Org Concierge."},
            {"label": "a2a_instructions", "text": "Use delegate_task..."},
        ],
    )

    class _ToolInner(_Inner):
        def __init__(self):
            super().__init__()
            self._last_tool_calls = [
                {"name": "list_peers", "input": {}, "output": "3 peers"},
                {"name": "delegate_task", "input": {"task": "x"}, "output": "queued"},
            ]

    inner = _ToolInner()
    wrapped = tracing.wrap_executor(inner, "ws-1", "claude-opus-4-8")
    q = _Queue()
    asyncio.run(wrapped.execute(_Ctx("set up a team"), q))
    assert tracing._drain(), "off-loop trace worker did not finish"

    # Generation input = one labeled system message per component + the user msg.
    gen = calls["generation"][0]
    sys_msgs = [m for m in gen["input"] if m["role"] == "system"]
    assert len(sys_msgs) == 3, gen["input"]
    assert "base_platform_identity" in sys_msgs[0]["content"]
    assert "role_prompt_files" in sys_msgs[1]["content"]
    assert gen["input"][-1] == {"role": "user", "content": "set up a team"}
    assert gen["metadata"]["prompt_component_labels"] == [
        "base_platform_identity", "role_prompt_files", "a2a_instructions",
    ]

    # One span per tool call, with name + input + output. Spans are wired from
    # the canonical (string-coerced) at["steps"], NOT the raw tool_calls, so the
    # dict args reach the backend serialized — matching the SSOT schema, which
    # types step.input as a string.
    assert len(calls["span"]) == 2, calls["span"]
    names = [s["name"] for s in calls["span"]]
    assert names == ["tool:list_peers", "tool:delegate_task"]
    assert calls["span"][0]["output"] == "3 peers"
    assert calls["span"][1]["input"] == "{'task': 'x'}"
    assert isinstance(calls["span"][1]["input"], str)


def test_ordered_steps_thinking_and_tools(monkeypatch):
    calls = _install_fake_langfuse(monkeypatch)
    tracing.record_system_prompt("ws-1", "P", components=[{"label": "base", "text": "x"}])

    class _StepInner(_Inner):
        def __init__(self):
            super().__init__()
            # Ordered: reason → call tool → reason again (the SSOT AgentTrace.steps)
            self._last_steps = [
                {"kind": "thinking", "text": "I should list peers first."},
                {"kind": "tool_call", "name": "list_peers", "input": "{}", "result": "[]"},
                {"kind": "thinking", "text": "No peers — I'll explain my role."},
            ]

    wrapped = tracing.wrap_executor(_StepInner(), "ws-1", "m")
    asyncio.run(wrapped.execute(_Ctx("who are you"), _Queue()))
    assert tracing._drain(), "off-loop trace worker did not finish"

    spans = calls["span"]
    assert len(spans) == 3, spans
    # order preserved: thinking, tool, thinking
    assert spans[0]["name"] == "thinking"
    assert spans[0]["output"] == "I should list peers first."
    assert spans[1]["name"] == "tool:list_peers"
    assert spans[1]["input"] == "{}"
    assert spans[1]["output"] == "[]"          # result surfaced when the runtime has it
    assert spans[2]["name"] == "thinking"


def test_failopen_when_emit_raises(monkeypatch):
    _install_fake_langfuse(monkeypatch)

    # Make the client's trace() blow up — the turn must still complete.
    def _boom(**kw):
        raise RuntimeError("langfuse down")

    tracing._client = None
    assert tracing.enabled() is True
    tracing._langfuse().trace = _boom  # type: ignore[attr-defined]

    inner = _Inner()
    wrapped = tracing.wrap_executor(inner, "ws-1", "m")
    q = _Queue()
    # Must not raise despite the failing trace emit.
    asyncio.run(wrapped.execute(_Ctx("hi"), q))
    assert len(q.events) == 1


def test_cancel_delegates(monkeypatch):
    _install_fake_langfuse(monkeypatch)
    inner = _Inner()
    wrapped = tracing.wrap_executor(inner, "ws-1", "m")
    asyncio.run(wrapped.cancel())
    assert inner.cancelled is True


def test_slow_flush_does_not_block_the_turn(monkeypatch):
    """Regression (SDK #65 review): a slow/hung Langfuse flush must NOT stall
    the executor / A2A delivery. Emission runs off the turn, so execute()
    returns promptly even while flush is blocked."""
    import threading
    import time

    release = threading.Event()

    class _Trace:
        def generation(self, **kw):
            return object()

        def span(self, **kw):
            pass

    class _SlowLF:
        def __init__(self, **kw):
            pass

        def trace(self, **kw):
            return _Trace()

        def flush(self):
            release.wait(timeout=5)  # simulates a slow/hung backend flush

    fake = types.ModuleType("langfuse")
    fake.Langfuse = _SlowLF
    monkeypatch.setitem(sys.modules, "langfuse", fake)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")

    wrapped = tracing.wrap_executor(_Inner(), "ws-slow", "m")
    q = _Queue()
    t0 = time.monotonic()
    asyncio.run(wrapped.execute(_Ctx("hi"), q))  # must not wait on flush
    elapsed = time.monotonic() - t0
    assert len(q.events) == 1                    # reply delivered
    assert elapsed < 0.5, f"turn blocked on slow flush ({elapsed:.2f}s)"
    release.set()
    tracing._drain()


# ---------------------------------------------------------------------------
# SSOT conformance — the producer's canonical record matches the vendored
# agent-trace schema, and the vendored golden example does too. Closes the
# drift loop with check-schemas-in-sync.sh (which keeps the vendored copy
# byte-identical to molecule-ai-sdk main).
# ---------------------------------------------------------------------------


def _vendored(name):
    import json
    from importlib import resources

    text = resources.files("molecule_runtime").joinpath(f"contracts/{name}").read_text(
        encoding="utf-8"
    )
    return json.loads(text)


def test_build_agent_trace_conforms_to_vendored_schema():
    import jsonschema

    schema = _vendored("agent-trace.schema.json")
    tracing._system_prompts["ws-1"] = "CONSOLIDATED SYSTEM PROMPT"
    trace = tracing.build_agent_trace(
        workspace_id="ws-1",
        model="anthropic/claude-opus-4-8",
        user_input="who are you?",
        output="I'm the concierge.",
        tool_uses=["list_peers", "delegate_task"],
        steps=[
            {"kind": "thinking", "text": "list peers first"},
            {"kind": "tool_call", "name": "list_peers", "input": "{}", "result": "[]"},
        ],
        session_id="sess-1",
    )
    # The producer's emitted shape validates against the SSOT contract.
    jsonschema.validate(trace, schema)
    assert trace["workspace_id"] == "ws-1"          # load-bearing routing tag
    assert trace["system_prompt"] == "CONSOLIDATED SYSTEM PROMPT"
    assert [s["kind"] for s in trace["steps"]] == ["thinking", "tool_call"]


def test_minimal_trace_only_workspace_id_conforms():
    import jsonschema

    schema = _vendored("agent-trace.schema.json")
    # A producer that captured nothing but the workspace still emits a routable
    # trace (only workspace_id is required).
    trace = tracing.build_agent_trace("ws-9", "", "", "", None, None)
    jsonschema.validate(trace, schema)
    assert set(trace) <= {"workspace_id", "name"}


def test_vendored_golden_example_conforms():
    import jsonschema

    schema = _vendored("agent-trace.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(_vendored("agent-trace.contract.json"), schema)


def test_tool_calls_fallback_dict_input_is_coerced_and_conformant():
    """Regression (#252 review, findings b+c): the tool_calls fallback path
    passes raw dict `input` args. build_agent_trace MUST serialize them to
    strings so the emitted record validates against the SSOT schema (which
    types step.input as a string) — previously the dict leaked through and
    the 'conformance' was hollow because the wire diverged from the record."""
    import jsonschema

    schema = _vendored("agent-trace.schema.json")
    trace = tracing.build_agent_trace(
        workspace_id="ws-1",
        model="m",
        user_input="hi",
        output="ok",
        tool_uses=["delegate_task"],
        # kind of step the tool_calls fallback synthesizes: dict input, no str.
        steps=[{"kind": "tool_call", "name": "delegate_task",
                "input": {"task": "x", "n": 3}, "result": "queued"}],
    )
    jsonschema.validate(trace, schema)  # would raise if input stayed a dict
    step = trace["steps"][0]
    assert isinstance(step["input"], str) and "task" in step["input"]
    assert step["result"] == "queued"


def test_submit_offloop_releases_slot_when_submit_raises(monkeypatch):
    """Regression (#252 review, finding a): if executor.submit() raises after
    the in-flight slot is reserved, the slot MUST be released — otherwise a
    transient failure permanently shrinks capacity until every trace is
    dropped. The turn is never affected (fully fail-open)."""
    class _BoomExecutor:
        def submit(self, *a, **k):
            raise RuntimeError("interpreter shutting down")

    monkeypatch.setattr(tracing, "_executor", _BoomExecutor())
    tracing._inflight = 0
    tracing._submit_offloop(lambda: None)  # must not raise
    assert tracing._inflight == 0, "in-flight slot leaked on submit failure"


def test_session_id_flows_to_trace(monkeypatch):
    """The a2a conversation id (context_id) is captured and set as the trace
    session_id so multiple turns group into one Traces-tab session."""
    calls = _install_fake_langfuse(monkeypatch)

    class _CtxSess(_Ctx):
        def __init__(self, text, context_id):
            super().__init__(text)
            self.context_id = context_id

    wrapped = tracing.wrap_executor(_Inner(), "ws-1", "m")
    asyncio.run(wrapped.execute(_CtxSess("hi", "conv-abc"), _Queue()))
    assert tracing._drain(), "off-loop trace worker did not finish"
    assert calls["trace"][0]["session_id"] == "conv-abc"


# --- Prompt-consolidation COMPLETENESS (regression: runtime tracing gap) -----
#
# build_system_prompt previously recorded the Langfuse decomposition via a
# SECOND, self-contained re-derivation that silently OMITTED the capabilities
# preamble, MEMORY.md/USER.md snapshots, the delegation-failures block, and full
# skill instructions (it recorded skill NAMES only). The traced generation.input
# therefore misrepresented the prompt the model actually received. These tests
# pin the property that the recorded components are COMPLETE (cover the real
# prompt) and FAITHFUL (never contain content that isn't in the prompt). They
# FAIL against the old parallel-derivation code — the negative control.

def _write_cfg(tmp_path):
    (tmp_path / "system-prompt.md").write_text(
        "You are the Org Concierge. Ground decisions in live platform state."
    )
    (tmp_path / "MEMORY.md").write_text("- MEMORY_SENTINEL: prefer the leanest increment.")
    (tmp_path / "USER.md").write_text("# User\nUSER_SENTINEL: founder is CTO.")
    return str(tmp_path)


def _build_full_prompt(config_path, workspace_id="ws-complete"):
    from molecule_runtime import prompt as rt_prompt
    from molecule_runtime.skill_loader.loader import LoadedSkill, SkillMetadata

    def sk(name):
        return LoadedSkill(
            metadata=SkillMetadata(id=name, name=name, description=name + " skill"),
            instructions=f"SKILL_INSTRUCTIONS_SENTINEL for {name}.",
        )

    return rt_prompt.build_system_prompt(
        config_path=config_path,
        workspace_id=workspace_id,
        loaded_skills=[sk("git-ops"), sk("web-research")],
        peers=[{"name": "ops-agent", "workspace_id": "ws-ops", "status": "idle"}],
        prompt_files=["system-prompt.md"],
        plugin_rules=["PLUGIN_RULE_SENTINEL: never push to main."],
        plugin_prompts=["PLUGIN_GUIDELINE_SENTINEL: lean increments."],
        platform_instructions="PLATFORM_INSTRUCTIONS_SENTINEL: 5 workspaces.",
        a2a_mcp=True,
        platform_guardrail=True,
    )


def test_recorded_components_are_faithful_no_fabrication(tmp_path):
    cfg = _write_cfg(tmp_path)
    full = _build_full_prompt(cfg, "ws-faithful")
    comps = tracing._system_components.get("ws-faithful", [])
    assert comps, "no components recorded"
    # Every recorded component's text must be present verbatim in the prompt the
    # model actually received — Langfuse must never show content the LLM didn't see.
    for c in comps:
        assert c["text"] in full, f"component {c['label']!r} not a substring of the real prompt"


def test_recorded_components_cover_all_real_sections(tmp_path):
    cfg = _write_cfg(tmp_path)
    full = _build_full_prompt(cfg, "ws-complete")
    comps = tracing._system_components.get("ws-complete", [])
    joined = "\n".join(c["text"] for c in comps)

    # Each of these sentinels is in the real prompt; the decomposition must carry
    # it too. The four memory/skill/delegation/plugin sentinels are exactly what
    # the old parallel derivation dropped (memory snapshots, full skill
    # instructions) or never labeled (delegation block).
    for sentinel in (
        "MEMORY_SENTINEL",
        "USER_SENTINEL",
        "SKILL_INSTRUCTIONS_SENTINEL",
        "PLATFORM_INSTRUCTIONS_SENTINEL",
        "PLUGIN_RULE_SENTINEL",
        "PLUGIN_GUIDELINE_SENTINEL",
    ):
        assert sentinel in full, f"test fixture wrong: {sentinel} not in prompt"
        assert sentinel in joined, (
            f"{sentinel} is in the real prompt but MISSING from the traced "
            f"decomposition — Langfuse would misrepresent the consolidation"
        )
    # The delegation-failures block is unconditional; it must be labeled too.
    assert "Handling delegation failures" in joined
    # New complete label set includes the sections the old code dropped.
    labels = [c["label"] for c in comps]
    for lbl in ("memory_snapshots", "skills", "delegation_failures"):
        assert lbl in labels, f"missing component label {lbl}; got {labels}"


def test_memory_snapshot_named_in_prompt_files_labeled_by_its_SOURCE(tmp_path, monkeypatch):
    """AMENDED (was ``..._labeled_memory_not_role``, PR #313 follow-up).

    #313 labeled a memory basename NAMED in prompt_files as ``memory_snapshots``
    because RC #203 resolved it to its durable mailbox copy — so it really was
    memory. That resolution is what froze the openclaw persona (see
    tests/test_declared_role_file_not_frozen_by_mailbox_memory.py) and is now
    reverted: a DECLARED basename is served from ``/configs``, which is
    provisioner-authored and re-rendered every provision. Labeling THAT as
    ``memory_snapshots`` is the misattribution now — an operator auditing
    injected memory in /traces would be shown the role persona.

    #313's actual guarantee is unchanged and still pinned below: durable memory
    is never traced as the role prompt. The label now follows the SOURCE the
    text was read from rather than the basename.
    """
    import molecule_runtime.mailbox_dir as mailbox_dir
    from molecule_runtime import prompt as rt_prompt

    base = tmp_path / "workspace" / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    (base / "memory").mkdir(parents=True)
    cfg = tmp_path / "configs"
    cfg.mkdir()

    (cfg / "system-prompt.md").write_text("You are the Org Concierge.")
    (cfg / "MEMORY.md").write_text("- ROLE_NAMED_SENTINEL: provisioner-authored baseline.")
    # Durable memory a WRITER produced, on top of that baseline.
    (base / "memory" / "MEMORY.md").write_text(
        "- ROLE_NAMED_SENTINEL: provisioner-authored baseline.\n"
        "- MEM_NAMED_SENTINEL: prefer lean increments."
    )
    full = rt_prompt.build_system_prompt(
        config_path=str(cfg), workspace_id="ws-memnamed",
        loaded_skills=[], peers=[],
        prompt_files=["system-prompt.md", "MEMORY.md"],  # MEMORY.md NAMED here
    )
    comps = tracing._system_components.get("ws-memnamed", [])
    mem_text = "\n".join(c["text"] for c in comps if c["label"] == "memory_snapshots")
    role_text = "\n".join(c["text"] for c in comps if c["label"] == "role_prompt_files")
    assert "MEM_NAMED_SENTINEL" in full, "fixture wrong"
    # #313's load-bearing property, intact: durable memory is traced as memory.
    assert "MEM_NAMED_SENTINEL" in mem_text, "durable memory not traced under memory_snapshots"
    assert "MEM_NAMED_SENTINEL" not in role_text, "durable memory mislabeled as role_prompt_files"
    # New: the /configs-served declared copy is the ROLE slot and is labeled so.
    assert "ROLE_NAMED_SENTINEL" in role_text, "the /configs role copy must be traced as role"
    assert "ROLE_NAMED_SENTINEL" not in mem_text, (
        "the param-rendered /configs copy is a role file — tracing it as "
        "memory_snapshots shows an operator the persona when they audit memory"
    )
    # role file is still its own labeled component
    assert "Org Concierge" in role_text


def test_declared_memory_basename_served_from_mailbox_is_labeled_memory(tmp_path, monkeypatch):
    """The mailbox FALLBACK arm: /configs has no copy of a declared memory
    basename, so the durable copy occupies the role slot. That text really is
    durable memory and must be traced as such."""
    import molecule_runtime.mailbox_dir as mailbox_dir
    from molecule_runtime import prompt as rt_prompt

    base = tmp_path / "workspace" / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    (base / "memory").mkdir(parents=True)
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "system-prompt.md").write_text("You are the Org Concierge.")
    (base / "memory" / "MEMORY.md").write_text("- MAILBOX_ONLY_SENTINEL: durable.")

    rt_prompt.build_system_prompt(
        config_path=str(cfg), workspace_id="ws-memfallback",
        loaded_skills=[], peers=[],
        prompt_files=["system-prompt.md", "MEMORY.md"],
    )
    comps = tracing._system_components.get("ws-memfallback", [])
    mem_text = "\n".join(c["text"] for c in comps if c["label"] == "memory_snapshots")
    role_text = "\n".join(c["text"] for c in comps if c["label"] == "role_prompt_files")
    assert "MAILBOX_ONLY_SENTINEL" in mem_text
    assert "MAILBOX_ONLY_SENTINEL" not in role_text
