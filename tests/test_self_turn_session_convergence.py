"""Session convergence across restart / plugin-install / every self-wake.

THE expectation this pins (user, 2026-07-24): *across any reason a workspace
restarts or a plugin is installed, the agent's Langfuse session stays the same* —
one session, not a fresh one per boot / harvester / idle / delegation-result /
scheduler tick.

Design (corrected after the xhigh review, wf_fbccb212)
======================================================
Session convergence is decoupled from turn ROUTING. A runtime self-wake keeps its
OWN a2a ``context_id`` so the non-blocking inbox schedules it independently (it is
never fast-ack+dropped against, nor defers, the user's in-flight canvas turn, and
never compacts the shared thread). Only the Langfuse *trace* ``session_id`` is
converged, in ``tracing.TracingExecutor``: a turn whose ``source_type`` starts
with ``self-`` is filed under ``canvas-<wsid>`` regardless of its context_id.

Because the convergence reads the INBOUND ``RequestContext`` (not the outbound
builder), it is builder-agnostic — it also covers the trigger/scheduler SDK lane
(``channel_sdk.build_trigger_message_send_request``) that never routes through
``build_message_send_params``. That was the live regression the previous,
builder-side approach missed.

Runtime-agnostic: the convergence lives in shared ``molecule_runtime.tracing``
(the single funnel every adapter's executor is wrapped in), so it guards hermes /
openclaw / codex / claude-code alike.
"""
import asyncio
import sys
import types

import pytest

from molecule_runtime import tracing
from molecule_runtime.a2a_client import build_message_send_params

WS = "ea3cfcf1-cb9c-53b4-90fd-c53123569c4a"
CANVAS_SESSION = f"canvas-{WS}"

# Every self-wake reason the runtime injects. Each must TRACE into canvas-<ws>,
# INCLUDING self-scheduler — whose production turns are built by the trigger SDK
# lane, not build_message_send_params (the exact path the old fix missed).
SELF_WAKE_SOURCE_TYPES = [
    "self-idle",              # idle self-wake (main._run_idle_loop)
    "self-harvester",         # heartbeat delegation-results harvester
    "self-delegation-result", # mailbox-kernel delegation harvest turn
    "self-cron",              # platform cron self-tick
    "self-scheduler",         # trigger-daemon / mailbox scheduler tick (SDK lane)
    "self-goal-nudge",        # goal / plan re-engagement nudge
    "self-lifecycle",         # boot initial_prompt + reprovision-wake announce
]


# --- fakes ---------------------------------------------------------------------

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


# WHERE the sdk surfaces source_type. Production self-sends stamp it on the
# ENVELOPE (MessageSendParams(metadata=...)) and build the Message with NO
# metadata, so on the wire it arrives as context.metadata (the "envelope"
# placement) — that is the branch real self-wakes take. We also cover the
# message-level placement so both branches of the extraction ladder are guarded.
_PLACEMENTS = ("envelope", "message")


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
    """A RequestContext-shaped stand-in. context_id is what the a2a-sdk minted
    for THIS turn (self-wakes get their own, distinct from canvas-<ws>);
    source_type is placed exactly where the sdk surfaces it — on the ENVELOPE
    (context.metadata, the production self-send shape) by default, or on
    message.metadata when placement="message"."""

    def __init__(self, text, context_id, source_type=None, placement="envelope"):
        md = {"source_type": source_type} if source_type is not None else None
        if placement == "message":
            self.message = _Msg(text, md)
            self.metadata = None
        else:  # "envelope" — production: message carries NO metadata
            self.message = _Msg(text, None)
            self.metadata = md
        self.context_id = context_id


@pytest.fixture(autouse=True)
def _reset_tracing():
    tracing._client = None
    yield
    tracing._drain(1.0)
    tracing._client = None


def _emit_session(monkeypatch, *, context_id, source_type, placement="envelope"):
    calls = _install_fake_langfuse(monkeypatch)
    wrapped = tracing.wrap_executor(_Inner(), WS, "m")
    asyncio.run(wrapped.execute(_Ctx("tick", context_id, source_type, placement), _Queue()))
    assert tracing._drain(), "off-loop trace worker did not finish"
    assert calls["trace"], "no trace emitted"
    return calls["trace"][0]["session_id"]


# --- convergence: every self-wake traces into the canvas session ---------------

@pytest.mark.parametrize("placement", _PLACEMENTS)
@pytest.mark.parametrize("source_type", SELF_WAKE_SOURCE_TYPES)
def test_self_wake_with_its_own_context_traces_into_canvas_session(monkeypatch, source_type, placement):
    # The self-wake ran on its OWN minted context_id (NOT canvas-<ws>) — proving
    # routing stays independent — yet its trace is filed under canvas-<ws>.
    # Swept over BOTH metadata placements; "envelope" is the real production
    # shape (context.metadata), the branch the previous test never exercised.
    own_ctx = f"ctx-{source_type}-{placement}-9f3a1b"
    assert own_ctx != CANVAS_SESSION
    assert _emit_session(monkeypatch, context_id=own_ctx, source_type=source_type, placement=placement) == CANVAS_SESSION


def test_scheduler_lane_converges_even_though_it_bypasses_the_builder(monkeypatch):
    # Regression guard for the xhigh-review finding: self-scheduler turns are
    # built by the trigger SDK (no contextId stamped) and mint their own
    # context_id. Convergence is builder-agnostic (trace-side), so they STILL
    # land in the canvas session.
    assert _emit_session(monkeypatch, context_id="ctx-trigger-daemon-tick", source_type="self-scheduler") == CANVAS_SESSION


def test_future_self_prefixed_kind_also_converges(monkeypatch):
    assert _emit_session(monkeypatch, context_id="ctx-brand-new", source_type="self-brand-new") == CANVAS_SESSION


def test_canvas_turn_traces_under_its_own_context(monkeypatch):
    # A real canvas turn already carries contextId=canvas-<ws> (belt/client) and
    # no self- source_type: it traces under that id — same session as the wakes.
    assert _emit_session(monkeypatch, context_id=CANVAS_SESSION, source_type=None) == CANVAS_SESSION


def test_peer_turn_keeps_its_own_session(monkeypatch):
    # A peer-agent turn is NOT a self-wake: it must keep its own per-conversation
    # session, never be dragged into the human's canvas session.
    assert _emit_session(monkeypatch, context_id="peer-conv-abc", source_type="peer") == "peer-conv-abc"


def test_self_wake_and_canvas_turn_share_one_session(monkeypatch):
    """THE end-to-end guarantee: a canvas user turn and a runtime self-wake (on
    its own distinct context_id) emit the SAME Langfuse session_id — so a
    restart / plugin-install / idle / scheduler tick never starts a new session."""
    calls = _install_fake_langfuse(monkeypatch)
    wrapped = tracing.wrap_executor(_Inner(), WS, "m")
    asyncio.run(wrapped.execute(_Ctx("hi from canvas", CANVAS_SESSION, None), _Queue()))
    asyncio.run(wrapped.execute(_Ctx("results", "ctx-self-wake-xyz", "self-delegation-result"), _Queue()))
    assert tracing._drain(), "off-loop trace worker did not finish"
    sessions = {t["session_id"] for t in calls["trace"]}
    assert sessions == {CANVAS_SESSION}, f"self-wake and canvas turn diverged: {sessions}"


# --- routing decoupling: the builder must NOT stamp contextId ------------------

@pytest.mark.parametrize("source_type", SELF_WAKE_SOURCE_TYPES)
def test_builder_does_not_stamp_contextid_on_self_turns(source_type):
    # The convergence is trace-side ONLY. build_message_send_params must leave
    # contextId UNSET so every self-wake keeps its own routing context (the fix
    # for the fast-ack-drop / canvas-deferral regressions).
    params = build_message_send_params("wake", metadata={"source_type": source_type})
    assert "contextId" not in params["message"], (
        f"{source_type}: builder must not stamp contextId — self-wakes keep their "
        f"own routing context; convergence is trace-side only"
    )


def test_builder_does_not_stamp_contextid_on_plain_send():
    params = build_message_send_params("hello")
    assert "contextId" not in params["message"]
