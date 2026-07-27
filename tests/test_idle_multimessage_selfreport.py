"""Idle §5.5 multi-message proactive-reporting verification.

RFC §5.5 idle-behavior contract: on an idle wake the agent SELF-REPORTS
during the turn — it emits an upfront ACK ("No active work from you — I'm
picking up: <items>") and then a per-work-item status, each pushed via its
``send_message_to_user`` tool (→ core AgentMessageWriter → the platform
``/notify`` seam). The poster's final-reply forward (idle_digest/poster.py
step 4) is only a FALLBACK for the last-reply text — the PRIMARY delivery is
these in-turn self-reports. Until now no test asserted the multi-message
path actually reaches the user-notify seam; a regression that dropped one of
the proactive messages would have shipped silently.

WHAT THIS FILE COVERS (honest scope):

  1. ``test_idle_framing_carries_ack_and_per_item_directive`` — the behavior
     contract that TRAVELS WITH the idle prompt (frame_idle_prompt) actually
     instructs the ack-before-work + report-each-item-via-send_message_to_user
     protocol. This guards the prompt-driven contract (item (a)).

  2. ``test_send_message_to_user_delivers_multiple_proactive_messages`` —
     the REAL ``tool_send_message_to_user`` seam, invoked TWICE mid-turn
     (ack + one per-item status), delivers BOTH messages to the platform's
     ``/notify`` endpoint (item (b): the send-to-user path posts to /notify
     and is invocable multiple times within one turn).

  3. ``test_idle_selffire_scripted_agent_multimessage_reaches_notify`` — the
     composed end-to-end: the poster fires the idle wake over real HTTP; a
     SCRIPTED agent (the fake platform's /a2a handler) reacts by invoking the
     REAL ``tool_send_message_to_user`` twice (ack + per-item) — exactly the
     §5.5 self-report protocol — and BOTH land at /notify. The turn's final
     reply is ``(idle)`` (deliberate silence), proving the two delivered
     messages are the PROACTIVE self-reports and NOT the single final-reply
     fallback.

COVERAGE LIMIT (documented, not faked): the runtime unit-test env stubs the
heavy ``claude_agent_sdk`` / ``a2a`` executor, so we cannot drive a real LLM
turn that autonomously *chooses* to call send_message_to_user off the framed
prompt. The "agent" here is scripted — it deterministically performs the
protocol the contract prescribes. What is proven end-to-end is the WIRING:
the framing carries the directive, and the real send_message_to_user tool
delivers multiple proactive messages to the /notify seam mid-turn. What is
NOT proven here is that a given model, shown the framing, will emit the acks
(that is a model-behavior / eval concern, out of unit-test scope).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from molecule_runtime.idle_digest.contract import (
    IDLE_BEHAVIOR_CONTRACT,
    SYSTEM_IDLE_HEADER,
    frame_idle_prompt,
)
from molecule_runtime.idle_digest.poster import make_digest_poster

WS = "ws-idle-multimessage-1"

ACK_MESSAGE = "No active work from you — I'm picking up: rebuild, triage-inbox."
PER_ITEM_MESSAGE = "Done: rebuild — green in 42s."


# ---------------------------------------------------------------------------
# (1) framing carries the §5.5 ack + per-item self-report directive
# ---------------------------------------------------------------------------

def test_idle_framing_carries_ack_and_per_item_directive():
    """The idle-behavior contract prepended to every idle prompt must
    prescribe: (a) a one-line ACK before starting ("picking up"), and (b)
    report each item as it completes via ``send_message_to_user``. This is
    the prompt-driven half of the §5.5 delivery; if the directive regressed
    to a bare digest, the multi-message reporting would silently stop."""
    framed = frame_idle_prompt("<digest body>")
    assert framed.splitlines()[0] == SYSTEM_IDLE_HEADER
    # The behavior contract travels WITH the prompt.
    assert IDLE_BEHAVIOR_CONTRACT in framed
    # Ack-before-work directive.
    assert "picking up" in framed.lower()
    # Per-item reporting is routed through the send-to-user tool by name.
    assert "send_message_to_user" in framed
    # "report each" is the multi-message (not single-final-reply) instruction.
    assert "report each" in framed.lower()


# ---------------------------------------------------------------------------
# Fake platform: a ThreadingHTTPServer so a /notify POST fired from WITHIN
# the /a2a handler (the scripted agent's mid-turn self-report) is served
# concurrently instead of dead-locking a single-threaded server.
# ---------------------------------------------------------------------------

class _FakePlatform(BaseHTTPRequestHandler):
    captured: dict[str, list[dict]] = {"a2a": [], "notify": []}
    # When set, the /a2a handler runs this callback (the "scripted agent")
    # before returning the final reply. It receives the fake platform base
    # URL so it can drive the real send_message_to_user tool back at us.
    agent_script = None
    final_reply = "(idle)"

    def _base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/a2a"):
            type(self).captured["a2a"].append({"body": body})
            script = type(self).agent_script
            if script is not None:
                # The scripted agent self-reports mid-turn (ack + per-item)
                # via the REAL send_message_to_user tool, which POSTs to the
                # /notify route on THIS same server (served on another thread).
                script(self._base())
            resp = {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"parts": [{"kind": "text", "text": type(self).final_reply}]},
            }
        elif self.path.endswith("/notify"):
            type(self).captured["notify"].append({"body": body})
            resp = {"ok": True}
        else:  # pragma: no cover — unexpected route is a test bug
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture()
def fake_platform():
    _FakePlatform.captured = {"a2a": [], "notify": []}
    _FakePlatform.agent_script = None
    _FakePlatform.final_reply = "(idle)"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakePlatform)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        t.join(timeout=5)


@pytest.fixture()
def point_send_tool_at(monkeypatch):
    """Point the REAL send_message_to_user tool's platform resolves at a
    given base URL, so invoking it makes a genuine /notify POST to the fake
    platform (no network mock — the real tool code runs)."""
    import molecule_runtime.a2a_tools_messaging as m

    def _apply(base: str):
        monkeypatch.setattr(m, "_resolve_workspace_id", lambda: WS)
        monkeypatch.setattr(m, "_resolve_platform_url", lambda _ws: base)
        monkeypatch.setattr(m, "_auth_headers_for_heartbeat", lambda _ws: {})

    return _apply


def _headers(_ws: str) -> dict:
    return {"Authorization": "Bearer test", "X-Workspace-ID": _ws}


# ---------------------------------------------------------------------------
# (2) the real send_message_to_user seam delivers MULTIPLE proactive messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_to_user_delivers_multiple_proactive_messages(
    fake_platform, point_send_tool_at
):
    """Invoke the REAL ``tool_send_message_to_user`` twice within one turn
    (the ack, then a per-item status). BOTH must reach /notify, in order —
    proving the send-to-user path supports mid-turn multi-message proactive
    reporting, not just a single final reply."""
    from molecule_runtime.a2a_tools_messaging import tool_send_message_to_user

    point_send_tool_at(fake_platform)

    r1 = await tool_send_message_to_user(ACK_MESSAGE)
    r2 = await tool_send_message_to_user(PER_ITEM_MESSAGE)

    assert r1 == "Message sent to user"
    assert r2 == "Message sent to user"

    notifies = [n["body"]["message"] for n in _FakePlatform.captured["notify"]]
    assert notifies == [ACK_MESSAGE, PER_ITEM_MESSAGE], notifies
    # No a2a traffic here — this isolates the send-to-user seam.
    assert _FakePlatform.captured["a2a"] == []


# ---------------------------------------------------------------------------
# (3) composed end-to-end: idle wake → scripted agent self-reports → /notify
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_selffire_scripted_agent_multimessage_reaches_notify(
    fake_platform, point_send_tool_at
):
    """End-to-end wiring proof.

    The poster fires the idle wake (real message/send over HTTP). A scripted
    agent (the fake platform's /a2a handler) performs the §5.5 protocol: it
    calls the REAL ``send_message_to_user`` tool TWICE mid-turn (ack, then a
    per-item status), then returns ``(idle)`` as the turn's final reply.

    Asserts:
      * BOTH proactive self-reports reached /notify (multi-message delivery);
      * the outgoing idle prompt carried the §5.5 ack directive (framing);
      * the ``(idle)`` final reply was SUPPRESSED, so the two /notify posts
        are the proactive self-reports — NOT the single final-reply fallback.
    """
    point_send_tool_at(fake_platform)

    def _scripted_agent(base: str):
        # Runs on the /a2a handler thread; drive the async tool in a fresh
        # event loop for this thread. Real httpx → real /notify POSTs.
        from molecule_runtime.a2a_tools_messaging import tool_send_message_to_user

        async def _turn():
            await tool_send_message_to_user(ACK_MESSAGE)
            await tool_send_message_to_user(PER_ITEM_MESSAGE)

        asyncio.run(_turn())

    _FakePlatform.agent_script = staticmethod(_scripted_agent).__func__
    _FakePlatform.final_reply = "(idle)"

    poster = make_digest_poster(
        fake_platform,
        WS,
        30,
        headers_fn=_headers,
        notify_auth_headers=_headers,
    )
    await poster("<digest body: 1 urgent item>")

    # exactly one idle wake was posted, framed with the §5.5 ack directive
    [a2a] = _FakePlatform.captured["a2a"]
    sent_text = "\n".join(
        p.get("text", "") for p in a2a["body"]["params"]["message"]["parts"]
    )
    assert sent_text.startswith(SYSTEM_IDLE_HEADER)
    assert "picking up" in sent_text.lower()
    assert "send_message_to_user" in sent_text

    # BOTH proactive self-reports were delivered to the user-notify seam.
    notifies = [n["body"]["message"] for n in _FakePlatform.captured["notify"]]
    assert notifies == [ACK_MESSAGE, PER_ITEM_MESSAGE], notifies
    # The (idle) final reply was suppressed — so these two are the proactive
    # self-reports, not the poster's single final-reply fallback forward.
    assert len(notifies) == 2
