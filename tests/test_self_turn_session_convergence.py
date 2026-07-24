"""Session convergence across restart / plugin-install / every self-wake.

THE expectation this pins (user, 2026-07-24): *across any reason a workspace
restarts or a plugin is installed, the agent's session stays the same* — one
Langfuse session, not a fresh one per boot / harvester / idle / delegation-result
tick.

Why this file exists — the gap a prior CI missed
=================================================
The platform (workspace-server) stamps ``contextId = canvas-<ws>`` on its OWN
self-turns (restart-context, first-boot) and its session-continuity test proves
that. But the *runtime's* own self-wakes (idle loop, delegation harvester, cron,
scheduler, goal-nudge, boot ``initial_prompt`` + reprovision announcement) are
POSTed to the LOCAL executor via ``build_message_send_params`` — which, before
this fix, set NO ``contextId``. The a2a-sdk then minted a FRESH ``context_id``
per self-wake, and ``TracingExecutor`` (session_id = ``context.context_id``)
filed each under its own throwaway Langfuse session. That is the "sessions still
turn into different sessions after a plugin install" fragmentation — and no
prior test exercised the runtime self-wake path, so it slipped through.

The two halves the convergence needs, tested end-to-end here:
  1. SEND side — ``build_message_send_params`` stamps the stable
     ``canvas-<ws>`` contextId on any self-* turn (:func:`test_*_stamps_*`).
  2. TRACE side — ``TracingExecutor`` turns that contextId into the trace
     session_id, so a self-wake and a canvas turn land in ONE session
     (:func:`test_self_wake_and_canvas_turn_share_one_session`).

Runtime-agnostic by construction: the fix lives in ``molecule_runtime`` (shared
by every runtime adapter — hermes / openclaw / codex / …), so this guards all of
them, not just hermes.
"""
import asyncio
import sys
import types

import pytest

from molecule_runtime import tracing
from molecule_runtime.a2a_client import (
    build_message_send_params,
    default_self_turn_context_id,
)

WS = "ea3cfcf1-cb9c-53b4-90fd-c53123569c4a"
CANVAS_SESSION = f"canvas-{WS}"

# Every self-wake reason the runtime injects. Each MUST converge on canvas-<ws>.
# (The literal strings are the source_type markers a2a_executor stamps; kept as
# literals here so this test does not depend on importing the full a2a-sdk to
# read the A2A_SOURCE_SELF_* constants in the stub unit-test env.)
SELF_WAKE_SOURCE_TYPES = [
    "self-idle",              # idle self-wake (main._run_idle_loop)
    "self-harvester",         # heartbeat delegation-results harvester
    "self-delegation-result", # mailbox-kernel delegation harvest turn
    "self-cron",              # platform cron self-tick
    "self-scheduler",         # mailbox scheduler tick
    "self-goal-nudge",        # goal / plan re-engagement nudge
    "self-lifecycle",         # boot initial_prompt + reprovision-wake announce
]


@pytest.fixture(autouse=True)
def _ws_env(monkeypatch):
    monkeypatch.setenv("WORKSPACE_ID", WS)
    monkeypatch.delenv("MOLECULE_DEFAULT_SESSION_CONTEXT_ID", raising=False)
    yield


# --- SEND side: the canonical builder stamps the stable session ----------------

@pytest.mark.parametrize("source_type", SELF_WAKE_SOURCE_TYPES)
def test_self_wake_stamps_stable_canvas_context_id(source_type):
    params = build_message_send_params(
        "wake up and check your work",
        metadata={"source_type": source_type},
    )
    assert params["message"].get("contextId") == CANVAS_SESSION, (
        f"{source_type} self-wake did not converge on the canvas session — it "
        f"would fragment into a fresh Langfuse session on every tick"
    )


def test_future_self_prefixed_kind_also_converges():
    """Any NEW self-* kind converges automatically — the rule keys on the
    ``self-`` prefix, not a per-kind allowlist that a future sender could miss."""
    params = build_message_send_params("x", metadata={"source_type": "self-brand-new"})
    assert params["message"].get("contextId") == CANVAS_SESSION


def test_peer_delegation_send_is_not_stamped():
    """A delegation to a PEER (parent_task_id metadata, no self- source_type)
    keeps its own per-conversation context — converging it would drag peer
    traffic into the human's session."""
    params = build_message_send_params(
        "do this task",
        metadata={"parent_task_id": "t-1", "source_workspace_id": WS},
    )
    assert "contextId" not in params["message"]


def test_plain_user_send_is_not_stamped():
    params = build_message_send_params("hello")
    assert "contextId" not in params["message"]


def test_explicit_context_id_wins_over_self_default():
    """A caller-supplied contextId (e.g. a user's rotated New-Session id threaded
    through) is never overridden by the self-turn default."""
    params = build_message_send_params(
        "x", metadata={"source_type": "self-idle"}, context_id="sess-rotated-123"
    )
    assert params["message"].get("contextId") == "sess-rotated-123"


def test_no_workspace_id_leaves_context_unset(monkeypatch):
    """Unit/CLI with no WORKSPACE_ID must not stamp a nonsense ``canvas-`` id —
    it leaves contextId unset (legacy behaviour), never ``canvas-``."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    assert default_self_turn_context_id() == ""
    params = build_message_send_params("x", metadata={"source_type": "self-idle"})
    assert "contextId" not in params["message"]


def test_platform_override_env_is_authoritative(monkeypatch):
    """If the platform hands the runtime the authoritative default-session id, it
    wins over the derived canvas-<ws> — so the convention has ONE authority."""
    monkeypatch.setenv("MOLECULE_DEFAULT_SESSION_CONTEXT_ID", "canvas-override-xyz")
    params = build_message_send_params("x", metadata={"source_type": "self-harvester"})
    assert params["message"].get("contextId") == "canvas-override-xyz"


# --- TRACE side + end-to-end convergence ---------------------------------------

def _install_fake_langfuse(monkeypatch):
    calls = {"trace": [], "flush": 0}

    class _Trace:
        def generation(self, **kw):
            return object()

        def span(self, **kw):
            pass

    class _LF:
        def __init__(self, **kw):
            pass

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


class _Part:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text, metadata=None):
        self.parts = [_Part(text)]
        self.metadata = metadata


class _Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _Inner:
    _last_tool_uses = []

    async def execute(self, context, event_queue):
        await event_queue.enqueue_event(_Msg("ok"))

    async def cancel(self, *a, **k):
        pass


class _Ctx:
    """A RequestContext-shaped stand-in that carries the a2a context_id the
    same way the sdk surfaces it (built FROM the message.contextId the sender
    stamped)."""

    def __init__(self, text, context_id, metadata=None):
        self.message = _Msg(text, metadata)
        self.context_id = context_id


@pytest.fixture(autouse=True)
def _reset_tracing():
    tracing._client = None
    yield
    tracing._drain(1.0)
    tracing._client = None


def _emit_session_for_self_turn(monkeypatch, source_type):
    """Full path: build the self-turn params (SEND side) → feed its stamped
    contextId into a RequestContext → run TracingExecutor → return the emitted
    trace session_id (TRACE side)."""
    calls = _install_fake_langfuse(monkeypatch)
    params = build_message_send_params("tick", metadata={"source_type": source_type})
    stamped = params["message"]["contextId"]  # what the sdk will set context_id to
    wrapped = tracing.wrap_executor(_Inner(), WS, "m")
    asyncio.run(wrapped.execute(_Ctx("tick", stamped), _Queue()))
    assert tracing._drain(), "off-loop trace worker did not finish"
    assert calls["trace"], "no trace emitted"
    return calls["trace"][0]["session_id"]


@pytest.mark.parametrize("source_type", SELF_WAKE_SOURCE_TYPES)
def test_every_self_wake_traces_into_the_canvas_session(monkeypatch, source_type):
    assert _emit_session_for_self_turn(monkeypatch, source_type) == CANVAS_SESSION


def test_self_wake_and_canvas_turn_share_one_session(monkeypatch):
    """THE end-to-end guarantee: a canvas user turn (contextId injected by the
    platform belt = canvas-<ws>) and a runtime self-wake (any reason) emit the
    SAME Langfuse session_id — so a restart / plugin-install / idle tick never
    starts a new session."""
    calls = _install_fake_langfuse(monkeypatch)
    wrapped = tracing.wrap_executor(_Inner(), WS, "m")

    # 1) A canvas turn as the platform belt delivers it.
    asyncio.run(wrapped.execute(_Ctx("hi from canvas", CANVAS_SESSION), _Queue()))
    # 2) A self-wake, built through the real sender then traced.
    self_params = build_message_send_params(
        "delegation results are ready", metadata={"source_type": "self-delegation-result"}
    )
    asyncio.run(wrapped.execute(_Ctx("results", self_params["message"]["contextId"]), _Queue()))
    assert tracing._drain(), "off-loop trace worker did not finish"

    sessions = {t["session_id"] for t in calls["trace"]}
    assert sessions == {CANVAS_SESSION}, (
        f"canvas turn and self-wake landed in different sessions: {sessions} — "
        f"the fragmentation this fix closes"
    )
