"""Tools for raising user-facing requests (tasks + approvals) into the
unified requests inbox — the Tasks / Approvals tabs in the canvas.

core#2606: these were previously available ONLY to the org concierge
(via the management-mode npm mcp-server's create_request/create_approval).
A normal workspace agent — and any external runtime that loads this a2a
bridge — now gets the same surface, pointed at the SAME unified endpoint
``POST /workspaces/<self>/requests`` (wsAuth-gated, so the workspace's own
token authenticates and the platform records the workspace as requester).

Distinct from:
  * ``send_message_to_user`` — a chat bubble, no decision/worklist item.
  * ``request_approval`` (legacy builtin) — writes the old
    ``approval_requests`` table + floating banner, NOT the unified tab.
"""

from __future__ import annotations

import httpx

from molecule_runtime.a2a_client import _resolve_platform_url, _resolve_workspace_id
from molecule_runtime.a2a_tools_rbac import auth_headers_for_heartbeat as _auth_headers_for_heartbeat

_ERR = "Error: "


async def _post_request(kind: str, title: str, detail: str, priority: int | None) -> str:
    title = (title or "").strip()
    if not title:
        return _ERR + "title is required"
    if kind not in ("task", "approval"):
        return _ERR + 'kind must be "task" or "approval"'
    ws = _resolve_workspace_id()
    if not ws:
        return _ERR + "no workspace id resolved (cannot raise a request)"
    base = _resolve_platform_url(ws)
    payload: dict = {
        "kind": kind,
        "recipient_type": "user",
        "recipient_id": "",
        "title": title,
        "detail": detail or "",
    }
    if priority is not None:
        payload["priority"] = priority
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{base}/workspaces/{ws}/requests",
                json=payload,
                headers=_auth_headers_for_heartbeat(ws),
            )
    except Exception as e:  # pylint: disable=broad-except
        return _ERR + f"failed to reach platform: {e}"
    if resp.status_code not in (200, 201):
        return _ERR + f"platform returned HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        rid = resp.json().get("request_id", "")
    except Exception:  # pylint: disable=broad-except
        rid = ""
    label = "approval" if kind == "approval" else "task"
    return (
        f"Raised {label} request “{title}” to the user (id {rid}, status pending). "
        f"It is now in the user's {'Approvals' if kind == 'approval' else 'Tasks'} tab. "
        f"You are NOT blocked — poll later with check_requests / list your outgoing requests; "
        f"the user's decision arrives back to you as an inbound turn."
    )


async def tool_create_request(
    kind: str,
    title: str,
    detail: str | None = None,
    priority: int | None = None,
) -> str:
    """Raise a request (a task OR an approval) addressed to the user.

    Args:
        kind: "task" = please DO something; "approval" = please APPROVE
            something. Both surface in the user's unified inbox tabs.
        title: One-line summary shown in the user's Tasks/Approvals tab.
        detail: Optional fuller context / reason.
        priority: Optional integer (higher = more urgent).
    """
    return await _post_request(kind, title, detail or "", priority)


async def tool_create_approval(
    action: str,
    reason: str | None = None,
) -> str:
    """Ask the user to APPROVE an action — the approval-kind convenience.

    Args:
        action: What needs approval (becomes the request title).
        reason: Optional why-it's-needed (becomes the detail).
    """
    return await _post_request("approval", action, reason or "", None)
