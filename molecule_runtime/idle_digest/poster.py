"""The digest self-fire poster — extracted from main.py so the full self-fire
wire behavior is testable end-to-end (conformance-style: drive the REAL
builders, capture the REAL bytes) instead of living untested in a boot
closure.

One poster call does the complete self-initiated turn round trip:

  1. frame the rendered digest with <SYSTEM IDLE PROMPT> (contract framing);
  2. build the message/send JSON-RPC payload via the canonical
     ``build_message_send_params`` with the SELF_IDLE source marker AND
     ``context_id=canvas-<wsid>`` (RFC §5.5 one-line model): the idle wake
     runs on the agent's single canvas execution line, sharing memory with the
     user's turns, and is PREEMPTIBLE by a returning user turn (core interrupts
     the in-flight self-idle turn);
  3. POST it to the platform's ``/workspaces/<id>/a2a`` (blocking urllib in a
     thread-pool executor — unchanged from the original closure);
  4. parse the response body — the agent's reply text — and forward non-empty,
     non-sentinel text to the user via ``/notify`` (reply_forwarder), logging
     the outcome.

RFC §5.5 delivery: under the one-line model the agent SELF-REPORTS during the
idle turn — it acks before working ("no active work from you — I'm picking up:
<items>") and reports per work-item via its ``send_message_to_user`` tool
(→ core AgentMessageWriter). The final-reply forward in step 4 is retained only
as a FALLBACK for the last-reply text; the primary delivery is the in-turn
self-reports, driven by the idle-prompt behavior contract (see
idle_digest/contract.SYSTEM_IDLE_HEADER framing).

Injectable seams (``headers_fn``, ``notify_auth_headers``, ``notify_client``)
default to the production functions; tests point them at a fake platform.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable, Optional
from urllib import request as _urlreq

# Module-level (not fire-time) imports, deliberately: a broken symbol must
# fail LOUD at wiring/boot (degrading to the legacy loop via main.py's
# try/except), not convert every digest tick into a silently-SKIPPED no-op
# at fire time (review #327). Cycle-free: none of these import idle_digest.
from molecule_runtime.a2a_client import build_message_send_params
from molecule_runtime.a2a_executor import (
    A2A_MESSAGE_SOURCE_TYPE,
    A2A_SOURCE_SELF_IDLE,
)
from molecule_runtime.idle_digest.contract import frame_idle_prompt
from molecule_runtime.idle_digest.reply_forwarder import (
    describe_reply,
    forward_reply_to_user,
)


def make_digest_poster(
    platform_url: str,
    workspace_id: str,
    fire_timeout: int,
    *,
    headers_fn: Optional[Callable[[str], dict]] = None,
    notify_auth_headers: Optional[Callable[[str], dict]] = None,
    notify_client: Any = None,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> Callable[[str], Any]:
    """Build the async poster the idle-digest controller fires with."""

    async def _digest_poster(text: str) -> None:
        if headers_fn is None:
            from molecule_runtime.platform_auth import self_source_headers as _hf
        else:
            _hf = headers_fn

        payload = json.dumps(
            {
                "method": "message/send",
                "params": build_message_send_params(
                    # <SYSTEM IDLE PROMPT> framing: mark the consolidation as
                    # system-generated in the agent's context and in traces —
                    # transport-layer, never part of the hashed digest.
                    frame_idle_prompt(text),
                    message_id=f"idle-{uuid.uuid4().hex[:8]}",
                    metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_IDLE},
                    # RFC §5.5 (the one-line agent model): the idle wake runs ON
                    # the agent's single canvas execution line (shared memory
                    # with the user's turns), NOT a detached minted context. It
                    # is low-priority work done while quiet; a returning user
                    # turn PREEMPTS it (core interrupts the in-flight self-idle
                    # turn). ``canvas-<wsid>`` MUST equal core's
                    # sessionid.DefaultContextID / the a2a-proxy canvas belt.
                    context_id=f"canvas-{workspace_id}",
                ),
            }
        ).encode()

        def _post() -> bytes:
            req = _urlreq.Request(
                f"{platform_url}/workspaces/{workspace_id}/a2a",
                data=payload,
                headers={"Content-Type": "application/json", **_hf(workspace_id)},
            )
            with _urlreq.urlopen(req, timeout=fire_timeout) as r:
                return r.read()

        resp_body = await asyncio.get_running_loop().run_in_executor(None, _post)

        # The turn's reply text comes back as THIS response body — for a
        # self-fire the initiator is us, so forward it to the user instead of
        # discarding it (the pre-fix behavior silently dropped every
        # digest-turn answer; see reply_forwarder.py).
        try:
            kind, reply_text = describe_reply(resp_body)
            if kind != "result":
                # Queued (busy/poll-mode target) and error envelopes are real
                # outcomes, never silent (review #327): the reply — if any —
                # will not arrive through this response body.
                detail = f" — {reply_text}" if reply_text else ""
                log(f"Idle digest: self-fire outcome {kind}{detail}")
                return
            status = await forward_reply_to_user(
                platform_url,
                workspace_id,
                reply_text,
                auth_headers=notify_auth_headers,
                client=notify_client,
            )
            if status == "delivered":
                log(f"Idle digest: reply forwarded to user ({len(reply_text)} chars)")
            elif status != "suppressed":
                log(f"Idle digest: reply not delivered — {status}")
        except Exception as e:  # pragma: no cover — never crash the loop
            log(f"Idle digest: reply forward error — {e}")

    return _digest_poster
