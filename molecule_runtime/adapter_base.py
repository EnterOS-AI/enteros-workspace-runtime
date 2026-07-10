"""Base adapter interface for agent infrastructure providers."""

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2a.server.agent_execution import AgentExecutor

from molecule_runtime.event_log import DisabledEventLog, EventLogBackend

# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class PrivilegedPluginInstallError(RuntimeError):
    """Raised when a *privileged* plugin (currently: ``molecule-platform-mcp``)
    fails to install.

    Subclasses :class:`RuntimeError` so existing ``except RuntimeError`` /
    ``pytest.raises(RuntimeError)`` call sites keep working. The dedicated
    type exists so :class:`BaseAdapter.install_plugins_via_registry` and the
    outer boot-time try/except in ``main.py`` can distinguish
    "privileged-plugin setup failed" from "ordinary plugin hiccup", and so
    the privileged case can abort the runtime setup loudly rather than
    degrading to a reachable-but-misconfigured boot (which would leave the
    concierge with a configured-but-missing privileged binary and no loud
    failure signal).
    """


# ---------------------------------------------------------------------------
# Provider routing — type alias + resolver used by individual adapters.
# Each adapter defines its own ProviderRegistry with the providers it accepts.
# ---------------------------------------------------------------------------

# Maps prefix → (ordered_auth_env_vars, default_base_url).
ProviderRegistry = dict[str, tuple[tuple[str, ...], str]]


def resolve_provider_routing(
    model_str: str,
    env: Mapping[str, str],
    *,
    registry: ProviderRegistry,
    runtime_config: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Resolve a ``provider:model`` string to ``(api_key, base_url, bare_model_id)``.

    URL precedence (highest to lowest):
      1. ``<PREFIX>_BASE_URL`` env var
      2. ``runtime_config["provider_url"]``
      3. registry default for the prefix

    Unknown prefixes fall back to OPENAI_API_KEY + api.openai.com.
    Raises RuntimeError when no API key env var is set for the prefix.
    """
    if ":" in model_str:
        prefix, model_id = model_str.split(":", 1)
    else:
        prefix, model_id = "openai", model_str

    env_vars, default_url = registry.get(
        prefix, (("OPENAI_API_KEY",), "https://api.openai.com/v1")
    )
    api_key = next((env[v] for v in env_vars if env.get(v)), "")
    if not api_key:
        raise RuntimeError(
            f"No API key found for provider {prefix!r} "
            f"(checked: {', '.join(env_vars)}). Set one in workspace secrets."
        )

    env_url = env.get(f"{prefix.upper()}_BASE_URL", "")
    config_url = (runtime_config or {}).get("provider_url", "")
    base_url = env_url or config_url or default_url

    return api_key, base_url, model_id

logger = logging.getLogger(__name__)

# Shared no-op default for adapter.event_log. Safe to share across
# adapters because every DisabledEventLog method is a pure no-op with
# no per-instance state.
_DISABLED_EVENT_LOG: EventLogBackend = DisabledEventLog()


@dataclass
class SetupResult:
    """Result from the shared _common_setup() pipeline."""
    system_prompt: str
    loaded_skills: list          # LoadedSkill instances
    langchain_tools: list        # LangChain BaseTool instances
    is_coordinator: bool
    children: list               # child workspace dicts


@dataclass
class AdapterConfig:
    """Standardized config passed to every adapter."""
    model: str                              # e.g. "anthropic:claude-sonnet-4-6" or "openrouter:google/gemini-2.5-flash"
    # The assembled system prompt — BASE-OWNED (an OUTPUT, not an input).
    # None at construction; the base fills it during setup()
    # (_common_setup -> build_system_prompt, which honors config.yaml
    # prompt_files) on THIS instance, before any executor reads it. Executors
    # consume config.system_prompt; none builds or re-reads its own prompt.
    system_prompt: str | None = None
    tools: list[str] = field(default_factory=list)  # Tool names from config.yaml
    runtime_config: dict[str, Any] = field(default_factory=dict)  # Raw runtime_config block
    config_path: str = "/configs"           # Path to configs directory
    workspace_id: str = ""                  # Workspace identifier
    prompt_files: list[str] = field(default_factory=list)  # Ordered prompt file names
    a2a_port: int = 8000                    # Port for A2A server
    heartbeat: Any = None                   # HeartbeatLoop instance


@dataclass(frozen=True)
class SessionRef:
    """A single agent session in a runtime's NATIVE session store.

    The read-side value type for the session-lifecycle capability
    (``session_list`` / ``session_current`` / ``session_start`` /
    ``session_resume``). ``id`` is the runtime's OWN opaque session handle
    (a Claude Code session uuid, a hermes/openclaw ``sessionFile`` stem, a
    codex session id) — NEVER minted by the platform. Every descriptive field
    is optional so an adapter reports only what its store actually knows; the
    honest base default carries just ``id`` + ``is_active``.
    """

    id: str
    label: str | None = None
    created_at: float | None = None
    last_active_at: float | None = None
    message_count: int | None = None
    is_active: bool = False

    def to_dict(self) -> dict:
        """Plain JSON shape for the A2A op / MCP tool result (Go-safe)."""
        return {
            "id": self.id,
            "label": self.label,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "message_count": self.message_count,
            "is_active": self.is_active,
        }


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Adapter-declared ownership of cross-cutting platform capabilities.

    The platform provides FALLBACK implementations of heartbeat, cron,
    durable session, etc. When a runtime SDK provides one of these
    natively (e.g. claude-code's streaming session model, hermes-agent's
    sidecar lifecycle), the adapter sets the corresponding flag to True.
    The platform reads these flags and skips its fallback for that
    capability — the adapter is responsible instead.

    Observability is NEVER skipped: A2A protocol, activity_logs, and the
    broadcaster always run regardless of who owns the capability. These
    flags only switch WHO IMPLEMENTS the behavior, not whether the
    platform sees it.

    All defaults are False so introducing this dataclass is a no-op:
    every existing adapter inherits BaseAdapter.capabilities() which
    returns RuntimeCapabilities() with everything off, matching today's
    "platform does it all" behavior. Each capability gets a platform-
    side consumer in a follow-up PR; this class is the foundation.

    See project memory `project_runtime_native_pluggable.md` for the
    architecture principle these flags encode.
    """
    # Heartbeat — adapter sends its own keep-alive signal to the platform's
    # broadcaster instead of relying on workspace/heartbeat.py's 30s loop.
    # Set True when the SDK already maintains a long-lived session that
    # produces natural progress events (e.g. claude-code streaming).
    provides_native_heartbeat: bool = False

    # Cron / schedule — adapter handles scheduled triggers internally
    # (external workflow engines, Durable Functions, sidecar daemons). Platform
    # scheduler skips polling workspace_schedules for this workspace,
    # avoiding double-fire on restart.
    provides_native_scheduler: bool = False

    # Durable session — adapter persists in-flight session state across
    # restarts and exposes it via pre_stop_state/restore_state. When True,
    # the platform's a2a_queue does not need to enqueue mid-session
    # requests; the adapter handles QUEUED-state on its own.
    provides_native_session: bool = False

    # Session LIFECYCLE — adapter can ENUMERATE and SWITCH among MULTIPLE
    # native sessions (session_list / session_start / session_resume), which is
    # DISTINCT from provides_native_session (durability of the ONE in-flight
    # session). False ⇒ single-session: session_list reports the one current
    # session with ``supported: False`` and the canvas hides the switcher.
    # Additive: default False keeps every existing adapter single-session.
    provides_native_session_lifecycle: bool = False

    # Status lifecycle — adapter reports its own ready/degraded/failed
    # state (e.g. via heartbeat metadata). Platform respects the adapter
    # report instead of inferring status from heartbeat error rate.
    provides_native_status_mgmt: bool = False

    # Retry — adapter handles transient errors (rate limits, 5xx) with
    # its own backoff. Platform stops re-dispatching A2A requests that
    # the adapter explicitly marked as "retrying internally".
    provides_native_retry: bool = False

    # Activity log decoration — adapter contributes runtime-specific
    # fields (model, token_count, latency breakdown) into activity_log
    # rows alongside the platform-defined columns.
    provides_activity_decoration: bool = False

    # Channel dispatch — adapter sends to external channels (Slack,
    # Lark, etc.) directly instead of routing through platform channels
    # manager. Used when the SDK has built-in channel integrations.
    provides_channel_dispatch: bool = False

    def to_dict(self) -> dict[str, bool]:
        """Serializable shape for the heartbeat payload + /capabilities
        endpoint. Plain dict avoids leaking dataclass internals to Go."""
        return {
            "heartbeat": self.provides_native_heartbeat,
            "scheduler": self.provides_native_scheduler,
            "session": self.provides_native_session,
            "session_lifecycle": self.provides_native_session_lifecycle,
            "status_mgmt": self.provides_native_status_mgmt,
            "retry": self.provides_native_retry,
            "activity_decoration": self.provides_activity_decoration,
            "channel_dispatch": self.provides_channel_dispatch,
        }


class BaseAdapter(ABC):
    """Interface every agent infrastructure adapter must implement.

    To add a new agent infra:
    1. Create a standalone template repo (molecule-ai-workspace-template-<infra>)
    2. Implement adapter.py with a class extending BaseAdapter
    3. Add requirements.txt with your infra's dependencies + molecule-runtime
    4. Set ADAPTER_MODULE in the Dockerfile to your adapter module path

    Cross-cutting capabilities your adapter can opt into:
    - capabilities() — declare native ownership of heartbeat, scheduler,
      session, status mgmt, etc. (see RuntimeCapabilities above)
    - idle_timeout_override() — extend the platform's per-dispatch
      silence window for SDKs with long synth turns
    - runtime_wedge.mark_wedged() / clear_wedge() — flip the workspace
      to `degraded` + auto-recover when your SDK hits a non-recoverable
      error class. Import directly from `runtime_wedge`; the heartbeat
      forwards the state to the platform automatically. See the
      runtime_wedge module docstring for the integration recipe.
    """

    @staticmethod
    @abstractmethod
    def name() -> str:  # pragma: no cover
        """Return the runtime identifier (e.g. 'claude-code', 'codex').
        This must match the 'runtime' field in config.yaml."""
        ...

    @staticmethod
    @abstractmethod
    def display_name() -> str:  # pragma: no cover
        """Human-readable name for UI display."""
        ...

    @staticmethod
    @abstractmethod
    def description() -> str:  # pragma: no cover
        """Short description of what this adapter provides."""
        ...

    @staticmethod
    def get_config_schema() -> dict:
        """Return JSON Schema for runtime_config fields this adapter supports.
        Used by the Config tab UI to render the right form fields.
        Override in subclasses for adapter-specific settings."""
        return {}

    def capabilities(self) -> "RuntimeCapabilities":
        """Declare which cross-cutting capabilities this adapter owns
        natively vs delegates to platform fallback.

        Default returns RuntimeCapabilities() — every flag False, meaning
        the platform owns everything (today's behavior). Adapters override
        to declare native ownership; e.g. claude-code's adapter returns
        RuntimeCapabilities(provides_native_heartbeat=True,
                             provides_native_session=True).

        Subsequent platform-side consumers (idle-timeout override,
        scheduler skip, etc.) read this and route accordingly. See
        project memory `project_runtime_native_pluggable.md`."""
        return RuntimeCapabilities()

    def idle_timeout_override(self) -> int | None:
        """Per-A2A-dispatch silence window override, in SECONDS.

        Return None to use the platform default (env var
        A2A_IDLE_TIMEOUT_SECONDS, falling back to 5 minutes — see
        a2a_proxy.go:defaultIdleTimeoutDuration). Override when this
        runtime's SDK can legitimately go silent longer than the
        default before the dispatch should be considered wedged.

        Why this is per-adapter, not just env: the env value is a
        cluster-wide knob set by ops. Different SDKs have different
        latency profiles — claude-code synthesis on Opus + tool use
        legitimately runs 8-10 min between broadcasts; hermes synth
        with custom providers can be even slower. Hardcoding 5min for
        everyone either cancels real work (claude-code synth) or leaves
        other maintained runtimes hanging too long.

        Platform reads this from the heartbeat payload and stashes
        it per-workspace; dispatchA2A consults it before applying the
        idle timer. None / unset / zero falls through to the global
        default — same behavior as before this hook landed."""
        return None

    @property
    def event_log(self) -> EventLogBackend:
        """Pluggable in-process event-log backend.

        Adapters MAY call ``self.event_log.append(kind=..., payload=...)``
        to record runtime-internal events (tool dispatch, skill load,
        executor errors, peer-handoff). Readers query the buffer via
        the platform's ``/workspaces/:id/activity`` endpoint with a
        cursor — see ``event_log.py`` for the protocol.

        Default: shared ``DisabledEventLog`` no-op, so adapters that
        never set this still link cleanly. ``main.py`` overrides at boot
        from the ``observability.event_log`` config block."""
        return getattr(self, "_event_log", None) or _DISABLED_EVENT_LOG

    @event_log.setter
    def event_log(self, backend: EventLogBackend) -> None:
        self._event_log = backend

    # ------------------------------------------------------------------
    # Plugin install hooks
    # ------------------------------------------------------------------
    # New pipeline: each plugin ships per-runtime adaptors resolved via
    # `plugins_registry.resolve()`. Adapters expose hooks below that
    # adaptors call to wire plugin content into the runtime.
    #
    # Default implementations are filesystem-only (write to /configs,
    # append to CLAUDE.md). Runtimes with a dynamic tool registry
    # (e.g. sub-agent-capable runtimes) override the hooks to also register
    # in-process state.

    def memory_filename(self) -> str:
        """File under /configs that the runtime treats as long-lived memory.

        Both Claude Code and maintained runtimes can read CLAUDE.md natively, so this is
        the sensible default. Override only if a runtime expects a different
        filename.
        """
        return "CLAUDE.md"

    def register_tool_hook(self, name: str, fn) -> None:
        """Default no-op. Override on runtimes with a dynamic tool registry.

        Runtimes that pick tools up at startup via filesystem scan (Claude
        Code reads /configs/skills, native runtime globs **/*.py) don't need to
        do anything here — the adaptor's file-write step is enough.
        """
        return None

    async def transcript_lines(self, since: int = 0, limit: int = 100) -> dict:
        """Return live transcript entries for the most-recent agent session.

        Default implementation returns ``supported: False`` for runtimes
        that don't expose a per-session log on disk. Override in subclasses
        that DO (Claude Code reads ``~/.claude/projects/<cwd>/<session>.jsonl``).

        This is the "look over the agent's shoulder" feature — lets canvas /
        operators see live tool calls + AI thinking instead of waiting for
        the high-level activity log to flush.

        Args:
            since: line offset to skip — caller's last cursor (0 = from start)
            limit: max lines to return (caller-side cap, default 100, max 1000)

        Returns:
            ``{runtime, supported, lines, cursor, more, source}`` where
            ``cursor`` is the new offset to pass on the next poll, ``more``
            is True if additional lines remain past ``limit``, and ``source``
            is the file path lines were read from (useful for debugging).
        """
        return {
            "runtime": self.name(),
            "supported": False,
            "lines": [],
            "cursor": since,
            "more": False,
            "source": None,
        }

    def pre_stop_state(self) -> dict:
        """Capture in-memory state for pause/resume serialization.

        Called by main.py's shutdown handler just before the container exits.
        Returns a dict that will be scrubbed (via lib.snapshot_scrub) and
        written to /configs/.agent_snapshot.json.

        Default implementation:
        1. Attempts to read ``self._executor._session_id`` (set by
           create_executor) and includes it as ``session_id``.
        2. Includes up to 200 recent transcript lines via transcript_lines().

        Override in adapters that hold additional in-memory state that
        should survive a container stop.

        Returns:
            A JSON-serializable dict. All string values are scrubbed before
            persisting, so it is safe to include raw content from the
            agent's context.
        """
        from molecule_runtime.lib.pre_stop import MAX_TRANSCRIPT_LINES

        state: dict = {}

        # Session handle — critical for resuming the Claude Code session.
        executor = getattr(self, "_executor", None)
        if executor is not None:
            session_id = getattr(executor, "_session_id", None)
            if session_id:
                state["session_id"] = session_id

        # Recent conversation log — captures where the agent left off.
        # transcript_lines() may be async; call it synchronously if possible,
        # otherwise let async adapters override pre_stop_state entirely.
        try:
            import inspect as _inspect
            transcript_fn = self.transcript_lines
            if _inspect.iscoroutinefunction(transcript_fn):
                # Async adapter — override pre_stop_state() for transcript access.
                # The base impl still captures session_id above.
                pass
            else:
                transcript = transcript_fn(since=0, limit=MAX_TRANSCRIPT_LINES)
                if transcript.get("supported"):
                    state["transcript_lines"] = transcript.get("lines", [])
        except Exception:
            # Best-effort: never let transcript capture failure block serialization.
            pass

        return state

    def restore_state(self, snapshot: dict) -> None:
        """Restore in-memory state from a pause/resume snapshot.

        Called by main.py on first boot when /configs/.agent_snapshot.json
        exists. Gives the adapter a chance to restore session handles,
        conversation context, or any other in-memory state before the A2A
        server starts accepting requests.

        Default implementation stores ``snapshot["session_id"]`` and
        ``snapshot["transcript_lines"]`` as ``self._snapshot_session_id``
        and ``self._snapshot_transcript`` so that ``create_executor()`` or
        the executor itself can pick them up.

        Args:
            snapshot: The scrubbed snapshot dict previously written by
                     pre_stop_state(). All secrets have already been redacted.
        """
        self._snapshot_session_id: str | None = snapshot.get("session_id")
        self._snapshot_transcript: list | None = snapshot.get("transcript_lines")

    # ---- Session lifecycle (ADR-004 capability seam) --------------------
    # The SDK-owned session surface: enumerate + switch among a runtime's
    # NATIVE sessions. These base defaults are the honest single-session
    # fallback — like transcript_lines(), an adapter with no multi-session
    # store reports ``supported: False`` rather than faking a list. The
    # official adapters (claude-code / hermes / openclaw / codex) OVERRIDE
    # the READ side (session_list / session_current) to read their native
    # store (e.g. ~/.claude/projects/<cwd>/<session>.jsonl). The WRITE side
    # (session_start / session_resume routing) is a follow-up phase; the base
    # keeps it a no-op so this whole seam is purely ADDITIVE — it changes no
    # existing routing and cannot regress the stable-session contract in
    # subprocess_executor.py.

    def _current_session_ref(self) -> "SessionRef | None":
        """The active session as a SessionRef, from the executor's stable id.

        Reads ``self._executor._session_id`` (the workspace-keyed stable id
        the SessionManager resumes each turn). Returns None before an
        executor/session exists. Adapters with a richer native store override
        session_current() to add label/timestamps.
        """
        executor = getattr(self, "_executor", None)
        session_id = getattr(executor, "_session_id", None) if executor else None
        if not session_id:
            return None
        return SessionRef(id=str(session_id), is_active=True)

    async def session_current(self) -> dict:
        """Return the ACTIVE session — the one turns route to right now.

        Default reads the stable workspace-keyed id via _current_session_ref().
        Returns ``{runtime, supported, session}`` where ``session`` is a
        SessionRef dict or None. ``supported`` mirrors the lifecycle
        capability flag; the current session is reported honestly regardless.
        """
        ref = self._current_session_ref()
        return {
            "runtime": self.name(),
            "supported": self.capabilities().provides_native_session_lifecycle,
            "session": ref.to_dict() if ref else None,
        }

    async def session_list(self) -> dict:
        """List the workspace's native sessions (read side).

        Honest single-session default: reports the ONE current session with
        ``supported: False``. Adapters that keep a native session store
        (claude-code JSONL projects dir, hermes/openclaw sessionFile, codex
        sessions) OVERRIDE this to enumerate the store — id, label (first
        user line), created/last-active, message_count — and set
        ``supported: True``.

        Returns:
            ``{runtime, supported, sessions, active_id}`` where ``sessions``
            is a list of SessionRef dicts and ``active_id`` is the id turns
            currently route to (or None).
        """
        ref = self._current_session_ref()
        return {
            "runtime": self.name(),
            "supported": False,
            "sessions": [ref.to_dict()] if ref else [],
            "active_id": ref.id if ref else None,
        }

    async def session_start(self, label: str | None = None) -> dict:
        """Begin a NEW native session (write side) — an explicit clean slate.

        Base default is a no-op that reports ``supported: False`` (a
        single-session runtime cannot open a second session). The active-
        session pointer + the ``subprocess_executor`` indirection that make
        this route to a fresh native session are a follow-up phase, so this
        method changes no routing today.

        Args:
            label: optional human title for the new session (adapters that
                keep titles use it; the default ignores it).

        Returns:
            ``{runtime, supported, session, started}``.
        """
        return {
            "runtime": self.name(),
            "supported": False,
            "session": None,
            "started": False,
        }

    async def session_resume(self, session_id: str) -> dict:
        """Switch the active session to an existing one (write side).

        Base default is a no-op reporting ``supported: False``. Adapters with
        a native store + the active-pointer follow-up validate ``session_id``
        exists and route subsequent turns to it.

        Args:
            session_id: the native session id to resume (from session_list).

        Returns:
            ``{runtime, supported, session, resumed}``.
        """
        return {
            "runtime": self.name(),
            "supported": False,
            "session": None,
            "resumed": False,
        }

    def register_subagent_hook(self, name: str, spec: dict) -> None:
        """Default no-op. Sub-agent-capable runtimes override to register a sub-agent."""
        return None

    # MCP-config seam (ADR-004 §3). These base defaults are name-AGNOSTIC: per
    # ADR-004 the per-runtime shape (renderers/readers/present-probes) moved OUT
    # of the shared engine and INTO each adapter, so the base NO LONGER dispatches
    # on self.name(). The four official adapters (claude-code / codex / hermes /
    # openclaw) OVERRIDE every method below to their OWN native format in their
    # template repos, so these defaults are only the fallback for a not-yet-migrated
    # / third-party adapter that hasn't implemented the seam. The default is a
    # single generic JSON ``mcpServers`` native config (mcp_render's name-free
    # helpers) at the reference path ``<config_path>/.claude/settings.json`` — it
    # spells no runtime name. An adapter whose runtime reads a different file/format
    # MUST override these (that is what the official four do).
    def mcp_settings_path(self, config: "AdapterConfig") -> str:
        """Default native MCP-config file, absolute — the generic JSON
        ``mcpServers`` config at ``<config_path>/.claude/settings.json``.

        Name-agnostic base fallback (ADR-004: no self.name() dispatch). An adapter
        whose runtime reads a different native file (codex ``~/.codex/config.toml``,
        openclaw ``~/.openclaw/openclaw.json``, hermes ``~/.hermes/config.yaml``)
        OVERRIDES this — the official four all do."""
        from molecule_runtime.mcp_render import default_json_settings_path
        return str(default_json_settings_path(config.config_path))

    def register_mcp_server_hook(self, config: "AdapterConfig", name: str, spec: dict) -> None:
        """Wire an MCP server into the default native config (the MCP-wiring PORT).

        Name-agnostic base fallback (ADR-004 §3): additively merges the
        ``{command, args?, env?}`` descriptor into the generic JSON ``mcpServers``
        map at the default path (``mcp_render.render_json_mcp_servers`` on
        ``mcp_settings_path``). Additive + idempotent; writes only that one file.
        An adapter whose runtime reads a different native format OVERRIDES this to
        render its OWN file — the official four (claude-code JSON, codex TOML,
        hermes/openclaw YAML/JSON) all do, so this fallback is only for a
        not-yet-migrated / third-party adapter.

        Enriches the privileged management-MCP spec via ``inject_privileged_env``
        first (no-op for non-management names; idempotent; descriptor-wins) — the
        same enrichment the install funnel applies — so a direct caller (e.g. the
        ensure_management_mcp_in_settings self-heal) gets the enriched spec too.
        """
        from molecule_runtime.mcp_render import render_json_mcp_servers
        from molecule_runtime.privileged_mcp_env import inject_privileged_env

        # F2 belt-and-suspenders: enrich the privileged MCP spec for any caller
        # that invokes this hook DIRECTLY (e.g. the ensure_management_mcp_in_settings
        # self-heal), not only via install_plugins_via_registry's funnel. No-op for
        # non-management names; idempotent + descriptor-wins, so re-running over an
        # already-enriched spec changes nothing.
        spec = inject_privileged_env(name, spec)
        target = Path(self.mcp_settings_path(config))
        render_json_mcp_servers(target, name, spec)
        logger.info("register_mcp_server_hook: wired MCP %r into %s (runtime=%s)",
                    name, target, self.name())

    def management_mcp_present(self, config: "AdapterConfig") -> bool:
        """True when the privileged management MCP (``molecule-platform``) is
        wired into the default native MCP config.

        Name-agnostic base fallback (ADR-004: no self.name() dispatch) — judges the
        generic JSON ``mcpServers`` config at ``mcp_settings_path``
        (``mcp_render.json_mcp_servers_has``). Fail-CLOSED: a missing / unreadable /
        malformed / structurally-unexpected config yields ``False``, so a genuinely
        MCP-less concierge stays degraded at the RCA#2970 gate. An adapter whose
        runtime reads a different native file OVERRIDES this (the official four do).

        main.py registers this as the gate probe via
        ``platform_agent_identity.register_mcp_present_probe`` once the adapter
        is resolved.
        """
        from molecule_runtime.mcp_render import json_mcp_servers_has
        from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

        return json_mcp_servers_has(Path(self.mcp_settings_path(config)), MANAGEMENT_MCP_NAME)

    def mcp_launch_env(self, config: "AdapterConfig") -> dict[str, str]:
        """Runtime-specific environment overlay for SPAWNED MCP-server children.

        ADR-004 socket (adapters own runtime-specific concerns; the shared engine
        stays runtime-agnostic). When the runtime spawns a stdio MCP server — the
        boot enumeration probe, and the live model's MCP launch — the child inherits
        the runtime process env. But a runtime that bundles its OWN interpreter (its
        node/python) OFF the system PATH (e.g. hermes ships Node 22 under
        ``$HERMES_HOME/node/bin``, NOT on PATH) leaves the child unable to resolve
        ``npx``/``node`` — so ``npx @molecule-ai/mcp-server`` never launches, the
        management MCP never enumerates, and the concierge is stuck provisioning.

        This method contributes the DELTA an adapter needs merged into each spawned
        MCP server's env (typically ``PATH`` with the runtime's bundled interpreter
        bin dir prepended). Merge precedence at the spawn site is:
        ``os.environ`` (base) < this overlay (adapter) < the per-server ``spec.env``
        (descriptor always wins), so a server that pins its own env is never
        overridden by the adapter overlay.

        BASE default: ``{}`` — NO runtime-specific injection. The name-agnostic base
        (ADR-004: no ``self.name()`` dispatch) assumes the runtime's interpreter is
        already on the process PATH (system node), so the child inherits it as-is.
        A runtime bundling its own interpreter OVERRIDES this to DYNAMICALLY resolve
        + verify its bin dir at launch (never a static image-build PATH hardcode) and
        prepend it — resolution stays in the adapter, so the engine spells no runtime
        name and no interpreter path.
        """
        return {}

    async def enumerate_loaded_mcp_tools(
        self, config: "AdapterConfig"
    ) -> "list[str] | None":
        """Return the LOADED MCP tool ids this runtime actually has, or None.

        RUNTIME CONTRACT (runtime#181): each adapter answers for ITS OWN runtime,
        however it knows best. This name-agnostic base fallback (ADR-004: no
        self.name() dispatch) reads the DEFAULT native config's declared servers
        (the generic JSON ``mcpServers`` map at ``mcp_settings_path``, via
        ``mcp_render.read_json_mcp_servers``) and hands the resolved ``{name: spec}``
        map to the shared boot-safe stdio probe engine
        (:func:`loaded_mcp_tools_probe.enumerate_from_specs_async`) — the generic,
        runtime-name-free engine the platform keeps. An adapter whose native MCP
        config lives elsewhere / in another format OVERRIDES this to read its own
        config and feed the same probe engine — the official four all do.

        TRI-STATE (identical to the loaded_mcp_tools producer contract):
          * ``None`` — nothing could be observed (no servers declared, or all
            probes failed/stalled/unreadable). Producer left unset → heartbeat
            omits the field → core's grace window applies. NEVER a guessed list.
          * ``[]``   — a server genuinely connected and advertised zero tools.
          * ``[ids]`` — deduped/sorted union of observed ``mcp__<server>__<tool>``
            ids.

        BOOT-SAFE + NEVER-RAISES: ``enumerate_from_specs_async`` bounds the whole
        probe by the enumeration deadline and maps every failure to None.
        """
        from molecule_runtime.loaded_mcp_tools_probe import enumerate_from_specs_async
        from molecule_runtime.mcp_render import read_json_mcp_servers

        try:
            servers = read_json_mcp_servers(Path(self.mcp_settings_path(config)))
        except Exception:  # noqa: BLE001 — enumeration must never crash boot
            logger.warning(
                "enumerate_loaded_mcp_tools: could not read declared MCP servers "
                "for runtime=%s — leaving producer unset (grace window applies)",
                self.name(), exc_info=True,
            )
            return None
        # Pass the adapter's launch-env overlay so a runtime bundling its own
        # interpreter (off the system PATH) can make node/npx resolvable for the
        # spawned server; the base default is {} (system interpreter on PATH).
        return await enumerate_from_specs_async(
            servers, launch_env=self.mcp_launch_env(config)
        )

    def materialize_persona(self, config: "AdapterConfig") -> "Any":
        """Materialize the workspace's CANONICAL PERSONA into the default native
        identity file (the persona-materialization PORT).

        Name-agnostic base fallback (ADR-004: no self.name() dispatch). Reads the
        canonical persona runtime-agnostically from the delivered
        ``config.prompt_files`` (``persona_render.read_canonical_persona`` — the one
        generic, runtime-name-free helper the engine keeps), then writes it to the
        DEFAULT identity file ``<config_path>/system-prompt.md``
        (``persona_render.write_persona`` at ``persona_render.default_persona_path``).
        An adapter whose runtime reads a different native identity file (openclaw
        SOUL.md, codex AGENTS.md, hermes ~/.hermes/SOUL.md) OVERRIDES this to write
        its OWN file — the official four all do, so this fallback is only for a
        not-yet-migrated / third-party adapter.

        Best-effort by design: returns ``None`` (no-op) when no persona is
        delivered — a persona is not a privileged capability like the management
        MCP, so this never bricks boot. Returns the path written, or ``None``.
        """
        from molecule_runtime import persona_render

        persona = persona_render.read_canonical_persona(
            config.config_path, config.prompt_files
        )
        if not (persona or "").strip():
            logger.info(
                "materialize_persona: no canonical persona delivered for runtime "
                "%s — leaving the runtime's native default untouched",
                self.name(),
            )
            return None
        target = persona_render.default_persona_path(config.config_path)
        persona_render.write_persona(target, persona)
        logger.info(
            "materialize_persona: wrote %s persona (%d chars) to %s",
            self.name(), len(persona), target,
        )
        return target

    def append_to_memory_hook(self, config: AdapterConfig, filename: str, content: str) -> None:
        """Append text to the durable memory file if the marker isn't present.

        MUST-FIX (memory WRITE-path reconciliation): with the mailbox kernel ON
        this writes to the durable mailbox memory dir
        (``/workspace/.molecule/memory/<filename>``) — the SAME directory
        ``prompt.py`` READS memory snapshots from — so plugin-injected memory
        survives a restart and is never shadowed by a stale ``/configs`` copy.
        Kernel OFF keeps the legacy ``/configs/<filename>`` target so the flow
        is byte-identical.

        Idempotent: looks for the first line of `content` as a marker so a
        re-install doesn't duplicate the block. Adaptors should pass content
        beginning with a unique header (e.g. ``# Plugin: molecule-dev-conventions``).
        """
        import os

        import molecule_runtime.mailbox_dir as mailbox_dir

        if mailbox_dir.kernel_enabled():
            target = str(mailbox_dir.memory_file(filename))
        else:
            target = os.path.join(config.config_path, filename)
        marker = content.splitlines()[0].strip() if content else ""
        existing = ""
        if os.path.exists(target):
            with open(target) as f:
                existing = f.read()
            if marker and marker in existing:
                logger.info("append_to_memory: %s already contains %r — skipping", filename, marker)
                return
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(content if content.endswith("\n") else content + "\n")
        logger.info("append_to_memory: appended %d chars to %s", len(content), filename)

    async def install_plugins_via_registry(
        self,
        config: AdapterConfig,
        plugins,
    ) -> list:
        """Drive the new per-runtime adaptor pipeline for every loaded plugin.

        For each plugin in `plugins.plugins`, resolve the adaptor for this
        runtime (via :func:`plugins_registry.resolve`) and invoke
        ``install(ctx)``. Returns the list of :class:`InstallResult` so
        callers can surface warnings (e.g. raw-drop fallback hits).

        Adapters whose runtime supports the new pipeline call this from
        ``setup()`` instead of the legacy ``inject_plugins()``.
        """
        from pathlib import Path
        from molecule_runtime.plugins_registry import InstallContext, resolve
        from molecule_runtime.plugins_registry.builtins import _PRIVILEGED_MCP_PLUGIN
        from molecule_runtime.privileged_mcp_env import inject_privileged_env

        results = []
        runtime = self.name().replace("-", "_")  # e.g. "claude-code" -> "claude_code"

        for plugin in plugins.plugins:
            adaptor, source = resolve(plugin.name, runtime, Path(plugin.path))
            ctx = InstallContext(
                configs_dir=Path(config.config_path),
                workspace_id=config.workspace_id,
                runtime=runtime,
                plugin_root=Path(plugin.path),
                memory_filename=self.memory_filename(),
                register_tool=self.register_tool_hook,
                register_subagent=self.register_subagent_hook,
                # F2: enrich the privileged MCP spec at the ONE base funnel all
                # adapters share, BEFORE dispatch — so even an overriding hook
                # (openclaw) receives the pre-merged org-admin env. inject_privileged_env
                # no-ops for any name != the management MCP and is descriptor-wins +
                # idempotent, so ordinary MCP specs and the proven flow are unchanged.
                register_mcp_server=lambda n, s, _cfg=config: self.register_mcp_server_hook(
                    _cfg, n, inject_privileged_env(n, s)
                ),
                append_to_memory=lambda fn, c, _cfg=config: self.append_to_memory_hook(_cfg, fn, c),
            )
            try:
                result = await adaptor.install(ctx)
                results.append(result)
                if result.errors:
                    logger.error(
                        "Plugin %s installed via %s with %d error(s): %s",
                        plugin.name, source, len(result.errors), "; ".join(result.errors),
                    )
                logger.info(
                    "Plugin %s installed via %s (warnings: %d, errors: %d)",
                    plugin.name, source, len(result.warnings), len(result.errors),
                )
            except PrivilegedPluginInstallError:
                # Privileged plugin setup failed — re-raise so the runtime
                # boot fails loudly (caller in main.py checks the type and
                # aborts rather than degrading to a "reachable-but-misconfigured"
                # workspace, which would leave the concierge with a
                # configured-but-missing privileged binary and no loud signal).
                raise
            except Exception as exc:
                logger.exception("Plugin %s install via %s failed: %s", plugin.name, source, exc)
                if plugin.name == _PRIVILEGED_MCP_PLUGIN:
                    # Defensive: if a non-PrivilegedPluginInstallError exception
                    # still comes from the privileged plugin, re-raise it
                    # anyway. The spec (#151) is "fail loudly when the privileged
                    # plugin's setup.sh fails" — a swallowed Exception on that
                    # path is the regression we are guarding against.
                    raise

        return results

    async def inject_plugins(self, config: AdapterConfig, plugins) -> None:
        """Legacy hook — kept for backwards compatibility during migration.

        Default: drive the new per-runtime adaptor pipeline. Adapters not yet
        migrated may still override this with their own logic.
        """
        await self.install_plugins_via_registry(config, plugins)

    async def _common_setup(self, config: AdapterConfig) -> SetupResult:
        """Shared setup pipeline — loads plugins, skills, tools, coordinator, and builds system prompt.

        All adapters can call this to get the full platform feature set.
        Returns a SetupResult with LangChain BaseTool instances that adapters
        convert to their native format if needed.
        """
        from molecule_runtime.plugins import load_plugins
        from molecule_runtime.skill_loader.loader import load_skills
        from molecule_runtime.coordinator import get_children, build_children_description
        from molecule_runtime.prompt import build_system_prompt, get_peer_capabilities, get_platform_instructions
        from molecule_runtime.builtin_tools.approval import request_approval
        from molecule_runtime.builtin_tools.delegation import delegate_task, delegate_task_async, check_task_status
        from molecule_runtime.builtin_tools.memory import commit_memory, recall_memory
        from molecule_runtime.builtin_tools.sandbox import run_code

        platform_url = os.environ.get("PLATFORM_URL", "http://host.docker.internal:8080")

        # Load plugins from per-workspace dir first, then shared fallback
        workspace_plugins_dir = os.path.join(config.config_path, "plugins")
        plugins = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=os.environ.get("PLUGINS_DIR", "/plugins"),
        )
        await self.inject_plugins(config, plugins)
        if plugins.plugin_names:
            logger.info(f"Plugins: {', '.join(plugins.plugin_names)}")

        # Protected platform-MCP self-heal (RCA #2970). On the baked
        # platform-agent (concierge) image, ALWAYS re-assert the
        # ``molecule-platform`` management MCP entry in settings.json AFTER the
        # plugin merges above — additively, never evicting a user plugin. This
        # makes the desired set "protected-platform-entries ∪ declared-user-
        # plugins": a user-plugin install (which triggers a fresh-instance
        # restart) can no longer drop the management MCP, and a failed per-boot
        # fetch of the private molecule-platform-mcp plugin self-heals instead of
        # fail-closing the concierge. No-op on ordinary workspace images.
        try:
            from molecule_runtime.platform_agent_identity import (
                ensure_management_mcp_in_settings,
            )
            if ensure_management_mcp_in_settings():
                logger.info(
                    "platform-agent: re-asserted protected management MCP "
                    "(molecule-platform) into settings.json"
                )
        except Exception:  # noqa: BLE001 — self-heal must never block boot
            logger.exception("platform-agent: management MCP self-heal failed")

        # Load skills (workspace + plugin skills, deduped). Pass the runtime
        # name so SKILL.md frontmatter `runtime: [...]` can opt skills out
        # of incompatible adapters (hermes won't load claude-code-only
        # skills, etc.).
        runtime_name = type(self).name()
        loaded_skills = load_skills(config.config_path, config.tools, current_runtime=runtime_name)
        seen_skill_ids = {s.metadata.id for s in loaded_skills}
        for plugin_skills_dir in plugins.skill_dirs:
            plugin_skill_names = [
                d for d in os.listdir(plugin_skills_dir)
                if os.path.isdir(os.path.join(plugin_skills_dir, d))
            ]
            for skill in load_skills(plugin_skills_dir, plugin_skill_names, current_runtime=runtime_name):
                if skill.metadata.id not in seen_skill_ids:
                    loaded_skills.append(skill)
                    seen_skill_ids.add(skill.metadata.id)
        logger.info(f"Loaded {len(loaded_skills)} skills: {[s.metadata.id for s in loaded_skills]}")

        # Core platform tools — names mirror the platform_tools registry,
        # so the names referenced in get_a2a_instructions/get_hma_instructions
        # are guaranteed to exist as @tool symbols here. The structural
        # alignment test in tests/test_platform_tools.py pins this.
        all_tools = [
            delegate_task, delegate_task_async, check_task_status,
            request_approval, commit_memory, recall_memory, run_code,
        ]
        for skill in loaded_skills:
            all_tools.extend(skill.tools)

        # Coordinator mode: detect children and add routing tool
        children = await get_children()
        is_coordinator = len(children) > 0
        if is_coordinator:
            from molecule_runtime.coordinator import route_task_to_team
            logger.info(f"Coordinator mode: {len(children)} children")
            all_tools.append(route_task_to_team)

        # Build system prompt with all context. Parent→child knowledge sharing
        # was previously handled by `shared_context` (parent's config.yaml file
        # paths injected into the child's prompt at boot). That path was removed
        # — agents now pull team-scoped knowledge via memory v2's team:<id>
        # namespace (recall_memory) on demand instead of paying for it on every
        # boot regardless of need. See RFC #2789 for the future shared-file
        # storage that complements this for large blob-shaped artefacts.
        peers = await get_peer_capabilities(platform_url, config.workspace_id)
        platform_instructions = await get_platform_instructions(platform_url, config.workspace_id)
        coordinator_prompt = build_children_description(children) if is_coordinator else ""
        extra_prompts = list(plugins.prompt_fragments)
        if coordinator_prompt:
            extra_prompts.append(coordinator_prompt)

        # Orchestrator-only guardrail gate (platform/concierge ONLY). A concierge
        # (kind='platform') has the org-management MCP wired in; a normal worker
        # does not. mcp_server_present() is the runtime-side platform-ness signal
        # (baked binary OR the active adapter's management-MCP probe, registered
        # in main.py BEFORE setup()). True → inject the never-self-do guardrail so
        # the concierge orchestrates instead of self-executing, EVEN on a stale
        # template. False (every worker) → no guardrail; workers must do real work.
        # Defensive: a predicate error must never gag a worker nor crash boot, so
        # default to False (worker) on any exception.
        try:
            from molecule_runtime.platform_agent_identity import mcp_server_present
            is_platform_agent = mcp_server_present()
        except Exception:  # noqa: BLE001 — never let the gate crash boot
            logger.exception(
                "orchestrator-guardrail: mcp_server_present() raised; "
                "defaulting to worker (no guardrail injected)"
            )
            is_platform_agent = False

        system_prompt = build_system_prompt(
            config.config_path, config.workspace_id, loaded_skills, peers,
            prompt_files=config.prompt_files,
            plugin_rules=plugins.rules,
            plugin_prompts=extra_prompts,
            platform_instructions=platform_instructions,
            platform_guardrail=is_platform_agent,
        )

        # SSOT: publish the single base-built system prompt (which honors
        # config.yaml `prompt_files`, with the `system-prompt.md` fallback baked
        # into build_system_prompt) back onto the shared AdapterConfig instance.
        # main.py passes this SAME AdapterConfig to create_executor, so every
        # runtime executor can consume ONE source via config.system_prompt
        # instead of each re-reading /configs/system-prompt.md itself (the
        # per-runtime drift that left the concierge identity-less). Idempotent.
        config.system_prompt = system_prompt

        return SetupResult(
            system_prompt=system_prompt,
            loaded_skills=loaded_skills,
            langchain_tools=all_tools,
            is_coordinator=is_coordinator,
            children=children,
        )

    @abstractmethod
    async def setup(self, config: AdapterConfig) -> None:
        """One-time setup: validate config, prepare internal state.
        Called after deps are installed but before create_executor().
        Raise RuntimeError if setup fails (missing deps, bad config, etc.)."""
        ...  # pragma: no cover

    @abstractmethod
    async def create_executor(self, config: AdapterConfig) -> AgentExecutor:
        """Create and return an AgentExecutor ready for A2A integration.
        The returned executor's execute() method will be called by the
        A2A server's DefaultRequestHandler.

        Subclasses should also store the returned executor as ``self._executor``
        so ``pre_stop_state()`` can access it for serialization.
        """
        ...  # pragma: no cover
