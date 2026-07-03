"""Shared-contract guard for SubprocessA2AExecutor (tenant-agent BUG 3).

These tests enforce, at the SHARED SDK layer, the two things every subprocess
runtime adapter used to get wrong on its own:

  1. conversation HISTORY is injected into the task text (a 2nd turn keeps
     context), and
  2. the native session id is STABLE (workspace-keyed), so it does NOT rotate
     with the per-request context_id the a2a-sdk mints fresh each turn.

A new runtime that subclasses SubprocessA2AExecutor and implements only
run_agent() INHERITS both — it cannot silently drop them without overriding
execute(), which this suite also guards against.
"""

from types import SimpleNamespace

import pytest

from molecule_runtime.subprocess_executor import SubprocessA2AExecutor


# --------------------------------------------------------------------------- #
# Fakes: minimal shapes the shared helpers read.
# --------------------------------------------------------------------------- #
class _FakeMessage:
    def __init__(self, text):
        # Text-only dict parts; extract_attached_files returns [] for these.
        self.parts = [{"text": text}]
        self.metadata = {}


class _FakeContext:
    def __init__(self, text, history=None, context_id=None, task_id=None):
        self.message = _FakeMessage(text)
        self.request = SimpleNamespace(metadata={"history": history or []})
        self.metadata = {"history": history or []}
        self.context_id = context_id
        self.task_id = task_id


class _RecordingQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _StubExecutor(SubprocessA2AExecutor):
    """A runtime that ONLY shells out — everything else is inherited."""

    runtime_label = "Stub"

    def __init__(self, *, workspace_id="", heartbeat=None):
        super().__init__(workspace_id=workspace_id, heartbeat=heartbeat)
        self.seen_task_text = None
        self.seen_session_id = None

    async def run_agent(self, task_text, session_id, context):
        self.seen_task_text = task_text
        self.seen_session_id = session_id
        return f"stub reply for session {session_id}"


def _history(*pairs):
    return [{"role": role, "parts": [{"text": text}]} for role, text in pairs]


# --------------------------------------------------------------------------- #
# CONTRACT #1 — history injection.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_injects_history_into_task_text():
    ex = _StubExecutor(workspace_id="ws-abc")
    ctx = _FakeContext(
        "what did I just say?",
        history=_history(("user", "my name is Ada"), ("agent", "Hello Ada")),
    )
    await ex.execute(ctx, _RecordingQueue())

    assert ex.seen_task_text is not None
    # The current message AND the prior turns must be present in the task text.
    assert "what did I just say?" in ex.seen_task_text
    assert "my name is Ada" in ex.seen_task_text
    assert "Hello Ada" in ex.seen_task_text
    # build_task_text framing proves history was PREPENDED, not dropped.
    assert "Conversation so far:" in ex.seen_task_text


@pytest.mark.asyncio
async def test_execute_passes_bare_message_when_no_history():
    ex = _StubExecutor(workspace_id="ws-abc")
    await ex.execute(_FakeContext("hello"), _RecordingQueue())
    assert ex.seen_task_text == "hello"


# --------------------------------------------------------------------------- #
# CONTRACT #2 — stable, workspace-keyed session id.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_id_is_stable_across_fresh_context_ids():
    ex = _StubExecutor(workspace_id="ws-stable-123")
    # Two turns of the SAME conversation, each with a DIFFERENT context_id
    # (the a2a-sdk mints these fresh when the client threads none).
    await ex.execute(_FakeContext("turn 1", context_id="ctx-aaaa"), _RecordingQueue())
    first = ex.seen_session_id
    await ex.execute(_FakeContext("turn 2", context_id="ctx-bbbb"), _RecordingQueue())
    second = ex.seen_session_id

    assert first == second == "workspace:ws-stable-123", (
        "session id must be workspace-keyed and stable across per-request context_ids "
        "so the runtime's native session resumes (tenant-agent BUG 3)"
    )


def test_derive_session_id_prefers_workspace_over_request_ids():
    ex = _StubExecutor(workspace_id="ws-xyz")
    ctx = _FakeContext("m", context_id="ctx-1", task_id="task-1")
    assert ex.derive_session_id(ctx) == "workspace:ws-xyz"


def test_derive_session_id_falls_back_to_context_then_task_then_default():
    ex = _StubExecutor(workspace_id="")  # no workspace identity
    assert ex.derive_session_id(_FakeContext("m", context_id="ctx-1", task_id="task-1")) == "ctx-1"
    assert ex.derive_session_id(_FakeContext("m", context_id=None, task_id="task-1")) == "task-1"
    assert ex.derive_session_id(_FakeContext("m", context_id=None, task_id=None)) == "default"


def test_workspace_id_read_from_env_when_not_passed(monkeypatch):
    monkeypatch.setenv("WORKSPACE_ID", "env-ws-9")
    ex = _StubExecutor()  # no explicit workspace_id
    assert ex.derive_session_id(_FakeContext("m", context_id="ctx-z")) == "workspace:env-ws-9"


# --------------------------------------------------------------------------- #
# GUARD — a subprocess runtime cannot silently drop the contract.
# --------------------------------------------------------------------------- #
def test_run_agent_only_subclass_inherits_the_enforced_execute():
    # The whole point: a subclass implements ONLY run_agent and INHERITS the
    # base execute() that injects history + derives a stable session id. If a
    # future runtime overrides execute() it opts out of the shared contract —
    # this assertion is the tripwire that forces that decision to be explicit.
    assert _StubExecutor.execute is SubprocessA2AExecutor.execute


@pytest.mark.asyncio
async def test_missing_run_agent_fails_closed():
    class _NoRunAgent(SubprocessA2AExecutor):
        pass

    with pytest.raises(NotImplementedError):
        await _NoRunAgent(workspace_id="ws").run_agent("t", "s", _FakeContext("m"))


@pytest.mark.asyncio
async def test_empty_message_is_reported_not_dropped():
    ex = _StubExecutor(workspace_id="ws")
    q = _RecordingQueue()
    await ex.execute(_FakeContext(""), q)
    assert ex.seen_task_text is None  # run_agent never invoked
    assert len(q.events) == 1  # a "No message provided" event was enqueued
