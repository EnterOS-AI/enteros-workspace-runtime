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

SSOT — the literals below (``SETTINGS_PATH``, ``MCPSERVERS_KEY``,
``MANAGEMENT_MCP_NAME``, and ``REQUIRED_TOOL``) are NOT free to drift. They are
declared in the cross-repo contract ``contracts/mcp-plugin-delivery.contract.json``.
The same path/key/name are produced by the MCPServerAdaptor plugin and consumed
by ``claude_sdk_executor._load_settings_mcp`` — this module is a THIRD consumer,
and ``mcp_readiness_probe`` (the active tools/list readiness probe) is a FOURTH.

Enforcement is layered, and honestly scoped:
  * RUNTIME-LOCAL (this repo, active): ``tests/test_mcp_plugin_delivery_contract.py``
    pins every literal used here to this repo's vendored copy of the contract,
    so an in-repo edit that changes a literal without the contract (or vice
    versa) fails ``unit-tests`` before any image ships.
  * SDK-SSOT DRIFT (byte-identical to the canonical contract): this repo's
    vendored ``contracts/mcp-plugin-delivery.contract.json`` is byte-gated against
    the SDK SSOT (``molecule-ai-sdk:contracts/mcp/mcp-plugin-delivery.contract.json``)
    by this repo's ``schema-sync`` workflow (``scripts/check-schemas-in-sync.sh``).
    The core/template copies are additionally guarded by
    ``mcp-plugin-delivery-contract-drift`` in molecule-core.

Literal drift between producer and consumers is the exact bug this file was
changed to fix; the contract + the runtime-local gate keep it from recurring
in this repo.
"""

import json
import logging
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

# The management lifecycle verb the controlplane online/degraded gate requires
# to consider the management MCP actually LOADED (contract ``required_tool``).
# In MOLECULE_MCP_MODE=management the @molecule-ai/mcp-server registers ONLY the
# management surface — provision_workspace is the lifecycle verb on ALL
# published versions (create_workspace is a workspace-mode tool that never ships
# on the concierge's management surface). Pinned to the contract by
# tests/test_mcp_plugin_delivery_contract.py so it can't silently drift from the
# core gate's conciergePlatformMCPRequiredTool.
REQUIRED_TOOL = "provision_workspace"

# The fully-qualified tool id the Claude dispatcher exposes for the management
# MCP's required verb (``mcp__<server>__<tool>``), composed from the two
# contract-pinned literals above. This is byte-identical to the core gate's
# conciergePlatformMCPProvisionWorkspaceTool == mcp__molecule-platform__provision_workspace
# — the exact value the heartbeat's loaded_mcp_tools must carry for the concierge
# to be marked online (core#3082 / runtime#181).
MANAGEMENT_PROVISION_TOOL_ID = f"mcp__{MANAGEMENT_MCP_NAME}__{REQUIRED_TOOL}"

# The management-MCP npm package, its SSOT-pinned version, and the scoped npm
# registry it is published to (contract
# ``management_mcp_server.{npm_package,pinned_version,registry,registry_scope}``).
# The concierge boot launches ``npx --prefer-offline <npm_package>@<version>``
# inside the runtime enumeration spawn (a HARD deadline), so every runtime IMAGE
# must PRE-BAKE this EXACT version into the agent npm cache — the base-runtime
# helper ``scripts/prebake-mgmt-mcp.sh`` does that, and templates DELEGATE to it
# (ADR-004: SDK contract -> base-runtime default -> per-adapter override-if-needed;
# no per-template bake fork). A stale/missing bake cold-pulls -> ETARGET / WAF
# throttle -> #1027 fail-close (the LAUNCH-side of RCA #2970). Pinned to the
# contract by tests/test_mcp_plugin_delivery_contract.py so the baked version can
# never silently drift from the SSOT (Guard D lockstep).
MANAGEMENT_MCP_NPM_PACKAGE = "@molecule-ai/mcp-server"
MANAGEMENT_MCP_PINNED_VERSION = "1.8.2"
MANAGEMENT_MCP_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
MANAGEMENT_MCP_REGISTRY_SCOPE = "@molecule-ai"

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

# Explicit logger name (not `__name__`) so the gate-decision log lines
# surface under a stable, grep-friendly namespace regardless of how Python's
# import system happens to spell the module path. CR2 RC 13372: the
# previous `__name__`-derived logger would surface as
# `molecule_runtime.platform_agent_identity` (varies with sys.path / package
# layout), breaking the documented "grep platform-agent.identity"
# operator contract. Pin the name explicitly.
PLATFORM_AGENT_IDENTITY_LOGGER = "platform-agent.identity"
logger = logging.getLogger(PLATFORM_AGENT_IDENTITY_LOGGER)


# ── Active-adapter MCP-config probe (runtime-agnostic gate) ─────────────────
# The "is the management MCP wired?" signal must ask the ACTIVE runtime where
# IT reads MCP servers from — not unconditionally read .claude/settings.json,
# which is meaningless on a codex/hermes concierge (the #3159 bug). The
# claude path stays the DEFAULT when no adapter probe is registered (so the
# baked-image self-heal, the legacy binary path, and every existing test keep
# working unchanged).
#
# main.py registers a probe right after it creates the adapter:
#   register_mcp_present_probe(lambda: adapter.management_mcp_present(cfg))
# A probe returns True/False ("is the management MCP wired into MY native
# config?"). When unset, the gate falls back to the claude settings.json check.
_mcp_present_probe = None  # type: callable | None


def register_mcp_present_probe(probe) -> None:
    """Register the active adapter's "is the management MCP wired?" probe.

    ``probe`` is a zero-arg callable returning bool. Pass None to clear (tests).
    main.py wires this from the resolved adapter so the gate asks the runtime
    that's actually running rather than assuming Claude's settings.json.
    """
    global _mcp_present_probe
    _mcp_present_probe = probe


# ── Active-adapter MCP launch-env provider (heartbeat readiness probe) ──────
# The heartbeat-driven readiness prober (``mcp_readiness_probe``) spawns the
# management MCP from the runtime process, but — unlike the boot enumeration
# path (``loaded_mcp_tools_probe`` / ``BaseAdapter.enumerate_loaded_mcp_tools``)
# — it does NOT hold the adapter, so it could not apply the adapter's
# ``mcp_launch_env`` overlay. On a runtime that bundles its interpreter OFF the
# system PATH (e.g. hermes' Node under ``$HERMES_HOME/node/bin``) that meant the
# prober's ``npx @molecule-ai/mcp-server`` spawn re-failed post-boot and could
# degrade the concierge via the heartbeat path (follow-up #49). main.py, the one
# place holding both the adapter and its config, registers the SAME overlay here
# so the prober can fold it UNDER each server's own env (spec.env wins).
_mcp_launch_env_provider = None  # type: callable | None


def register_mcp_launch_env_provider(provider) -> None:
    """Register the active adapter's ``mcp_launch_env`` overlay provider.

    ``provider`` is a zero-arg callable returning the adapter's launch-env
    overlay dict (``BaseAdapter.mcp_launch_env(config)`` — typically ``PATH``
    carrying the runtime's bundled interpreter bin dir). Pass None to clear
    (tests). main.py wires this from the resolved adapter so the heartbeat
    readiness prober's MCP spawn resolves ``npx``/``node`` on a runtime whose
    interpreter is off the system PATH, mirroring the boot enumeration path.
    """
    global _mcp_launch_env_provider
    _mcp_launch_env_provider = provider


def resolve_mcp_launch_env() -> dict:
    """Return the registered adapter's launch-env overlay (str->str), or ``{}``.

    Consulted by the heartbeat readiness prober's spawn to overlay the runtime's
    bundled-interpreter bin dir. When no provider is registered (the default, on
    the base claude-code runtime whose node is already on PATH) or the provider
    errors, returns ``{}`` — never raises into the prober, so a buggy provider
    can never crash readiness probing.
    """
    provider = _mcp_launch_env_provider
    if provider is None:
        return {}
    try:
        overlay = provider()
    except Exception:  # noqa: BLE001 — never let a provider crash the prober
        logger.exception(
            "platform-agent.identity: mcp_launch_env provider raised; "
            "spawning the readiness probe with no adapter overlay"
        )
        return {}
    if not isinstance(overlay, dict):
        return {}
    return {str(k): str(v) for k, v in overlay.items()}


def on_platform_agent_image() -> bool:
    """True when this container is the baked platform-agent (concierge) image.

    Reads the ``MOLECULE_PLATFORM_AGENT_IMAGE_BAKED`` env marker the
    Dockerfile.platform-agent sets. Treated as set when the value is a
    non-empty, non-"0"/"false" string so a stray ``=0`` doesn't flip it on.
    """
    val = os.environ.get(PLATFORM_AGENT_IMAGE_ENV, "").strip().lower()
    on_image = val not in ("", "0", "false", "no")
    # cp#3164 Layer-2 observability: log the env var state at boot so a
    # future #3164-style incident ("concierge LLM doesn't see the
    # management MCP") can be diagnosed from the boot logs alone. The
    # value is included so operators can spot a stale/missing
    # MOLECULE_PLATFORM_AGENT_IMAGE_BAKED without ssh-ing in.
    logger.info(
        "platform-agent.identity: env=%s=%r -> on_platform_agent_image=%s",
        PLATFORM_AGENT_IMAGE_ENV, os.environ.get(PLATFORM_AGENT_IMAGE_ENV), on_image,
    )
    return on_image


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
        # cp#3164 Layer-2 observability: log the no-op so a future
        # #3164-style incident ("concierge on an ordinary workspace image
        # OR a stale image that doesn't set the marker") is
        # diagnosable from the boot logs alone. The decision is made
        # by on_platform_agent_image() which already logs the env var.
        logger.info(
            "platform-agent.identity: ensure_management_mcp_in_settings "
            "skipped (not on platform-agent image) — relying on plugin "
            "install pipeline for any management MCP wiring"
        )
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

    Delivery- AND runtime-agnostic: a baked ``/opt/molecule-mcp-server`` binary
    OR a plugin-wired ``molecule-platform`` entry in the ACTIVE runtime's native
    MCP config both prove the org-admin MCP tooling is wired in. The active
    runtime is consulted via the registered probe (``register_mcp_present_probe``
    — main.py wires it from the resolved adapter); when no probe is registered
    the check falls back to the Claude ``settings.json`` (the default, since
    Claude Code is the base runtime). Absence of both is fail-closed.

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
    binary_exists = os.path.exists(MCPSERVER_PATH)
    # Ask the ACTIVE runtime where IT reads MCP servers from. When a probe is
    # registered (main.py wires it from the resolved adapter), it answers "is
    # the management MCP wired into MY native config?" — codex reads
    # config.toml, hermes reads its own config.yaml, etc. When no probe is
    # registered, fall back to the claude settings.json check (the default and
    # the historical behavior). A probe error is swallowed and treated as the
    # claude fallback so a buggy probe can never crash the register/heartbeat.
    settings_has = False
    probe_used = False
    probe = _mcp_present_probe
    if probe is not None:
        try:
            settings_has = bool(probe())
            probe_used = True
        except Exception:  # noqa: BLE001 — never let a probe crash the gate
            logger.exception(
                "platform-agent.identity: mcp_present probe raised; "
                "falling back to claude settings.json check"
            )
    if not probe_used:
        settings_has = _settings_has_management_mcp()
    present = binary_exists or settings_has
    # cp#3164 Layer-2 observability: log which delivery path satisfied
    # the gate (or neither). The two booleans map 1:1 to the two
    # delivery mechanisms (baked binary vs adapter-wired native config),
    # so a stuck-False case is immediately diagnosable.
    logger.info(
        "platform-agent.identity: mcp_server_present=%s "
        "(binary=%s at %s, settings_has_entry=%s via %s)",
        present, binary_exists, MCPSERVER_PATH, settings_has,
        "adapter-probe" if probe_used else "claude-settings-fallback",
    )
    return present


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


def management_mcp_diagnostic() -> dict:
    """Box-level diagnostic of WHY the management MCP is (or isn't) available.

    cp#3164 root-cause visibility: the existing Layer-2 logs
    (``on_platform_agent_image``, ``mcp_server_present``) only reach the
    container's stdout, which is INVISIBLE on a locked-down prod box (no inbound
    SSH; not shipped to any log store). That blind spot is precisely why the
    #3164 staging-E2E ("concierge can't create_workspace") stayed red and
    un-fixable: nobody could see why the ``molecule-platform`` MCP server fails
    to start. Shipping these four signals in the register/heartbeat payload lets
    the controlplane — which knows the workspace is ``kind=platform`` — record
    the cause WITHOUT box access:

      - ``on_platform_agent_image``: False ⇒ the box fell back to the plain
        runtime image, so the baked ``molecule-platform-mcp`` is absent.
      - ``mcp_command_resolved``: null ⇒ the ``molecule-platform-mcp`` command
        is not on PATH ⇒ the MCP server cannot start ⇒ ``status='failed'``.
      - ``mcp_binary_present`` / ``mcp_settings_entry``: which delivery path (if
        any) wired the MCP in.

    Computed WITHOUT re-calling the logging helpers so heartbeat cadence does
    not double the stdout log volume.
    """
    import shutil

    val = os.environ.get(PLATFORM_AGENT_IMAGE_ENV, "").strip().lower()
    return {
        "on_platform_agent_image": val not in ("", "0", "false", "no"),
        "mcp_binary_present": os.path.exists(MCPSERVER_PATH),
        "mcp_settings_entry": _settings_has_management_mcp(),
        "mcp_command_resolved": shutil.which(MANAGEMENT_MCP_COMMAND),
    }


def identity_gate_payload() -> dict:
    """Return the payload fragment the runtime sends on register/heartbeat.

    `mcp_server_present` is always present so the controlplane can treat its
    absence as fail-closed (an old/generic runtime that doesn't declare
    mcp-server availability cannot be trusted as a platform agent).

    `loaded_mcp_tools` is included ONLY once a live turn has reported a tool
    list (core#3082). Omitting it pre-first-turn keeps the gate fail-closed
    rather than asserting an empty/guessed list.

    `platform_mcp_diag` (cp#3164) ships the box-level diagnostic so a missing /
    failed management MCP is diagnosable from the controlplane without box SSH.

    `mcp_launch_failure` (#228/#1027) ships the runtime-side REFUSE-ONLINE reason
    when the management MCP could not be LAUNCHED AT ALL on this image (its pinned
    version is unresolvable — a DETERMINISTIC hard fail, not a transient stall).
    Its presence lets core fail the concierge CLOSED loudly and immediately rather
    than absorbing a permanent failure into the degrade grace window and then
    flapping. Omitted (key absent) in the healthy case so it never reads as a
    false alarm. Read via a lazy import because ``loaded_mcp_tools_probe`` imports
    THIS module — importing it at module load would be circular.
    """
    payload = {"mcp_server_present": mcp_server_present()}
    tools = loaded_mcp_tools()
    if tools is not None:
        payload["loaded_mcp_tools"] = tools
    payload["platform_mcp_diag"] = management_mcp_diagnostic()
    try:
        from molecule_runtime.loaded_mcp_tools_probe import launch_failure_reason
        reason = launch_failure_reason()
    except Exception:  # noqa: BLE001 — the gate payload must never crash a heartbeat
        reason = None
    if reason is not None:
        payload["mcp_launch_failure"] = reason
    return payload
