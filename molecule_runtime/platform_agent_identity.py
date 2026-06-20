"""Platform-agent identity assertions used at registration and heartbeat time.

A concierge (kind='platform') must boot with BOTH a seeded MODEL secret AND the
management MCP available, so the fail-closed gate in
``workspace-server/internal/handlers/registry.go`` can refuse online-marking
when either prerequisite is missing (RCA #2970).

The management MCP can be delivered two ways, and either satisfies the gate
runtime-agnostically:

  * legacy: the platform-agent image bakes ``/opt/molecule-mcp-server``; or
  * current: the ``molecule-platform`` plugin wires the MCP into the Claude
    ``settings.json`` ``mcpServers`` map (claude-code + plugin composition;
    runtime #149 / core#3079). Any plugin-capable runtime uses this same path.

This mirrors the platform model: "platform-ness" is a composition (org key +
management MCP plugin) on an ordinary runtime, not a special baked image. The
runtime-side signal therefore proves the MCP is *wired in* by whatever delivery
mechanism. Absence of BOTH stays fail-closed: a generic runtime that declares
neither cannot be trusted as a platform agent.

SSOT — the literals below (``SETTINGS_PATH``, the ``mcpServers`` key, and the
``MANAGEMENT_MCP_NAME`` entry) are NOT free to drift. They are governed by the
cross-repo contract ``contracts/mcp-plugin-delivery.contract.json`` in
molecule-core, enforced by ``.gitea/workflows/mcp-plugin-delivery-contract-drift.yml``.
The same path/key/name are produced by the MCPServerAdaptor plugin and consumed
by ``claude_sdk_executor._load_settings_mcp`` — this module is a THIRD consumer.
If you change any literal here, update the contract (and vice-versa) or the
drift gate fails. This drift between producer and consumers is the exact bug
this file was changed to fix; the contract is what keeps it from recurring.
"""

import json
import os

# Legacy in-container path to the platform MCP server binary baked into the
# platform-agent image.
MCPSERVER_PATH = "/opt/molecule-mcp-server"

# Plugin-delivery path: the molecule-platform plugin writes the management MCP
# into the Claude settings.json ``mcpServers`` map. Mirrors the location the
# claude-code executor loads from (runtime #149 ``_load_settings_mcp``).
SETTINGS_PATH = "/configs/.claude/settings.json"

# The ``mcpServers`` entry name the management plugin registers under.
MANAGEMENT_MCP_NAME = "molecule-platform"


def _settings_has_management_mcp() -> bool:
    """True when the plugin-delivered management MCP is wired into settings.json.

    Defensive by construction: a missing, unreadable, or malformed settings file
    yields False so the caller stays fail-closed.
    """
    try:
        with open(SETTINGS_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and MANAGEMENT_MCP_NAME in servers


def mcp_server_present() -> bool:
    """Return True when the management MCP is available to this runtime.

    Delivery-agnostic: a baked ``/opt/molecule-mcp-server`` binary OR a
    plugin-wired ``molecule-platform`` entry in the Claude ``settings.json``
    both prove the org-admin MCP tooling is wired in. Absence of both is
    fail-closed.

    NECESSARY-BUT-NOT-SUFFICIENT. This is the runtime-side *liveness* signal that
    the management MCP is wired in; it is NOT an authorization check. The actual
    privilege boundary is server-side: org-root entitlement to install the
    ``molecule-platform`` plugin AND injection of the org-admin key are both
    gated by the controlplane (see core ``workspace-server`` registry gate
    RCA #2970 and the org-root-only install entitlement). A workspace that merely
    drops a ``settings.json`` cannot mint itself the org key, so spoofing this
    boolean grants nothing without the server-side grant. Keep the literals here
    aligned with the cross-repo contract (see module docstring).
    """
    return os.path.exists(MCPSERVER_PATH) or _settings_has_management_mcp()


def identity_gate_payload() -> dict:
    """Return the payload fragment the runtime sends on register/heartbeat.

    Always present in the wire body so the controlplane can treat its absence
    as fail-closed (an old/generic runtime that doesn't declare mcp-server
    availability cannot be trusted as a platform agent).
    """
    return {"mcp_server_present": mcp_server_present()}
