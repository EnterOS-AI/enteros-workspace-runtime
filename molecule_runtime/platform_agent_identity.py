"""Platform-agent identity assertions used at registration and heartbeat time.

The concierge (kind='platform') must boot with BOTH a seeded MODEL secret AND
the platform-agent image's baked ``/opt/molecule-mcp-server`` binary. The
runtime advertises the latter to the controlplane so the fail-closed gate in
``workspace-server/internal/handlers/registry.go`` can refuse online-marking
when either prerequisite is missing (RCA #2970).
"""

import os

# In-container path to the platform MCP server binary baked into the
# platform-agent image. Its presence is the runtime-side proof that the
# concierge has the org-admin MCP tooling available.
MCPSERVER_PATH = "/opt/molecule-mcp-server"


def mcp_server_present() -> bool:
    """Return True when the platform-agent MCP server binary is present."""
    return os.path.exists(MCPSERVER_PATH)


def identity_gate_payload() -> dict:
    """Return the payload fragment the runtime sends on register/heartbeat.

    Always present in the wire body so the controlplane can treat its absence
    as fail-closed (an old/generic runtime that doesn't declare mcp-server
    availability cannot be trusted as a platform agent).
    """
    return {"mcp_server_present": mcp_server_present()}
