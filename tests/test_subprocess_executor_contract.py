"""Shared-contract guard for SubprocessA2AExecutor (tenant-agent BUG 3).

These tests enforce, at the SHARED SDK layer, the session contract every
subprocess runtime adapter used to get wrong on its own:

  * the native session id is STABLE (workspace-keyed), so it does NOT rotate
    with the per-request context_id the a2a-sdk mints fresh each turn — the
    runtime's native SessionManager RESUMES the same session, which is where
    continuity comes from.

The base deliberately passes ONLY the current user message to run_agent: it does
NOT force-inject metadata.history into the task text (that double-fed context and
grew the prompt unboundedly). These tests pin BOTH properties — the stable
session id, and the ABSENCE of history injection.

A new runtime that subclasses SubprocessA2AExecutor and implements only
run_agent() INHERITS the contract — it cannot silently drop it without overriding
execute(), which this suite also guards against.
"""

from types import SimpleNamespace

import pytest

from molecule_runtime.subprocess_executor import SubprocessA2AExecutor


@pytest.fixture(autouse=True)
def _isolate_workspace_id(monkeypatch):
    """Deterministic WORKSPACE_ID resolution regardless of test order.

    ``platform_auth.get_workspace_id`` caches the validated WORKSPACE_ID in a
    module global on first read; an EARLIER test in the full suite can populate
    that cache (and the ambient env) with a different value. The derive_session_id
    fallback + env-read assertions below both flow through that cache, so reset it
    and clear the ambient env before each test — otherwise the leaked value shadows
    the per-test setup (seen as ``workspace:00…0001 != ctx-1`` in CI). Mirrors the
    a2a_client cache reset already in tests/conftest.py.
    """
    from molecule_runtime import platform_auth

    platform_auth._reset_workspace_id_cache()
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    yield
    platform_auth._reset_workspace_id_cache()


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
# CONTRACT #1 — NO history injection: only the current message is passed through.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_does_not_inject_history_into_task_text():
    # Even when metadata carries prior turns, the base must NOT prepend them:
    # continuity is the runtime's native session (resumed via the stable
    # session id), not a force-injected transcript (tenant-agent BUG 3 fix).
    ex = _StubExecutor(workspace_id="ws-abc")
    ctx = _FakeContext(
        "what did I just say?",
        history=_history(("user", "my name is Ada"), ("agent", "Hello Ada")),
    )
    await ex.execute(ctx, _RecordingQueue())

    assert ex.seen_task_text is not None
    # ONLY the current message reaches the runtime — verbatim.
    assert ex.seen_task_text == "what did I just say?"
    # The prior turns must NOT be prepended, and none of the old build_task_text
    # framing ("Conversation so far:") may leak in.
    assert "my name is Ada" not in ex.seen_task_text
    assert "Hello Ada" not in ex.seen_task_text
    assert "Conversation so far:" not in ex.seen_task_text


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
    # base execute() that derives a stable, workspace-keyed session id (and does
    # NOT force-inject history). If a future runtime overrides execute() it opts
    # out of the shared contract — this assertion is the tripwire that forces
    # that decision to be explicit.
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
