"""Deliver a self-initiated turn's reply text to the user's canvas chat.

Root cause this module exists for (core#4460 follow-up, observed live
2026-07-17): a digest self-fire POSTs ``message/send`` to the platform, the
platform routes it back as a normal A2A turn, and the agent's reply comes
back as the HTTP **response body of that self-POST** — which the poster used
to ``read()`` and throw away. Request-response turns deliver their reply text
to the initiator (the user's canvas), so models — correctly — treat "write my
answer as the turn reply" as delivery. In self-initiated turns that text
silently evaporated: the agent answered the user into the void and believed
it succeeded ("yo+hi delivered ✅" while the canvas showed nothing).

This module restores the symmetry: classify the self-POST's response via the
canonical envelope parser (``a2a_response.parse`` — the SSOT; queued/error
envelopes are logged outcomes, never silently empty), extract the reply text
(richer than the SSOT Result variant: Task-shaped ``status.message`` and
``artifacts`` parts included), and forward non-empty, non-sentinel text to
the user via the same ``POST /workspaces/<id>/notify`` endpoint
``send_message_to_user`` uses.

Silence valve: digests fire on a cadence forever, so unconditional forwarding
would turn periodic housekeeping ticks into chat spam. The digest header
(identity provider REPLY_ROUTING_LINE) instructs the agent to reply with
exactly ``(idle)`` when nothing needs the user; that sentinel — plus empty or
decoration-only text, the executor's ``(no response generated)``, and the
autonomous-loop guard's bracketed ``[autonomous …]`` breaker notices (internal
diagnostics, never user-facing) — is suppressed here. A substantive reply
that merely CONTAINS the word "idle" is delivered.

Everything network-touching is never-raise: a delivery failure must never
crash the idle loop (it logs and moves on), and a 403 talk_to_user_disabled
is a policy outcome, not an error.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, Tuple

# Reply the agent uses to deliberately stay silent on a housekeeping tick.
# Mirrored in providers/identity.py REPLY_ROUTING_LINE — keep in lockstep.
IDLE_SENTINEL = "(idle)"

# Executor sentinel for an empty turn (see a2a_executor / a2a_cli).
_NO_RESPONSE_SENTINEL = "(no response generated)"

# The autonomous-loop guard substitutes final_text with bracketed breaker
# notices for routine self-turns ("[autonomous replay suppressed — …]",
# "[autonomous loop halted — …]", a2a_executor ~1002-1026). They are internal
# diagnostics; forwarding them would deliver the very spam the guard exists
# to stop. Prefix owned by the executor — keep in lockstep.
_GUARD_NOTICE_PREFIX = "[autonomous "

# Ceiling for a forwarded chat bubble. A verbose consolidation turn must not
# land a wall of text in the user's chat every cadence tick.
MAX_FORWARD_CHARS = 4000
_TRUNCATION_MARKER = " …[reply truncated]"


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


def _decode(body: Any) -> Optional[dict]:
    """bytes/str/dict → dict, or None when undecodable. Never raises."""
    data = body
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return None
    return data if isinstance(data, dict) else None


def _result_text(result: dict) -> str:
    """Rich text extraction for a Result envelope: Message parts, Task
    status.message parts, and artifacts parts (the SSOT parse() Result only
    reads parts[0] — this caller needs the fuller sweep)."""
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


def describe_reply(body: Any) -> Tuple[str, str]:
    """Classify a message/send response and extract its user-facing text.

    Returns ``(kind, text)`` where kind ∈ {"result", "queued", "error",
    "malformed"}. Variant detection is delegated to the canonical
    ``a2a_response.parse`` (SSOT — no inline shape sniffing); the platform's
    ``delivery_mode:"push-async"`` queued shape, which parse() predates, is
    additionally recognized here. text is non-empty only for kind="result"
    (the reply to deliver) and kind="error" (the message to log). Never
    raises.
    """
    data = _decode(body)
    if data is None:
        return "malformed", ""
    try:
        from molecule_runtime.a2a_response import Error, Queued, Result, parse

        variant = parse(data)
        if isinstance(variant, Result):
            return "result", _result_text(data.get("result") or {})
        if isinstance(variant, Queued):
            return "queued", ""
        if isinstance(variant, Error):
            return "error", str(getattr(variant, "message", "") or "")[:500]
    except Exception:  # noqa: BLE001 — classification must never crash the loop
        pass
    # parse() predates the cap-and-queue {"status":"queued",
    # "delivery_mode":"push-async"} envelope (a2a_proxy.go:556) — it would
    # land in Malformed; recognize it as queued here.
    if data.get("status") == "queued" or data.get("queued") is True:
        return "queued", ""
    if isinstance(data.get("result"), dict):
        # Fallback path if the SSOT import itself failed above.
        return "result", _result_text(data["result"])
    return "malformed", ""


def extract_reply_text(body: Any) -> str:
    """The Result-variant reply text, or "" for every other outcome.

    Kept for callers/tests that only need the deliverable text; prefer
    :func:`describe_reply` where the queued/error outcome should be logged.
    """
    kind, text = describe_reply(body)
    return text if kind == "result" else ""


def should_suppress(text: str) -> bool:
    """True when the reply is deliberate silence, noise, or internal
    diagnostics — anything that must not become a chat bubble."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    if stripped == _NO_RESPONSE_SENTINEL:
        return True
    if stripped.lower().startswith(_GUARD_NOTICE_PREFIX):
        return True
    # Strip trivial decoration so "(idle)." / "`(idle)`" still count as the
    # sentinel, and pure decoration ("```", "...") counts as silence. A bare
    # word "idle" is NOT the sentinel — a substantive one-word status reply
    # must be delivered (review #327: only the parenthesized form is the
    # contract REPLY_ROUTING_LINE instructs).
    normalized = stripped.strip(" .!`'\"").lower()
    if not normalized:
        return True
    return normalized == IDLE_SENTINEL.lower()


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
    if len(text) > MAX_FORWARD_CHARS:
        text = text[: MAX_FORWARD_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
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
