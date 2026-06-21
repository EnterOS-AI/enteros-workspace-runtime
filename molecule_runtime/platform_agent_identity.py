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

SSOT — the literals below (``SETTINGS_PATH``, ``MCPSERVERS_KEY``, and
``MANAGEMENT_MCP_NAME``) are NOT free to drift. They are declared in the
cross-repo contract ``contracts/mcp-plugin-delivery.contract.json``. The same
path/key/name are produced by the MCPServerAdaptor plugin and consumed by
``claude_sdk_executor._load_settings_mcp`` — this module is a THIRD consumer.

Enforcement is layered, and honestly scoped:
  * RUNTIME-LOCAL (this repo, active): ``tests/test_mcp_plugin_delivery_contract.py``
    pins every literal used here to this repo's vendored copy of the contract,
    so an in-repo edit that changes a literal without the contract (or vice
    versa) fails ``unit-tests`` before any image ships.
  * CROSS-REPO (byte-identical core/template/runtime copies): enforced by
    ``mcp-plugin-delivery-contract-drift`` in molecule-core. Adding this repo's
    copy to that byte-compare set is a tracked follow-up — until it lands, the
    cross-repo guarantee covers core<->template only, not yet the runtime copy.

Literal drift between producer and consumers is the exact bug this file was
changed to fix; the contract + the runtime-local gate keep it from recurring
in this repo.
"""

import json
import os
import threading

# Legacy in-container path to the platform MCP server binary baked into the
# platform-agent image.
MCPSERVER_PATH = "/opt/molecule-mcp-server"

# Plugin-delivery path: the molecule-platform plugin writes the management MCP
# into the Claude settings.json ``mcpServers`` map. Mirrors the location the
# claude-code executor loads from (runtime #149 ``_load_settings_mcp``).
SETTINGS_PATH = "/configs/.claude/settings.json"

# The settings.json map under which MCP servers are declared. Tied to the
# contract ``key`` so the gate catches a source-side rename of this literal.
MCPSERVERS_KEY = "mcpServers"

# The ``mcpServers`` entry name the management plugin registers under.
MANAGEMENT_MCP_NAME = "molecule-platform"

# Env marker baked into the platform-agent image (Dockerfile.platform-agent
# ``ENV MOLECULE_PLATFORM_AGENT_IMAGE_BAKED=1``). When set, this container IS
# the org-management concierge image: it bakes the ``@molecule-ai/mcp-server``
# management binary at the ``molecule-platform-mcp`` symlink, and the
# management MCP entry MUST be present in settings.json for the RCA #2970 gate
# to mark the concierge online. Used by ensure_management_mcp_in_settings to
# decide whether to self-heal the protected entry (a no-op on ordinary
# workspace images, which carry no management MCP and must not declare one).
PLATFORM_AGENT_IMAGE_ENV = "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED"

# The on-image command that launches the baked management MCP server. The
# platform-agent image symlinks ``@molecule-ai/mcp-server``'s entry to
# ``/usr/local/bin/molecule-platform-mcp`` (Dockerfile.platform-agent). This is
# the SAME command + env the template's mcp_servers.yaml overlay declares
# (``{name: molecule-platform, command: molecule-platform-mcp,
# env: {MOLECULE_MCP_MODE: management}}``) — re-asserting it from the baked
# binary makes the protected entry independent of the per-boot gitea fetch of
# the molecule-platform-mcp plugin, which is the failure this self-heal closes.
MANAGEMENT_MCP_COMMAND = "molecule-platform-mcp"

# The protected ``mcpServers`` spec the runtime re-asserts at boot on the
# platform-agent image. ``name -> {command, env}`` per the cross-repo delivery
# contract entry_shape.
MANAGEMENT_MCP_SPEC = {
    "command": MANAGEMENT_MCP_COMMAND,
    "env": {"MOLECULE_MCP_MODE": "management"},
}


def on_platform_agent_image() -> bool:
    """True when this container is the baked platform-agent (concierge) image.

    Reads the ``MOLECULE_PLATFORM_AGENT_IMAGE_BAKED`` env marker the
    Dockerfile.platform-agent sets. Treated as set when the value is a
    non-empty, non-"0"/"false" string so a stray ``=0`` doesn't flip it on.
    """
    val = os.environ.get(PLATFORM_AGENT_IMAGE_ENV, "").strip().lower()
    return val not in ("", "0", "false", "no")


def ensure_management_mcp_in_settings() -> bool:
    """Re-assert the protected ``molecule-platform`` management MCP entry into
    ``/configs/.claude/settings.json`` at boot — additively, never clobbering
    user-plugin entries.

    Root cause this closes (concierge fail-closed on user-plugin install):
    a SaaS restart is a fresh ephemeral instance, so ``/configs`` is rebuilt
    every boot. The entrypoint boot-install ``rm -rf /configs/plugins`` then
    re-fetches the DB desired-set (declared ∪ installed) and the runtime's
    per-plugin ``_merge_settings_fragment`` re-adds each plugin's mcpServers
    block additively. That additive merge is correct — BUT the management MCP's
    survival depended on its OWN plugin (``molecule-platform-mcp``, a PRIVATE
    gitea repo) re-fetching + re-merging on the SAME boot. When that private
    fetch fails (missing/over-scoped token, 404, gitea hang — recurring
    core#3065/#3108) while a PUBLIC user plugin (e.g. image-gen) fetches fine,
    settings.json ends up with only the user plugin's entry and the
    ``molecule-platform`` entry is gone → ``_settings_has_management_mcp()``
    is False → the RCA #2970 gate fail-closes the concierge to ``failed``.

    The fix makes the desired set ``protected-platform-entries ∪ declared-user-
    plugins``: the runtime ALWAYS re-asserts the protected entry from the
    image-baked binary, so a user-plugin install/restart can never evict it and
    a failed plugin re-fetch self-heals. Idempotent and additive — it only
    writes when the protected entry is absent or not byte-identical, and it
    preserves every other key + mcpServer already in settings.json.

    No-op (returns False) on ordinary workspace images: only the baked
    platform-agent image carries the management MCP, so only it may declare one
    (security hygiene — the org-admin MCP stays out of tenant workspaces). The
    server-side org-root entitlement + org-admin key injection remain the real
    privilege boundary; this only wires the local liveness entry.

    Returns True when settings.json was (re)written, False otherwise.
    """
    if not on_platform_agent_image():
        return False

    path = SETTINGS_PATH
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        # Unreadable/malformed: rebuild a minimal settings.json carrying the
        # protected entry rather than leave the concierge fail-closed. A
        # corrupt file with no recoverable user content is the safe case to
        # overwrite — losing a broken file beats a wedged concierge.
        data = {}

    servers = data.get(MCPSERVERS_KEY)
    if not isinstance(servers, dict):
        servers = {}

    if servers.get(MANAGEMENT_MCP_NAME) == MANAGEMENT_MCP_SPEC:
        return False  # already present + identical — nothing to do

    # Additive: keep every other mcpServer (user plugins) intact; only set ours.
    servers[MANAGEMENT_MCP_NAME] = dict(MANAGEMENT_MCP_SPEC)
    data[MCPSERVERS_KEY] = servers

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return True


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
    servers = data.get(MCPSERVERS_KEY) if isinstance(data, dict) else None
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


# ── loaded_mcp_tools producer (core#3082) ──────────────────────────────────
# mcp_server_present() proves the management MCP is DECLARED. The platform's
# online/degraded gate also wants to know its tools were ACTUALLY LOADED into
# the model's tool list — a declared-but-not-loaded server is the exact
# false-green #3082 catches. The runtime can only know that from a live turn:
# the claude CLI's `init` system-message carries the tool list the model sees.
# The executor records it here (the mcp__* ids) and the heartbeat reports it as
# `loaded_mcp_tools`. It stays None until the first turn runs, so the heartbeat
# OMITS the field and the gate stays fail-closed (degraded) until the tools are
# observed live — never reporting a guessed/static list.
_loaded_mcp_tools_lock = threading.Lock()
_loaded_mcp_tools = None  # type: list[str] | None


def set_loaded_mcp_tools(tools) -> None:
    """Record the MCP tool ids the running agent actually loaded.

    Called by the claude-code executor when it observes the `init`
    system-message tool list. Pass the mcp__* tool ids (or an empty list when a
    turn ran but loaded no MCP tools — that is itself a meaningful, non-None
    signal). Pass None to clear.
    """
    global _loaded_mcp_tools
    with _loaded_mcp_tools_lock:
        _loaded_mcp_tools = None if tools is None else [str(t) for t in tools]


def loaded_mcp_tools():
    """The last-observed loaded MCP tool ids, or None if no turn has run yet."""
    with _loaded_mcp_tools_lock:
        return None if _loaded_mcp_tools is None else list(_loaded_mcp_tools)


def identity_gate_payload() -> dict:
    """Return the payload fragment the runtime sends on register/heartbeat.

    `mcp_server_present` is always present so the controlplane can treat its
    absence as fail-closed (an old/generic runtime that doesn't declare
    mcp-server availability cannot be trusted as a platform agent).

    `loaded_mcp_tools` is included ONLY once a live turn has reported a tool
    list (core#3082). Omitting it pre-first-turn keeps the gate fail-closed
    rather than asserting an empty/guessed list.
    """
    payload = {"mcp_server_present": mcp_server_present()}
    tools = loaded_mcp_tools()
    if tools is not None:
        payload["loaded_mcp_tools"] = tools
    return payload
