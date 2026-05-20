"""Identity tool handlers — single-concern slice of the a2a_tools surface.

Owns the two MCP tools that close the T4-tier workspace owner-permission
gaps reported via the canvas:

  * ``tool_get_runtime_identity`` — env-only; returns model, model_provider,
    molecule_model, anthropic_base_url, tier, workspace_id, runtime
    (ADAPTER_MODULE). No HTTP call. Always permitted by RBAC — even
    read-only agents may know what model they are.

  * ``tool_update_agent_card`` — POSTs the card to ``/registry/update-card``
    with the workspace's own bearer (same auth path as ``tool_commit_memory``
    via ``a2a_tools_rbac.auth_headers_for_heartbeat``). The platform
    replaces the stored card and broadcasts an ``agent_card_updated``
    event so the canvas reflects the new card live. Gated on
    ``memory.write`` capability via the existing RBAC permission map so
    read-only roles can't silently rewrite the platform card.

Both originated as a port of molecule-ai-workspace-runtime PR#17
(``feat(mcp): add update_agent_card + get_runtime_identity tools``).
The mirror-only PR#17 was closed without merge per
``reference_runtime_repo_is_mirror_only``; the canonical edit point is
this monorepo at ``workspace/`` and the wheel mirror is regenerated
automatically by the publish-runtime workflow.

Imports the auth-header primitive from ``a2a_tools_rbac`` (iter 4a) —
NOT from ``a2a_tools`` — to avoid a circular import with the
kitchen-sink re-export module.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from molecule_runtime.a2a_client import PLATFORM_URL
from molecule_runtime.a2a_tools_rbac import (
    auth_headers_for_heartbeat as _auth_headers_for_heartbeat,
    check_memory_write_permission as _check_memory_write_permission,
)


def _runtime_identity_payload() -> dict[str, Any]:
    """Build the identity dict — env-only, no I/O.

    Factored out from ``tool_get_runtime_identity`` so tests can assert
    against the exact key set without re-parsing JSON. The MCP tool
    handler ``tool_get_runtime_identity`` is the only public caller in
    production; tests call this helper directly.
    """
    return {
        "model": os.environ.get("MODEL", ""),
        "model_provider": os.environ.get("MODEL_PROVIDER", ""),
        "molecule_model": os.environ.get("MOLECULE_MODEL", ""),
        "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "tier": os.environ.get("TIER", ""),
        "workspace_id": os.environ.get("WORKSPACE_ID", ""),
        # Adapter module is the closest thing the runtime has to a
        # "template slug" — e.g. "adapter" for claude-code-default,
        # "hermes" for hermes-template, etc. Picked from
        # $ADAPTER_MODULE env baked by each template's Dockerfile.
        "runtime": os.environ.get("ADAPTER_MODULE", ""),
    }


async def tool_get_runtime_identity() -> str:
    """Return this runtime's identity — model, provider, tier, IDs.

    Env-only; no HTTP call. Useful so the agent can answer "what model
    am I?" correctly instead of guessing from a stale system prompt
    that the operator may have changed between boots.

    Returns the identity as a JSON-encoded string (the dispatch contract
    every MCP tool in this module follows). Tests that want to assert
    individual fields can call ``_runtime_identity_payload()`` directly,
    or ``json.loads`` the return value.

    Always permitted by RBAC — there is no sensitive information here
    that isn't already available to the process via ``os.environ``.
    The point of the tool is to surface those env values to the agent
    layer in a stable, documented shape rather than expecting every
    agent runtime to know to ``echo $MODEL``.
    """
    return json.dumps(_runtime_identity_payload(), indent=2)


async def tool_update_agent_card(card: Any) -> str:
    """Update this workspace's agent_card on the platform.

    POSTs the provided card to ``/registry/update-card`` with the
    workspace's own bearer token (same auth path as ``tool_commit_memory``
    and ``tool_get_workspace_info``). The platform validates required
    fields server-side, replaces the stored card, and broadcasts an
    ``agent_card_updated`` event so the canvas updates live.

    Args:
        card: A JSON-serialisable object (typically a dict) holding the
            new card. The platform validates required fields server-side.

    Returns:
        JSON-encoded string. Body:
          - ``{"success": true, "status": "updated"}`` on success;
          - ``{"success": false, "error": "<msg>", "status_code": <int>}``
            on platform error;
          - ``{"success": false, "error": "<reason>"}`` on local validation
            (non-dict card, missing WORKSPACE_ID, network error).

    Permission gate: this tool requires the ``memory.write`` RBAC
    capability — same gate as ``tool_commit_memory``. The check runs
    inline rather than at the dispatcher layer to keep ``a2a_mcp_server``
    permission-agnostic (the gate sits with the implementation, not the
    transport). Read-only roles get a clear error string back instead
    of a 403 from the platform.

    We re-check ``isinstance(card, dict)`` here defensively rather than
    trust the MCP schema validator alone — the schema only constrains
    the transport, not the in-process call surface used by tests and
    sibling modules.
    """
    payload = await _update_agent_card_impl(card)
    return json.dumps(payload, indent=2)


async def _update_agent_card_impl(card: Any) -> dict[str, Any]:
    """Dict-returning core of ``tool_update_agent_card``.

    Split out so tests can assert against the raw dict shape (status
    codes, error messages) without re-parsing JSON on every assertion.
    The string-returning ``tool_update_agent_card`` is a thin wrapper
    invoked by the MCP dispatcher.
    """
    # RBAC: require memory.write permission. Same gate as
    # tool_commit_memory (the agent already needs this capability to
    # persist anything outbound). Read-only roles can still call
    # get_runtime_identity / get_workspace_info to introspect — those
    # are env-only / read-only and have no inline gate.
    if not _check_memory_write_permission():
        return {
            "success": False,
            "error": (
                "RBAC — this workspace does not have the 'memory.write' "
                "permission required to update the agent_card."
            ),
        }
    if not isinstance(card, dict):
        return {
            "success": False,
            "error": "card must be a JSON object (dict)",
        }
    ws_id = os.environ.get("WORKSPACE_ID", "")
    if not ws_id:
        return {
            "success": False,
            "error": "WORKSPACE_ID env not set; cannot identify caller",
        }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{PLATFORM_URL}/registry/update-card",
                json={"workspace_id": ws_id, "agent_card": card},
                headers=_auth_headers_for_heartbeat(),
            )
            if resp.status_code == 200:
                body: dict[str, Any] = {}
                try:
                    body = resp.json()
                except Exception:
                    pass
                return {
                    "success": True,
                    "status": body.get("status", "updated"),
                }
            # Non-200 — surface what the platform returned.
            error_msg = ""
            try:
                error_msg = resp.json().get("error", "") or resp.text
            except Exception:
                error_msg = resp.text
            return {
                "success": False,
                "status_code": resp.status_code,
                "error": error_msg,
            }
    except Exception as e:
        return {"success": False, "error": f"network error: {e}"}
