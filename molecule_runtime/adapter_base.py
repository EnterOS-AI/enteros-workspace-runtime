"""Base adapter interface for agent infrastructure providers."""

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    # (Temporal workflows, Durable Functions, sidecar daemons). Platform
    # scheduler skips polling workspace_schedules for this workspace,
    # avoiding double-fire on restart.
    provides_native_scheduler: bool = False

    # Durable session — adapter persists in-flight session state across
    # restarts and exposes it via pre_stop_state/restore_state. When True,
    # the platform's a2a_queue does not need to enqueue mid-session
    # requests; the adapter handles QUEUED-state on its own.
    provides_native_session: bool = False

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

    def register_subagent_hook(self, name: str, spec: dict) -> None:
        """Default no-op. Sub-agent-capable runtimes override to register a sub-agent."""
        return None

    # MCP-server config path where THIS runtime reads its mcpServers from.
    # The default DISPATCHES on self.name() through molecule_runtime.mcp_render,
    # so a codex run resolves ~/.codex/config.toml and a claude run resolves
    # .claude/settings.json — no per-template override needed. An adapter MAY
    # still override mcp_settings_path()/register_mcp_server_hook() for a runtime
    # mcp_render doesn't map.
    def mcp_settings_path(self, config: "AdapterConfig") -> str:
        """Native MCP-config file for THIS runtime (``self.name()``), absolute.

        Dispatches on the active runtime via
        :func:`molecule_runtime.mcp_render.mcp_settings_path_for` —
        ``.claude/settings.json`` for Claude Code, ``~/.codex/config.toml`` for
        codex, etc. An unmapped runtime falls back to the Claude path (the base
        runtime). Tied to the
        cross-repo delivery contract's per-runtime ``settings_path`` map."""
        from molecule_runtime.mcp_render import mcp_settings_path_for
        return str(mcp_settings_path_for(self.name(), config.config_path))

    def register_mcp_server_hook(self, config: "AdapterConfig", name: str, spec: dict) -> None:
        """Wire an MCP server into THIS runtime's native config (the MCP-wiring PORT).

        DISPATCHES on the active runtime (``self.name()``) through
        :func:`molecule_runtime.mcp_render.render_for_runtime`, so the SAME
        production path (``install_plugins_via_registry`` → this hook) renders
        the descriptor into the file the running runtime actually reads — codex →
        ``~/.codex/config.toml``, claude → ``.claude/settings.json``, hermes →
        ``~/.hermes/config.yaml`` — WITHOUT a per-template adapter override.
        This is the fix for the #3159 flaw where a concierge got the management
        MCP written to a config file its runtime never reads and so booted
        without ``create_workspace``.

        An unverified runtime (gemini/google-adk) renders via a deliberate
        NotImplementedError stub — caught by ``MCPServerAdaptor.install``, which
        fails the privileged management-MCP install LOUDLY rather than booting a
        silently capability-less concierge. An adapter for such a runtime may
        override this method once its native format is verified.
        """
        from molecule_runtime.mcp_render import render_for_runtime
        from molecule_runtime.privileged_mcp_env import inject_privileged_env

        # F2 belt-and-suspenders: enrich the privileged MCP spec for any caller
        # that invokes this hook DIRECTLY (e.g. the ensure_management_mcp_in_settings
        # self-heal), not only via install_plugins_via_registry's funnel. No-op for
        # non-management names; idempotent + descriptor-wins, so re-running over an
        # already-enriched spec changes nothing.
        spec = inject_privileged_env(name, spec)
        target = render_for_runtime(self.name(), config.config_path, name, spec)
        logger.info("register_mcp_server_hook: wired MCP %r into %s (runtime=%s)",
                    name, target, self.name())

    def management_mcp_present(self, config: "AdapterConfig") -> bool:
        """True when the privileged management MCP (``molecule-platform``) is
        wired into THIS runtime's native MCP config.

        Runtime-agnostic answer to the RCA#2970 online gate's "is the management
        MCP wired?" question — DISPATCHES on ``self.name()`` via
        :func:`molecule_runtime.mcp_render.management_mcp_present_for`, so a codex
        concierge is judged against ``~/.codex/config.toml`` (parsed as TOML) and
        a claude concierge against ``.claude/settings.json``, rather than every
        runtime being judged against a Claude file it may never read (#3159).

        main.py registers this as the gate probe via
        ``platform_agent_identity.register_mcp_present_probe`` once the adapter
        is resolved.
        """
        from molecule_runtime.mcp_render import management_mcp_present_for
        from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

        return management_mcp_present_for(self.name(), config.config_path, MANAGEMENT_MCP_NAME)

    def materialize_persona(self, config: "AdapterConfig") -> "Any":
        """Materialize the workspace's CANONICAL PERSONA into THIS runtime's
        native identity file (the persona-materialization PORT).

        DISPATCHES on the active runtime (``self.name()``) through
        :func:`molecule_runtime.persona_render.materialize_persona_for`, so a
        workspace on ANY runtime boots with its intended identity — even runtimes
        whose gateway/CLI reads a native identity file and never consumes the
        base-assembled ``config.system_prompt`` (openclaw → SOUL.md, codex →
        AGENTS.md, gemini/google-adk → GEMINI.md, claude-code → system-prompt.md).

        The canonical persona is read runtime-agnostically from the delivered
        ``config.prompt_files`` (a concierge's ``prompts/concierge.md``; a member's
        role prompt), so this is the runtime half of core #3418's provision half:
        the delivered persona actually becomes the model's on-disk identity for the
        ACTUAL runtime, not just claude-code.

        Best-effort by design: returns ``None`` (no-op) when no persona is
        delivered, and downgrades an unverified-runtime ``NotImplementedError``
        (hermes) to a warning — a persona is not a privileged capability like the
        management MCP, so a missing native convention must never brick the boot.
        Returns the path written, or ``None``.
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
        try:
            target = persona_render.materialize_persona_for(
                self.name(), config.config_path, persona
            )
        except NotImplementedError as exc:
            logger.warning(
                "materialize_persona: runtime %s has no verified native persona "
                "convention — persona NOT materialized (%s). The delivered persona "
                "still reaches any runtime that consumes config.system_prompt.",
                self.name(), exc,
            )
            return None
        if target is not None:
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
