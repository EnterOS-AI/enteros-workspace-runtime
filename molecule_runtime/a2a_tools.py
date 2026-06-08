"""A2A MCP tool implementations — the body of each tool handler.

Imports shared client functions and constants from a2a_client.
"""

import httpx

from molecule_runtime.a2a_client import (
    PLATFORM_URL,
    _resolve_workspace_id,
    _A2A_ERROR_PREFIX,
    _peer_names,
    _peer_to_source,
    discover_peer,
    get_peers,
    get_peers_with_diagnostic,
    get_workspace_info,
    send_a2a_message,
)
from molecule_runtime.builtin_tools.security import _redact_secrets
from molecule_runtime.platform_auth import list_registered_workspaces


# ---------------------------------------------------------------------------
# RBAC + auth helpers — extracted to a2a_tools_rbac (RFC #2873 iter 4a).
# Re-exported here under the legacy underscore names so existing tests'
# patch("a2a_tools._check_memory_write_permission", …) and call sites
# inside this module that resolve bare names against the module-level
# namespace continue to work unchanged.
# ---------------------------------------------------------------------------
from molecule_runtime.a2a_tools_rbac import (  # noqa: E402  (import after the from-a2a_client block)
    _auth_headers_for_heartbeat,
    _check_memory_read_permission,
    _check_memory_write_permission,
    _get_workspace_tier,
    _is_root_workspace,
    _ROLE_PERMISSIONS,
)


# Per-field caps on the heartbeat / activity payload. Borrowed from
# hermes-agent's design discipline: cap ONCE in the helper, not at every
# call site, so a future caller adding error_detail can't accidentally
# DoS activity_logs by pasting a 4MB stack trace + base64 image.
#
# Why these specific limits:
#   - error_detail (4096): hermes' value. Long enough for a multi-frame
#     stack trace, short enough that 100 errors in 5min is < 500KB total.
#   - summary (256): summary is a one-liner shown in the canvas card +
#     activity row. 256 covers UTF-8 emoji + a sentence.
#   - response_text (NOT capped): this is the agent's actual reply
#     content. Capping would silently truncate user-visible output.
_MAX_ERROR_DETAIL_CHARS = 4096
_MAX_SUMMARY_CHARS = 256


async def report_activity(
    activity_type: str, target_id: str = "", summary: str = "", status: str = "ok",
    task_text: str = "", response_text: str = "", error_detail: str = "",
):
    """Report activity to the platform for live progress tracking."""
    # Defensive caps in the helper itself so every caller benefits — see
    # _MAX_ERROR_DETAIL_CHARS / _MAX_SUMMARY_CHARS comments above.
    if error_detail and len(error_detail) > _MAX_ERROR_DETAIL_CHARS:
        error_detail = error_detail[:_MAX_ERROR_DETAIL_CHARS]
    if summary and len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            payload: dict = {
                "activity_type": activity_type,
                "source_id": _resolve_workspace_id(),
                "target_id": target_id,
                "method": "message/send",
                "summary": summary,
                "status": status,
            }
            if task_text:
                payload["request_body"] = {"task": task_text}
            if response_text:
                payload["response_body"] = {"result": response_text}
            if error_detail:
                # error_detail is a top-level activity row column on the
                # platform (handlers/activity.go). Surfacing the cleaned
                # exception string here lets the Activity tab render a
                # red error chip + the cause without forcing the user
                # to scroll into the raw response_body JSON.
                payload["error_detail"] = error_detail
            await client.post(
                f"{PLATFORM_URL}/workspaces/{_resolve_workspace_id()}/activity",
                json=payload,
                headers=_auth_headers_for_heartbeat(),
            )
            # Also push current_task via heartbeat for canvas card display
            if summary:
                await client.post(
                    f"{PLATFORM_URL}/registry/heartbeat",
                    json={
                        "workspace_id": _resolve_workspace_id(),
                        "current_task": summary,
                        "active_tasks": 1,
                        "error_rate": 0,
                        "sample_error": "",
                        "uptime_seconds": 0,
                    },
                    headers=_auth_headers_for_heartbeat(),
                )
    except Exception:
        pass  # Best-effort — don't block delegation on activity reporting


# Delegation tool handlers — extracted to a2a_tools_delegation
# (RFC #2873 iter 4b). Re-imported here so call sites + tests that
# reference ``a2a_tools.tool_delegate_task`` /
# ``a2a_tools._delegate_sync_via_polling`` keep resolving identically.
from molecule_runtime.a2a_tools_delegation import (  # noqa: E402  (import after the from-a2a_client block)
    _SYNC_POLL_BUDGET_S,
    _SYNC_POLL_INTERVAL_S,
    _delegate_sync_via_polling,
    tool_check_task_status,
    tool_delegate_task,
    tool_delegate_task_async,
)


# Messaging tool handlers — extracted to a2a_tools_messaging
# (RFC #2873 iter 4d). Re-imported here so call sites + tests that
# reference ``a2a_tools.tool_send_message_to_user`` /
# ``tool_list_peers`` / ``tool_get_workspace_info`` /
# ``tool_chat_history`` / ``_upload_chat_files`` keep resolving
# identically.
from molecule_runtime.a2a_tools_messaging import (  # noqa: E402  (import after the top-of-module imports)
    _upload_chat_files,
    tool_broadcast_message,
    tool_chat_history,
    tool_get_workspace_info,
    tool_list_peers,
    tool_send_message_to_user,
)


# Memory tool handlers — extracted to a2a_tools_memory (RFC #2873 iter 4c).
# Re-imported here so call sites + tests that reference
# ``a2a_tools.tool_commit_memory`` / ``tool_recall_memory`` keep
# resolving identically.
from molecule_runtime.a2a_tools_memory import (  # noqa: E402  (import after the top-of-module imports)
    tool_commit_memory,
    tool_recall_memory,
)


# Inbox tool handlers — extracted to a2a_tools_inbox (RFC #2873 iter 4e).
# Re-imported here so call sites + tests that reference
# ``a2a_tools.tool_inbox_peek`` / ``tool_inbox_pop`` / ``tool_wait_for_message``
# / ``_enrich_inbound_for_agent`` / ``_INBOX_NOT_ENABLED_MSG`` keep
# resolving identically.
from molecule_runtime.a2a_tools_inbox import (  # noqa: E402  (import after the top-of-module imports)
    _INBOX_NOT_ENABLED_MSG,
    _enrich_inbound_for_agent,
    tool_inbox_peek,
    tool_inbox_pop,
    tool_wait_for_message,
)


# Identity tool handlers — extracted to a2a_tools_identity. Ports the
# two T4-tier MCP tools (``tool_get_runtime_identity`` +
# ``tool_update_agent_card``) from molecule-ai-workspace-runtime PR#17.
# That repo is mirror-only (reference_runtime_repo_is_mirror_only);
# this is the canonical edit point, and the wheel mirror is
# regenerated by publish-runtime.yml on merge.
from molecule_runtime.a2a_tools_identity import (  # noqa: E402  (import after the top-of-module imports)
    tool_get_runtime_identity,
    tool_update_agent_card,
)
