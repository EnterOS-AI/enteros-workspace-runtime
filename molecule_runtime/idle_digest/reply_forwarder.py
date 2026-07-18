"""Deliver a self-initiated turn's reply text to the user's canvas chat.

Root cause this module exists for (core#4460 follow-up, observed live
2026-07-17): a digest/idle self-fire POSTs ``message/send`` to the platform,
the platform routes it back as a normal A2A turn, and the agent's reply comes
back as the HTTP **response body of that self-POST** — which the poster used
to ``read()`` and throw away. Request-response turns deliver their reply text
to the initiator (the user's canvas), so models — correctly — treat "write my
answer as the turn reply" as delivery. In self-initiated turns that text
silently evaporated: the agent answered the user into the void and believed
it succeeded ("yo+hi delivered ✅" while the canvas showed nothing).

This module restores the symmetry: parse the self-POST's JSON-RPC response,
and forward non-empty reply text to the user via the same
``POST /workspaces/<id>/notify`` endpoint ``send_message_to_user`` uses.

Silence valve: digests fire on a cadence forever, so unconditional forwarding
would turn periodic housekeeping ticks into chat spam. The digest header
(identity provider REPLY_ROUTING_LINE) instructs the agent to reply with
exactly ``(idle)`` when nothing needs the user; that sentinel — plus empty
text and the executor's ``(no response generated)`` — is suppressed here.

Everything network-touching is never-raise: a delivery failure must never
crash the idle loop (it logs and moves on), and a 403 talk_to_user_disabled
is a policy outcome, not an error.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

# Reply the agent uses to deliberately stay silent on a housekeeping tick.
# Mirrored in providers/identity.py REPLY_ROUTING_LINE — keep in lockstep.
IDLE_SENTINEL = "(idle)"

# Executor sentinel for an empty turn (see a2a_executor / a2a_cli).
_NO_RESPONSE_SENTINEL = "(no response generated)"


def _texts_from_parts(parts: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(parts, (list, tuple)):
        return out
    for p in parts:
        if isinstance(p, dict):
            t = p.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t)
    return out


def extract_reply_text(body: Any) -> str:
    """Extract the agent's reply text from a message/send JSON-RPC response.

    Accepts raw bytes/str (JSON) or an already-decoded dict, and tolerates
    every shape the platform returns: a Message result (``result.parts``),
    a Task result (``result.status.message.parts`` and/or
    ``result.artifacts[].parts``), or an error envelope. Returns "" whenever
    no user-visible text can be extracted — never raises.
    """
    data = body
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(data, dict):
        return ""
    result = data.get("result")
    if not isinstance(result, dict):
        return ""

    texts = _texts_from_parts(result.get("parts"))
    if not texts:
        status = result.get("status")
        if isinstance(status, dict):
            msg = status.get("message")
            if isinstance(msg, dict):
                texts = _texts_from_parts(msg.get("parts"))
    if not texts:
        artifacts = result.get("artifacts")
        if isinstance(artifacts, (list, tuple)):
            for a in artifacts:
                if isinstance(a, dict):
                    texts.extend(_texts_from_parts(a.get("parts")))

    return "\n".join(t.strip() for t in texts if t.strip()).strip()


def should_suppress(text: str) -> bool:
    """True when the reply is a deliberate-silence or empty sentinel."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    if stripped == _NO_RESPONSE_SENTINEL:
        return True
    # Exact sentinel or a bare sentinel with trivial decoration ("(idle)."),
    # so a model that almost follows the contract still stays silent.
    normalized = stripped.strip(" .!`'\"").lower()
    return normalized == IDLE_SENTINEL.strip("()").lower() or normalized == IDLE_SENTINEL.lower()


async def forward_reply_to_user(
    platform_url: str,
    workspace_id: str,
    text: str,
    *,
    auth_headers: Optional[Callable[[str], dict]] = None,
    client: Any = None,
) -> str:
    """POST the reply text to the user's chat. Returns a short status string
    for the caller's log line; never raises.

    ``auth_headers``/``client`` are injectable seams for unit tests; the
    defaults are the production heartbeat auth + a fresh httpx client.
    """
    if should_suppress(text):
        return "suppressed"
    try:
        if auth_headers is None:
            from molecule_runtime.a2a_tools_rbac import (
                auth_headers_for_heartbeat as auth_headers,  # type: ignore[no-redef]
            )
        headers = dict(auth_headers(workspace_id))
        url = f"{platform_url.rstrip('/')}/workspaces/{workspace_id}/notify"
        payload = {"message": text}

        if client is not None:
            resp = await client.post(url, json=payload, headers=headers)
        else:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as _c:
                resp = await _c.post(url, json=payload, headers=headers)

        if resp.status_code == 200:
            return "delivered"
        if resp.status_code == 403:
            # talk_to_user disabled is policy, not failure — the reply is
            # intentionally not user-deliverable for this workspace.
            return "skipped: talk_to_user disabled (403)"
        return f"error: platform returned {resp.status_code}"
    except Exception as e:  # noqa: BLE001 — never crash the idle loop
        return f"error: {e}"
