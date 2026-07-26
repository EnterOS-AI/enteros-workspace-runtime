"""Wire-conformance for the digest self-fire round trip (conformance-style:
drive the REAL builders and capture the REAL bytes over real HTTP).

Covers, in one composed flow against a fake platform:
  * the outgoing message/send payload is framed with <SYSTEM IDLE PROMPT> +
    the §5.5 idle behavior contract, and stamped source=self-idle;
  * RFC §5.5 one-line model: the payload carries message.contextId ==
    canvas-<wsid> so the idle turn runs on the agent's single canvas execution
    line (preemptible by a user turn);
  * the identity header inside the digest reports the in-process workspace
    bridge (never "workspace MCP (0)" — send_message_to_user must be listed);
  * the response body's reply text is forwarded to /notify as the FALLBACK
    final-reply delivery (the primary §5.5 delivery is the agent's in-turn
    send_message_to_user self-reports), while an (idle) reply is suppressed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from molecule_runtime.idle_digest import assemble, render
from molecule_runtime.idle_digest.contract import SYSTEM_IDLE_HEADER, Policy
from molecule_runtime.idle_digest.poster import make_digest_poster
from molecule_runtime.idle_digest.providers import IdentityCapabilitiesProvider

WS = "ws-conformance-1"


class _FakePlatform(BaseHTTPRequestHandler):
    """Captures a2a + notify POSTs; replies like the platform's a2a proxy."""

    captured: dict[str, list[dict]] = {"a2a": [], "notify": []}
    reply_text = "digest reply for the user"

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/a2a"):
            type(self).captured["a2a"].append(
                {"body": body, "headers": dict(self.headers)}
            )
            resp = {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"parts": [{"kind": "text", "text": type(self).reply_text}]},
            }
        elif self.path.endswith("/notify"):
            type(self).captured["notify"].append(
                {"body": body, "headers": dict(self.headers)}
            )
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
    server = HTTPServer(("127.0.0.1", 0), _FakePlatform)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        t.join(timeout=5)


class _NotifyClient:
    """httpx-shaped async client routed at the fake platform via urllib in a
    thread — keeps the forwarder's injectable-client seam honest without
    adding an httpx test dependency here."""

    async def post(self, url, json=None, headers=None):
        import urllib.request

        payload = __import__("json").dumps(json).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json", **(headers or {})}
        )

        def _do():
            with urllib.request.urlopen(req, timeout=10) as r:
                class R:
                    status_code = r.status

                return R()

        return await asyncio.get_running_loop().run_in_executor(None, _do)


async def _rendered_digest() -> str:
    """A real assembled digest: REAL identity provider (default bridge seam →
    the actual in-process registry) + assembler + render."""
    identity = IdentityCapabilitiesProvider(
        workspace_name="Enter OS Agent",
        runtime_kind="hermes",
        persona_reader=lambda cp, pf: "the Org Concierge\nplatform agent",
        # OBSERVED inventory as seen live on the defective build: management
        # MCP only — the bridge union must still surface workspace tools.
        tools_source=lambda: ["mcp__molecule-platform__create_workspace"],
        platform_agent_check=lambda: True,
        # bridge_tools_source deliberately NOT injected: the REAL registry.
    )
    contributions = await identity.contribute()
    assembled = assemble(contributions, Policy.default())
    return render(assembled.contributions)


def _headers(_ws: str) -> dict:
    return {"Authorization": "Bearer test", "X-Workspace-ID": _ws}


@pytest.mark.asyncio
async def test_selffire_wire_roundtrip(fake_platform):
    text = await _rendered_digest()
    poster = make_digest_poster(
        fake_platform,
        WS,
        30,
        headers_fn=_headers,
        notify_auth_headers=_headers,
        notify_client=_NotifyClient(),
    )
    await poster(text)

    # ── outgoing a2a payload: real builder output, captured bytes ──
    [a2a] = _FakePlatform.captured["a2a"]
    params = a2a["body"]["params"]
    sent_parts = params["message"]["parts"]
    sent_text = "\n".join(p.get("text", "") for p in sent_parts)
    assert sent_text.startswith(SYSTEM_IDLE_HEADER), sent_text[:80]
    # RFC §5.5 one-line model: the idle turn runs on the agent's single canvas
    # execution line — the builder must stamp message.contextId = canvas-<wsid>
    # (equal to core's sessionid.DefaultContextID) so the turn shares the user's
    # memory and is preemptible, instead of a detached minted context.
    assert sent_parts, "no message parts in idle payload"
    assert params["message"].get("contextId") == f"canvas-{WS}", (
        f"idle payload must carry canvas contextId, got {params['message'].get('contextId')!r}"
    )
    # The §5.5 behavior contract travels WITH the prompt (ack-before-work,
    # report-per-item, exhaust backlog+goals). Assert the ack directive is
    # present so the framing can't silently regress to a bare digest.
    assert "picking up" in sent_text, "idle §5.5 behavior contract (ack) missing from framing"
    # identity header must carry the in-process bridge — the live defect was
    # "workspace MCP (0)" while 30 bridge tools were served in-process. The
    # names line shows only the first 8 alphabetical tools, so assert the
    # COUNT is non-zero rather than any specific name (the old name check
    # false-positived on the routing line's prose).
    import re as _re

    m = _re.search(r"workspace MCP \((\d+)\)", sent_text)
    assert m, f"no workspace MCP line in digest: {sent_text[-200:]!r}"
    assert int(m.group(1)) > 0, sent_text
    # metadata is the sibling params.metadata (build_message_send_params
    # contract), keyed by A2A_MESSAGE_SOURCE_TYPE ("source_type").
    assert params["metadata"]["source_type"] == "self-idle"

    # ── reply delivery: the response text reached /notify ──
    [notify] = _FakePlatform.captured["notify"]
    assert notify["body"] == {"message": _FakePlatform.reply_text}


@pytest.mark.asyncio
async def test_selffire_idle_reply_is_suppressed(fake_platform):
    _FakePlatform.reply_text = "(idle)"
    try:
        poster = make_digest_poster(
            fake_platform,
            WS,
            30,
            headers_fn=_headers,
            notify_auth_headers=_headers,
            notify_client=_NotifyClient(),
        )
        await poster("some digest body")
        assert len(_FakePlatform.captured["a2a"]) == 1
        assert _FakePlatform.captured["notify"] == []
    finally:
        _FakePlatform.reply_text = "digest reply for the user"
