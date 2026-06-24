"""Load workspace configuration from config.yaml."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class RBACConfig:
    """Role-based access control settings for this workspace.

    ``roles`` declares what this workspace is *allowed* to do.  Each role
    name maps to a set of permitted actions.  Built-in roles are defined in
    ``tools/audit.ROLE_PERMISSIONS``; custom roles can be added via
    ``allowed_actions``.

    Built-in roles
    --------------
    admin           All actions (delegate, approve, memory.read, memory.write)
    operator        Same as admin — standard agent role  (default)
    read-only       memory.read only
    no-delegation   approve + memory.read + memory.write
    no-approval     delegate + memory.read + memory.write
    memory-readonly memory.read only

    Example config.yaml snippet::

        rbac:
          roles:
            - operator
          allowed_actions:
            analyst:
              - memory.read
              - memory.write
    """

    roles: list[str] = field(default_factory=lambda: ["operator"])
    """List of role names granted to this workspace."""

    allowed_actions: dict[str, list[str]] = field(default_factory=dict)
    """Custom role → [action, ...] overrides.  Takes precedence over built-ins."""


@dataclass
class HITLConfig:
    """Human-In-The-Loop settings loaded from the ``hitl:`` block in config.yaml.

    Example config.yaml snippet::

        hitl:
          channels:
            - type: dashboard       # always active
            - type: slack
              webhook_url: https://hooks.slack.com/services/…
            - type: email
              smtp_host: smtp.example.com
              from: alerts@example.com
              to: ops@example.com
          default_timeout: 300      # seconds
          bypass_roles: [admin]
    """
    channels: list[dict] = field(default_factory=lambda: [{"type": "dashboard"}])
    default_timeout: float = 300.0
    bypass_roles: list[str] = field(default_factory=list)


@dataclass
class DelegationConfig:
    retry_attempts: int = 3
    retry_delay: float = 5.0
    timeout: float = 120.0
    escalate: bool = True


@dataclass
class A2AConfig:
    port: int = 8000
    streaming: bool = True
    push_notifications: bool = True


@dataclass
class SandboxConfig:
    backend: str = "subprocess"  # subprocess | docker
    memory_limit: str = "256m"
    timeout: int = 30

@dataclass
class RuntimeConfig:
    """Configuration for CLI-based agent runtimes (claude-code, codex, openclaw, hermes)."""
    command: str = ""          # e.g. "claude" or "codex" (model goes in model field)
    args: list[str] = field(default_factory=list)  # additional CLI args
    required_env: list[str] = field(default_factory=list)  # env vars required to run (e.g. ["CLAUDE_CODE_OAUTH_TOKEN"])
    timeout: int = 0           # seconds (0 = no timeout — agents wait until done)
    model: str = ""            # model override for the CLI
    provider: str = ""         # explicit LLM provider (e.g., "anthropic", "openai",
                               # "minimax"). Falls back to the top-level resolved
                               # provider when empty. Adapters (hermes, claude-code,
                               # codex) prefer this over slug-parsing the model name.
    # Per-model entries surfaced in the canvas Model dropdown. Each entry is a
    # raw dict with at least ``id``; ``required_env`` is the per-model auth
    # list (e.g. ``{"id": "MiniMax-M2.7", "required_env": ["MINIMAX_API_KEY"]}``).
    # Preflight prefers an entry's ``required_env`` over the top-level
    # ``required_env`` when the picked ``model`` matches an entry's ``id``
    # (case-insensitive). The top-level list remains the fallback so single-
    # model templates need not migrate. Surfaced 2026-05-02 after a user
    # picked MiniMax in canvas, set MINIMAX_API_KEY, and still got booted
    # into a CLAUDE_CODE_OAUTH_TOKEN preflight failure.
    models: list[dict] = field(default_factory=list)
    # Deprecated — use required_env + secrets API instead. Kept for backward compat.
    auth_token_env: str = ""
    auth_token_file: str = ""


@dataclass
class GovernanceConfig:
    """Microsoft Agent Governance Toolkit integration settings.

    When ``enabled`` is True, Molecule AI's RBAC and audit trail are bridged
    to the Agent Governance Toolkit (agent-os-kernel) for policy evaluation.

    ``toolkit`` is reserved for future extensibility — only ``"microsoft"``
    is supported today.

    ``policy_mode`` controls enforcement:
      strict      RBAC *and* toolkit policy must both allow — strictest mode
      permissive  RBAC must allow; toolkit denials are logged but not enforced
      audit       RBAC only; toolkit evaluated and logged but never blocks

    ``policy_file`` path to a Rego (.rego), YAML (.yaml/.yml), or Cedar
    (.cedar) policy file, loaded into the PolicyEvaluator at startup.

    ``blocked_patterns`` is a list of regex patterns that the toolkit will
    always deny regardless of roles or policy.
    """

    enabled: bool = False
    toolkit: str = "microsoft"
    policy_endpoint: str = ""
    policy_mode: str = "audit"           # strict | permissive | audit
    policy_file: str = ""
    blocked_patterns: list[str] = field(default_factory=list)
    max_tool_calls_per_task: int = 50


@dataclass
class SecurityScanConfig:
    """Skill dependency security scanning settings.

    ``mode`` controls what happens when critical/high CVEs are found:

    block  — raise ``SkillSecurityError``; the skill is NOT loaded.
    warn   — emit a WARNING + audit event; the skill is loaded anyway (default).
    off    — skip scanning entirely (air-gapped or CI environments).

    Scanners tried in order: Snyk CLI (requires ``SNYK_TOKEN``), then
    pip-audit.  If neither is available the scan is silently skipped.

    Example config.yaml snippet::

        security_scan: warn         # shorthand string form
        # or verbose form:
        security_scan:
          mode: block
    """

    mode: str = "warn"
    """One of: block | warn | off."""

    fail_open_if_no_scanner: bool = True
    """When True (default), silently skip scanning if no scanner (snyk/pip-audit)
    is in PATH.  When False and mode='block', raise SkillSecurityError so that
    operators who require a CVE gate know the gate is absent.  Closes #268."""


@dataclass
class EventLogConfig:
    """Settings for the workspace event log (workspace/event_log.py).

    The event log is an append-and-query buffer for runtime events
    (turn started, tool invoked, peer message delivered, …) that the
    canvas Activity tab and platform-side `/activity` endpoint read.
    Defaults are tuned for a long-running workspace: 1-hour TTL and a
    10k-entry cap together hold ~1 MB of events in memory at the
    documented per-event size budget (~100 bytes payload).

    Example config.yaml snippet::

        observability:
          event_log:
            backend: memory       # or "disabled" to opt out
            ttl_seconds: 3600
            max_entries: 10000
    """

    backend: str = "memory"
    """``memory`` (default) buffers events in process RAM with the
    bounds below; ``disabled`` returns a no-op log so the canvas
    Activity tab is silent. Unknown values fall back to ``memory`` —
    a typo should not crash boot or silently drop telemetry."""

    ttl_seconds: int = 3600
    """How long an event survives before TTL eviction. 1 hour covers
    a long agentic loop comfortably without leaking; operators
    debugging a slow drift may temporarily widen this, but be aware
    the bound is RAM, not disk."""

    max_entries: int = 10_000
    """Hard cap on resident events. Together with ``ttl_seconds`` this
    bounds memory: the FIFO eviction drops oldest first, so a query
    cursor that falls behind sees a contiguous tail rather than a
    gappy log."""


@dataclass
class ObservabilityConfig:
    """Observability settings — heartbeat cadence, log verbosity, event log.

    Hermes-style block: groups platform-runtime knobs that operators
    typically tune together (cadence, verbosity, event-log retention)
    into one declarative section instead of scattering them across env
    vars and hard-coded constants. Adopting this shape unblocks
    per-workspace tuning without a code change.

    The ``event_log`` sub-block is schema-only in this PR (#119 PR-2);
    consumer wiring (the canvas Activity tab + `/activity` endpoint
    reading from the configured backend) lands in PR-3.

    Example config.yaml snippet::

        observability:
          heartbeat_interval_seconds: 60
          log_level: DEBUG
          event_log:
            backend: memory
            ttl_seconds: 3600
            max_entries: 10000
    """

    heartbeat_interval_seconds: int = 30
    """Seconds between heartbeats sent to the platform. Default 30 matches
    ``workspace/heartbeat.py``'s long-standing constant. Lower values
    reduce platform-side detection latency for crashed workspaces; higher
    values reduce platform write load. Bounds: clamped to [5, 300] at
    parse time — outside that range the workspace either floods the
    platform or looks dead before the next beat."""

    log_level: str = "INFO"
    """Python ``logging`` level for the workspace runtime. Accepts the
    standard names (DEBUG, INFO, WARNING, ERROR, CRITICAL). Today the
    runtime reads ``LOG_LEVEL`` env; PR-3 of the #119 stack switches to
    this field with env still honored as an override for ops debugging."""

    event_log: EventLogConfig = field(default_factory=EventLogConfig)
    """Event-log backend + retention bounds. See ``EventLogConfig``."""


@dataclass
class ComplianceConfig:
    """OWASP Top 10 for Agentic Applications compliance settings.

    Default is ``mode: owasp_agentic`` + ``prompt_injection: detect``.
    The detect mode logs injection attempts as audit events without
    blocking the request — so there is no false-positive UX cost, only
    a gain in visibility. Operators opt into stricter ``block`` mode per
    workspace. To disable compliance entirely (not recommended), set
    ``mode: ""`` in config.yaml.

    Before 2026-04-24, the default was ``mode: ""`` (fully off). A
    review of the A2A inbound path showed that no shipped template set
    ``mode`` explicitly, so prompt-injection detection was silently
    disabled for every live workspace despite the machinery existing.
    Flipping the default to ``owasp_agentic`` with ``prompt_injection:
    detect`` closes that gap with zero user-visible behavior change.

    Example config.yaml snippet to opt OUT::

        compliance:
          mode: ""                       # disables all compliance checks

    Example config.yaml snippet to tighten::

        compliance:
          mode: owasp_agentic            # (default)
          prompt_injection: block        # (default: detect)
          max_tool_calls_per_task: 30
          max_task_duration_seconds: 180
    """

    mode: str = "owasp_agentic"
    """Enable compliance mode. ``owasp_agentic`` (default) activates the
    OA-01/OA-02/OA-03/OA-06 checks; ``""`` disables everything."""

    prompt_injection: str = "detect"
    """``detect`` logs injection attempts (default, zero UX cost);
    ``block`` raises PromptInjectionError before the agent sees the
    text. Operators can tighten to ``block`` per workspace."""

    max_tool_calls_per_task: int = 50
    """Maximum number of tool invocations per task before ExcessiveAgencyError."""

    max_task_duration_seconds: int = 300
    """Maximum wall-clock seconds per task before ExcessiveAgencyError."""


@dataclass
class WorkspaceConfig:
    name: str = "Workspace"
    description: str = ""
    role: str = ""
    """Human-readable role label for this agent (e.g. 'Senior Code Reviewer').
    Surfaced in AGENTS.md so peer agents can understand this workspace's purpose
    without reading the full system prompt. Falls back to description when empty."""
    version: str = "1.0.0"
    tier: int = 1
    model: str = "anthropic:claude-opus-4-7"
    provider: str = ""
    """Explicit LLM provider slug (e.g., ``anthropic``, ``openai``, ``minimax``).

    When empty, ``load_config`` derives it from the ``model`` slug prefix
    (``anthropic:claude-opus-4-7`` → ``anthropic``; ``minimax/abab7-chat`` →
    ``minimax``; bare model names → ``""``). Set explicitly via the canvas
    Provider dropdown or the ``LLM_PROVIDER`` env var when the model name
    is provider-ambiguous (e.g., a custom alias) or when an adapter needs
    a specific gateway distinct from the model namespace.
    """
    runtime: str = "claude-code"  # claude-code | codex | openclaw | hermes | custom
    runtime_config: RuntimeConfig = field(default_factory=RuntimeConfig)
    initial_prompt: str = ""
    """Auto-sent as the first A2A message after startup. Default empty = no auto-message.
    Can be an inline string or a file reference (initial_prompt_file in yaml)."""
    idle_prompt: str = ""
    """Auto-sent every `idle_interval_seconds` while the workspace has no active
    task (heartbeat.active_tasks == 0). Default empty = no idle loop. This is
    the reflection-on-completion / backlog-pull pattern from the Hermes/Letta
    playbook: the workspace self-wakes when idle, runs a lightweight reflection
    prompt, and either picks up queued work or stops. Cost scales with useful
    activity (the prompt returns quickly if there's nothing to do). Can be
    inline or a file reference via `idle_prompt_file`."""
    idle_interval_seconds: int = 600
    """How often the idle loop checks in (seconds). Default 600 (10 min).
    Ignored when idle_prompt is empty."""
    skills: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)  # installed plugin names
    tools: list[str] = field(default_factory=list)
    prompt_files: list[str] = field(default_factory=list)
    config_path: str = ""
    """Directory the active config was loaded from. After the /opt fallback
    fires, this is REASSIGNED to the baked template's directory so every
    downstream consumer (prompts, skills, plugins, ExecRead) cascades
    through to the same base. Researcher RC 12052 — without this
    reassignment the concierge boots with the right model but an
    empty system prompt (silently identity-less).
    """
    a2a: A2AConfig = field(default_factory=A2AConfig)
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    rbac: RBACConfig = field(default_factory=RBACConfig)
    hitl: HITLConfig = field(default_factory=HITLConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    security_scan: SecurityScanConfig = field(default_factory=SecurityScanConfig)
    compliance: ComplianceConfig = field(default_factory=ComplianceConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    sub_workspaces: list[dict] = field(default_factory=list)
    effort: str = ""
    """Claude output effort level for the agentic loop: low | medium | high | xhigh | max.
    Empty string = not set (model default applies).  xhigh is the Opus 4.7 recommended
    default for long agentic tasks.  Passed as ``output_config.effort`` by ClaudeSDKExecutor."""
    task_budget: int = 0
    """Advisory total-token budget across the full agentic loop.  0 = not set.
    Must be >= 20000 when non-zero (API minimum).  When set, ClaudeSDKExecutor
    automatically adds the ``task-budgets-2026-03-13`` beta header."""


def _derive_provider_from_model(model: str) -> str:
    """Extract the provider slug prefix from a model identifier.

    Recognizes both ``provider:model`` (Anthropic / OpenAI / Google convention)
    and ``provider/model`` (HuggingFace / Minimax convention). Returns ``""``
    when the model has no recognizable separator — callers must treat empty
    as "use adapter default routing", not as a hard failure.
    """
    for sep in (":", "/"):
        if sep in model:
            return model.partition(sep)[0]
    return ""


def _resolve_provider_from_models(model: str, models: list[dict]) -> str:
    """Look up the intended provider for ``model`` from ``runtime_config.models``.

    The template's ``models`` list is the SSOT for which provider a given
    model id should route through (e.g. ``moonshot/kimi-k2.6`` → ``platform``).
    When the operator did not explicitly pin a provider, using this value
    keeps the runtime's idea of the provider consistent with the adapter's
    registry and with core's prefix-aware ``DeriveProvider`` — preventing
    raw namespace prefixes like ``moonshot`` from being emitted as a
    provider slug and failing the adapter's registry lookup.

    Model ids may use ``:`` (LangChain style) or ``/`` (template style) as
    the namespace separator, so both are normalized for lookup. Returns
    ``""`` when the model is not listed or the listed entry has no provider,
    signalling the caller to fall back to prefix derivation / adapter default.
    """
    if not model or not models:
        return ""
    normalized = model.replace(":", "/").lower()
    for entry in models:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id", "")).replace(":", "/").lower()
        if entry_id and entry_id == normalized:
            provider = str(entry.get("provider", "")).strip()
            if provider:
                return provider
    return ""


_legacy_model_provider_warned = False


def _picked_model_from_env(default: str) -> str:
    """Resolve the operator-picked model id from env; newest name wins.

    Precedence: ``MOLECULE_MODEL`` (canonical, unambiguous) → ``MODEL`` →
    ``MODEL_PROVIDER`` (legacy) → ``default`` (the YAML ``model:`` field).

    ``MODEL_PROVIDER`` is **misleadingly named**: it carries the picked
    *model id*, never the LLM provider — the provider lives in
    ``LLM_PROVIDER`` / the YAML ``provider:`` field. The legacy path stays
    so canvas Save+Restart, the workspace-server secret-mint path, and
    persona env files that set it keep working, but if it's the *only* one
    set we log a deprecation once — the misnomer keeps biting (e.g. setting
    ``MODEL_PROVIDER=claude-code`` expecting it to select the claude-code
    *runtime* — it doesn't, ``runtime:`` does — after which the claude CLI
    404s on ``--model claude-code``). Set ``MODEL``/``MOLECULE_MODEL`` to
    an id from ``runtime_config.models[].id`` (e.g. ``opus``, ``sonnet``,
    ``claude-opus-4-7``, ``MiniMax-M2.7-highspeed``) instead.
    """
    global _legacy_model_provider_warned
    for name in ("MOLECULE_MODEL", "MODEL"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    legacy = (os.environ.get("MODEL_PROVIDER") or "").strip()
    if legacy:
        if not _legacy_model_provider_warned:
            logger.warning(
                "MODEL_PROVIDER=%r is deprecated and misleadingly named — it "
                "sets the picked *model id*, not the LLM provider (that's "
                "LLM_PROVIDER / the YAML `provider:` field). Set MODEL (or "
                "MOLECULE_MODEL) to an id from runtime_config.models instead.",
                legacy,
            )
            _legacy_model_provider_warned = True
        return legacy
    return default


_EVENT_LOG_VALID_BACKENDS = {"memory", "disabled"}


def _parse_event_log(raw: object) -> "EventLogConfig":
    """Coerce the ``observability.event_log`` YAML block into EventLogConfig.

    Lenient like the rest of this parser: a missing block, a non-dict
    value, or a bad backend name resolves to defaults rather than
    raising at boot. The event_log is observability infra — a typo in
    one field should not crash the workspace before any event can fire.
    Bounds (ttl_seconds, max_entries) clamp to positives so a 0/-1
    misconfig doesn't disable the log silently; that's what
    ``backend: disabled`` is for.
    """
    if not isinstance(raw, dict):
        return EventLogConfig()
    backend = str(raw.get("backend", "memory")).strip().lower()
    if backend not in _EVENT_LOG_VALID_BACKENDS:
        backend = "memory"
    try:
        ttl_seconds = int(raw.get("ttl_seconds", 3600))
    except (TypeError, ValueError):
        ttl_seconds = 3600
    if ttl_seconds <= 0:
        ttl_seconds = 3600
    try:
        max_entries = int(raw.get("max_entries", 10_000))
    except (TypeError, ValueError):
        max_entries = 10_000
    if max_entries <= 0:
        max_entries = 10_000
    return EventLogConfig(
        backend=backend, ttl_seconds=ttl_seconds, max_entries=max_entries
    )


def _clamp_heartbeat(value: object) -> int:
    """Coerce raw YAML/env input into the [5, 300]-second heartbeat band.

    Outside that band the workspace either floods the platform with
    sub-second beats or looks dead long before the next one — both
    real failure modes seen on incidents, neither benign. Coerce here
    so adapters and ``heartbeat.py`` can read the value without
    re-validating.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 30
    return max(5, min(300, n))


def load_config(config_path: Optional[str] = None) -> WorkspaceConfig:
    """Load config from WORKSPACE_CONFIG_PATH or the given path."""
    if config_path is None:
        # issue #118: an empty-or-whitespace-only WORKSPACE_CONFIG_PATH must be
        # treated as unset, otherwise the loader looks for config.yaml relative
        # to "" and fails while the fallback configs_dir path is silently ignored.
        config_path = os.environ.get("WORKSPACE_CONFIG_PATH", "").strip() or None
        if config_path is None:
            from molecule_runtime.configs_dir import resolve as resolve_configs_dir

            config_path = str(resolve_configs_dir())

    config_file = Path(config_path) / "config.yaml"
    # opt_fallback_fired is True ONLY when the /opt fallback above (for
    # config.yaml) actually replaced the config_file. It is the gate for
    # the prompt-file defaulting below — a delivered /configs template
    # that has an EMPTY initial_prompt_file field is a config bug, not
    # a /opt fallback case, and we MUST NOT paper over it with a baked
    # file (delivery-wins / fill-absent-only semantics).
    opt_fallback_fired = False
    if not config_file.exists():
        # /opt fallback for the concierge self-host/no-token safety path
        # (core#2919 risk-2: concierge must never boot identity-less).
        #
        # When the asset-fetcher can't deliver a template (self-host with
        # no token, partial template without config.yaml, etc.), /configs is
        # empty and the runtime would MISSING_MODEL fail. If the image bakes
        # the concierge identity at /opt/molecule-platform-agent-template/
        # (the platform-agent image's baked content), fall back to it so a
        # no-fetch concierge still boots with config.yaml + the declared
        # model (moonshot/kimi-k2.6).
        #
        # Per-file / fill-absent-only semantics:
        #   - This is a READ fallback, not a copy. The runtime reads /opt
        #     directly; /configs is unchanged (the in-core
        #     applyConciergeProvisionConfig hook reads /configs via ExecRead
        #     and will see it as empty — that's a separate concern addressed
        #     by the entrypoint per-file copy in the platform-agent image).
        #   - Fires ONLY when /configs/config.yaml is missing. A delivered
        #     /configs/config.yaml always wins (the asset-fetcher's delivery
        #     is authoritative; the /opt fallback is a safety net, not a
        #     primary path).
        #   - If /opt/molecule-platform-agent-template/config.yaml is also
        #     missing (image wasn't baked), the FileNotFoundError fires as
        #     before — fail-closed.
        opt_fallback = Path("/opt/molecule-platform-agent-template/config.yaml")
        if opt_fallback.exists():
            logger.info(
                "load_config: /configs/config.yaml missing; using /opt baked fallback "
                "(%s) — concierge self-host/no-token safety path (core#2919 risk-2)",
                opt_fallback,
            )
            config_file = opt_fallback
            opt_fallback_fired = True
        else:
            raise FileNotFoundError(f"Config file not found: {config_file}")

    # The /opt fallback fires only for config.yaml above; the PROMPT
    # files (initial_prompt / idle_prompt / concierge.md), the SKILLS
    # directory, the PLUGINS directory, the in-core ExecRead hooks, and
    # the system-prompt.md loader all live ALONGSIDE config.yaml in the
    # template. If we kept `config_path` at /configs and only the config
    # file moved, every other resolver would silently miss (the /configs
    # is empty in the no-fetch case) and the concierge would boot
    # identity-less BEHAVIORALLY (right model, empty system prompt,
    # missing skills, missing plugins). Researcher RC 12052 finding.
    # Reassign config_path to the actual loaded config's directory so
    # every downstream consumer (ExecRead, load_skills, build_system_prompt,
    # workspace_plugins_dir, ...) cascades through to the same base.
    config_path = str(config_file.parent)

    with open(config_file) as f:
        raw = yaml.safe_load(f) or {}

    # Operator-picked model from env (canvas / secret-mint / persona env),
    # falling back to the YAML `model:` field. See _picked_model_from_env for
    # the precedence (MOLECULE_MODEL > MODEL > legacy MODEL_PROVIDER).
    model = _picked_model_from_env(raw.get("model", "anthropic:claude-opus-4-7"))

    runtime = raw.get("runtime", "claude-code")
    runtime_raw = raw.get("runtime_config", {})

    # ``runtime_config.models`` is the SSOT for model→provider routing in the
    # template. Resolve it early so an un-pinned provider can be inferred from
    # the picked model's listed provider (e.g. ``moonshot/kimi-k2.6`` →
    # ``platform``) rather than from the raw namespace prefix, which may be a
    # model prefix rather than a registry provider name (template-143).
    models = [m for m in (runtime_raw.get("models") or []) if isinstance(m, dict)]

    # Resolve top-level provider with this priority chain:
    #   1. ``LLM_PROVIDER`` env var (canvas Save+Restart sets this so the
    #      operator's choice survives a CP-driven restart even though the
    #      regenerated /configs/config.yaml drops most user fields).
    #   2. Explicit YAML ``provider:`` (an operator pinned it in the file).
    #   3. Provider listed for the picked model in ``runtime_config.models``.
    #   4. Derive from the model slug prefix for backward compat:
    #        ``anthropic:claude-opus-4-7`` → ``anthropic``
    #        ``minimax/abab7-chat-preview`` → ``minimax``
    #        bare model names → ``""``  (signals "use adapter default")
    # Empty after all four is fine — adapters that don't need an explicit
    # provider keep their existing routing; adapters that do (hermes via
    # derive-provider.sh) prefer this over slug-parsing the model name.
    provider = (
        os.environ.get("LLM_PROVIDER")
        or raw.get("provider")
        or _resolve_provider_from_models(model, models)
        or _derive_provider_from_model(model)
    )

    a2a_raw = raw.get("a2a", {})
    delegation_raw = raw.get("delegation", {})
    sandbox_raw = raw.get("sandbox", {})
    rbac_raw = raw.get("rbac", {})
    hitl_raw = raw.get("hitl", {})
    governance_raw = raw.get("governance", {})
    # security_scan accepts both shorthand string ("warn") and dict ({"mode": "warn"})
    _ss_raw = raw.get("security_scan", {})
    security_scan_raw = _ss_raw if isinstance(_ss_raw, dict) else {"mode": str(_ss_raw)}
    compliance_raw = raw.get("compliance", {})
    observability_raw = raw.get("observability", {})

    # Resolve initial_prompt: inline string or file reference
    initial_prompt = raw.get("initial_prompt", "")
    initial_prompt_file = raw.get("initial_prompt_file", "")
    if not initial_prompt and initial_prompt_file:
        prompt_path = Path(config_path) / initial_prompt_file
        if prompt_path.exists():
            initial_prompt = prompt_path.read_text().strip()
    elif not initial_prompt and not initial_prompt_file and opt_fallback_fired:
        # /opt fallback cascade — the YAML may not declare
        # initial_prompt_file (the asset-fetcher never delivered a
        # template in the no-fetch case), so resolve the prompt from
        # the baked template's conventional locations. Order matters:
        # the concierge template bakes `prompts/concierge.md`; the
        # runtime's general convention is `system-prompt.md`; we fall
        # back through both. Delivery-wins is preserved: this branch
        # only fires when opt_fallback_fired is True (a delivered
        # /configs template with an empty initial_prompt_file is a
        # config bug, NOT a no-fetch case, and we MUST NOT paper over
        # it). Researcher RC 12052 — without this default, the
        # concierge boots with the right model but an EMPTY system
        # prompt = silently identity-less.
        for candidate in ("prompts/concierge.md", "system-prompt.md"):
            prompt_path = Path(config_path) / candidate
            if prompt_path.exists():
                initial_prompt = prompt_path.read_text().strip()
                logger.info(
                    "load_config: /opt fallback fired and YAML has no "
                    "initial_prompt_file; loaded from baked %s",
                    prompt_path,
                )
                break

    # Resolve idle_prompt: same pattern as initial_prompt
    idle_prompt = raw.get("idle_prompt", "")
    idle_prompt_file = raw.get("idle_prompt_file", "")
    if not idle_prompt and idle_prompt_file:
        idle_path = Path(config_path) / idle_prompt_file
        if idle_path.exists():
            idle_prompt = idle_path.read_text().strip()
    elif not idle_prompt and not idle_prompt_file and opt_fallback_fired:
        for candidate in ("prompts/idle.md", "idle-prompt.md", "system-prompt.md"):
            idle_path = Path(config_path) / candidate
            if idle_path.exists():
                idle_prompt = idle_path.read_text().strip()
                logger.info(
                    "load_config: /opt fallback fired and YAML has no "
                    "idle_prompt_file; loaded from baked %s",
                    idle_path,
                )
                break
    idle_interval_seconds = int(raw.get("idle_interval_seconds", 600))

    return WorkspaceConfig(
        name=raw.get("name", "Workspace"),
        config_path=config_path,
        description=raw.get("description", ""),
        role=raw.get("role", ""),
        version=raw.get("version", "1.0.0"),
        tier=int(raw.get("tier", 1)) if str(raw.get("tier", 1)).isdigit() else 1,
        model=model,
        provider=provider,
        runtime=runtime,
        initial_prompt=initial_prompt,
        idle_prompt=idle_prompt,
        idle_interval_seconds=idle_interval_seconds,
        runtime_config=RuntimeConfig(
            command=runtime_raw.get("command", ""),
            args=runtime_raw.get("args", []),
            required_env=runtime_raw.get("required_env", []),
            timeout=runtime_raw.get("timeout", 0),
            # Picked-model precedence (priority order):
            #   1. operator-picked model from env — MOLECULE_MODEL > MODEL >
            #      (legacy) MODEL_PROVIDER, plumbed via canvas Save+Restart,
            #      workspace-server's secret-mint path, or the universal
            #      MODEL/MODEL_PROVIDER env from applyRuntimeModelEnv. The
            #      operator's canvas selection MUST win over the template's
            #      baked-in default; previously the template's
            #      `runtime_config.model: sonnet` always won and the picked
            #      MiniMax/GLM/etc model was silently dropped (Bug B,
            #      surfaced 2026-05-02 during E2E).
            #   2. runtime_raw.model — explicit YAML override in the
            #      template's runtime_config.
            #   3. top-level `model` (already env-resolved above). This is
            #      the SaaS restart case (CP regenerates a minimal
            #      config.yaml on every boot, dropping runtime_config.model).
            # Centralising here means EVERY adapter gets the override for
            # free — no per-adapter env-reading code required.
            model=_picked_model_from_env(runtime_raw.get("model") or model),
            # Same fallback shape as ``model`` above: an explicit
            # ``runtime_config.provider`` wins; otherwise inherit the
            # top-level resolved provider so adapters see a single
            # consistent choice without each one re-implementing
            # env/YAML/slug-prefix resolution.
            provider=runtime_raw.get("provider") or provider,
            # Per-model entries (canvas Model dropdown source). Pass through
            # raw dicts so the schema can grow without a parser change. Only
            # entries that are dicts are kept — a malformed YAML element
            # (string, list, None) is silently dropped rather than raising,
            # matching the rest of this parser's lenient defaults.
            models=models,
            # Deprecated fields — kept for backward compat
            auth_token_env=runtime_raw.get("auth_token_env", ""),
            auth_token_file=runtime_raw.get("auth_token_file", ""),
        ),
        skills=raw.get("skills", []),
        plugins=raw.get("plugins", []),
        tools=raw.get("tools", []),
        prompt_files=raw.get("prompt_files", []),
        a2a=A2AConfig(
            port=a2a_raw.get("port", 8000),
            streaming=a2a_raw.get("streaming", True),
            push_notifications=a2a_raw.get("push_notifications", True),
        ),
        delegation=DelegationConfig(
            retry_attempts=delegation_raw.get("retry_attempts", 3),
            retry_delay=delegation_raw.get("retry_delay", 5.0),
            timeout=delegation_raw.get("timeout", 120.0),
            escalate=delegation_raw.get("escalate", True),
        ),
        sandbox=SandboxConfig(
            backend=sandbox_raw.get("backend", "subprocess"),
            memory_limit=sandbox_raw.get("memory_limit", "256m"),
            timeout=sandbox_raw.get("timeout", 30),
        ),
        rbac=RBACConfig(
            roles=rbac_raw.get("roles", ["operator"]),
            allowed_actions=rbac_raw.get("allowed_actions", {}),
        ),
        hitl=HITLConfig(
            channels=hitl_raw.get("channels", [{"type": "dashboard"}]),
            default_timeout=float(hitl_raw.get("default_timeout", 300)),
            bypass_roles=hitl_raw.get("bypass_roles", []),
        ),
        governance=GovernanceConfig(
            enabled=governance_raw.get("enabled", False),
            toolkit=governance_raw.get("toolkit", "microsoft"),
            policy_endpoint=governance_raw.get("policy_endpoint", ""),
            policy_mode=governance_raw.get("policy_mode", "audit"),
            policy_file=governance_raw.get("policy_file", ""),
            blocked_patterns=governance_raw.get("blocked_patterns", []),
            max_tool_calls_per_task=governance_raw.get("max_tool_calls_per_task", 50),
        ),
        security_scan=SecurityScanConfig(
            mode=security_scan_raw.get("mode", "warn"),
            fail_open_if_no_scanner=security_scan_raw.get("fail_open_if_no_scanner", True),
        ),
        compliance=ComplianceConfig(
            # Default must match ComplianceConfig.mode's dataclass default
            # (see class docstring for rationale — 2026-04-24 flip).
            mode=compliance_raw.get("mode", "owasp_agentic"),
            prompt_injection=compliance_raw.get("prompt_injection", "detect"),
            max_tool_calls_per_task=int(compliance_raw.get("max_tool_calls_per_task", 50)),
            max_task_duration_seconds=int(compliance_raw.get("max_task_duration_seconds", 300)),
        ),
        observability=ObservabilityConfig(
            heartbeat_interval_seconds=_clamp_heartbeat(
                observability_raw.get("heartbeat_interval_seconds", 30)
            ),
            log_level=str(observability_raw.get("log_level", "INFO")).upper(),
            event_log=_parse_event_log(observability_raw.get("event_log", {})),
        ),
        sub_workspaces=raw.get("sub_workspaces", []),
        effort=str(raw.get("effort", "")),
        task_budget=int(raw.get("task_budget", 0)),
    )
