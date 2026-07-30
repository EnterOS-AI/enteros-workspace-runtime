"""Build the system prompt for the workspace agent."""

import hashlib
import logging
import os
from pathlib import Path

import molecule_runtime.mailbox_dir as mailbox_dir
from molecule_runtime.executor_helpers import (
    get_a2a_instructions,
    get_capabilities_preamble,
    get_display_instructions,
    get_hma_instructions,
)
from molecule_runtime.skill_loader.loader import LoadedSkill
from molecule_runtime.shared_runtime import build_peer_section
from molecule_runtime.platform_auth import platform_headers

logger = logging.getLogger(__name__)

# Durable memory-snapshot files auto-loaded into the system prompt every
# session if present (loaded only when they exist, and skipped when the SAME
# resolved FILE was already loaded by the prompt_files loop, to avoid
# duplication — a basename declared in prompt_files whose durable mailbox copy
# is a DIFFERENT file is still auto-loaded here). MEMORY.md/USER.md
# are the platform-agnostic canonical store the persistence discipline writes
# to; the rest are each framework's NATIVE durable-context convention so an
# agent that writes its framework's file (claude-code → CLAUDE.md; codex /
# many tools → AGENTS.md; openclaw → SOUL.md) also has
# it injected. Loading-if-present makes this safe across all runtimes without
# threading a per-runtime param through every caller — an agent only ever has
# the file(s) its framework uses. This is the "memory survives a context reset"
# leg: these files live on the persistent volume, so a fresh/auto-healed
# session re-injects them via the system prompt.
DEFAULT_MEMORY_SNAPSHOT_FILES = (
    "MEMORY.md",
    "USER.md",
    "CLAUDE.md",
    "AGENTS.md",
    "SOUL.md",
)


def _evolved_memory_residue(
    mem_path: Path,
    role_text: str,
    filename: str,
    seeds: dict[str, dict] | None = None,
) -> str:
    """What is in the durable mailbox copy that is NOT a snapshot of the role file.

    A memory basename DECLARED in ``prompt_files`` occupies TWO slots that the
    kernel migration accidentally welded together:

    * the ROLE slot — ``/configs/<name>``, param-rendered fresh on every
      provision, authoritative, must always be what the model reads as its role;
    * the MEMORY slot — ``<mailbox>/memory/<name>``, durable, but SEEDED as a
      byte-copy of the role file on the first kernel-on boot
      (``mailbox_dir._legacy_pairs``: ``(legacy/<name>) -> (base/memory/<name>)``).

    Because of that seeding the two paths normally hold the SAME BYTES, so a
    path-keyed dedup cannot see the duplication and injects the persona twice —
    and once ``/configs`` is re-rendered to v2, the frozen v1 snapshot trails
    the live persona forever. Both are settled by asking one question of the
    mailbox copy: *which of your bytes did a WRITER put there?* Only those are
    memory; the rest is a stale photocopy of the role file and is dropped.

    The answer, in decreasing order of evidence strength:

    1. identical to the CURRENT role file -> pure snapshot, nothing to keep;
    2. current role file + a tail -> the tail is what a writer appended;
    3. a recorded first-boot seed + a tail (``mailbox_dir.seed_manifest()``)
       -> the tail is what a writer appended on top of an OLDER role version.
       An exact seed match is this case with an empty tail: nothing to keep;
    4. diverged from a recorded seed with no shared prefix -> a writer rewrote
       the whole file (``agents_md`` force-writes ``AGENTS.md`` every boot);
       keep all of it;
    5. no provenance recorded at all (workspace seeded before this shipped) ->
       fall back to the writer inventory: keep the file when some writer can
       target that basename, drop it when none can
       (``mailbox_dir.ACCUMULATING_MEMORY_BASENAMES``).

    Never raises and never guesses in the losing direction: every ambiguous
    case keeps the content.
    """
    try:
        raw = mem_path.read_bytes()
        mem = raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    role = role_text.strip()
    if not mem:
        return ""
    if mem == role:
        return ""  # (1)
    if role and mem.startswith(role):
        return mem[len(role) :].strip()  # (2)
    seed = (mailbox_dir.seed_manifest() if seeds is None else seeds).get(filename)
    if isinstance(seed, dict):
        size, digest = seed.get("size"), seed.get("sha256")
        if isinstance(size, int) and isinstance(digest, str) and 0 <= size <= len(raw):
            if hashlib.sha256(raw[:size]).hexdigest() == digest:
                return raw[size:].decode("utf-8", "replace").strip()  # (3)
        return mem  # (4)
    return mem if filename in mailbox_dir.ACCUMULATING_MEMORY_BASENAMES else ""  # (5)


async def get_peer_capabilities(platform_url: str, workspace_id: str) -> list[dict]:
    """Fetch peer workspace capabilities from the platform."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{platform_url}/registry/{workspace_id}/peers",
                headers=platform_headers(workspace_id, source=True),
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"Warning: could not fetch peers: {e}")
    return []


async def get_platform_instructions(platform_url: str, workspace_id: str) -> str:
    """Fetch resolved platform instructions (global + workspace scope).

    Endpoint is gated by WorkspaceAuth — the workspace token (read from env)
    is sent as a bearer header. Fails open (returns "") on any error so a
    platform outage doesn't block agent startup. Short timeout (3s) because
    this runs in the boot hot path.
    """
    try:
        import httpx

        # platform_headers resolves the same MOLECULE_WORKSPACE_TOKEN through
        # platform_auth.get_token(), and adds the tenant-routing header this
        # endpoint is rejected without on a SaaS tenant (#373).
        headers = platform_headers(workspace_id, source=True)

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{platform_url}/workspaces/{workspace_id}/instructions/resolve",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("instructions", "")
    except Exception as e:
        logger.warning("could not fetch platform instructions: %s", e)
    return ""


# Base platform identity — prepended to EVERY workspace's system prompt,
# regardless of runtime or template. Every agent on the platform shares this
# foundational frame; the template's prompt_files layer the workspace-specific
# role on top. Single-sourced here in the base builder (not per-runtime, not
# per-template), so all agents present a consistent platform identity.
BASE_PLATFORM_PROMPT = """\
# You are a workspace on the Molecule AI platform

You are an AI agent running as a *workspace* inside an organization on the
Molecule AI platform — a multi-agent system where agents collaborate as peers,
delegate work to one another over A2A, extend themselves with plugins and skills,
and operate under shared platform governance and memory. Your specific role,
name, and instructions are defined in the sections that follow; this frame is the
platform you operate within, shared by every agent on it."""


# Orchestrator-only guardrail — injected ONLY for platform/concierge agents
# (kind='platform'), gated at the call site by mcp_server_present(). A normal
# worker workspace MUST keep doing real work, so it never receives this block.
#
# This is the runtime half of a two-layer durable fix (the other half is the
# platform-agent template's system-prompt.md persona). Injecting it here means
# the guardrail holds even when a concierge boots a STALE template that lacks
# it. It is worded to OVERRIDE any role text that suggests the concierge should
# do work itself.
#
# Root fix for two incidents:
#   1. the concierge self-adopting a never-ending PR-review mission that
#      duplicates the dedicated review agent (e.g. codex-reviewer);
#   2. the earlier autonomous self-wake loop.
ORCHESTRATOR_ONLY_GUARDRAIL = """\
## Orchestrator-only — you NEVER do the work yourself (hard platform rule)

You are the platform/concierge **orchestrator** for this team. This rule is
authoritative and OVERRIDES anything below that suggests you should do work
yourself.

You **do not do substantive work yourself** — no coding, no PR reviews, no
research, no analysis, no writing deliverables, no long-running or recurring
jobs. You **respond, route, delegate, and report — nothing else.**

For **any** task, do exactly one of two things:
1. **DELEGATE** it to an existing agent/workspace whose role fits — use your
   delegation tools (list_peers to find the right agent, then delegate_task /
   delegate_task_async). The doer is always someone else.
2. If **NO** suitable workspace exists, **ASK THE USER to create one** (or to
   confirm you should create it via the platform MCP). Do not improvise, do not
   self-assign, and do not quietly start the work because no team exists yet.

Hard limits:
- **Never adopt an open-ended or standing mission with no explicit
  done-condition.** Every task you accept has a clear owner that is NOT you and
  a clear finish line. Route recurring/never-ending work to a dedicated agent or
  a scheduled workspace — never run the loop yourself.
- **PR review is NOT yours.** Pull-request review is owned by the dedicated
  review agent (e.g. the team's codex-reviewer). Never run a review pass
  yourself and never appoint yourself to a standing "watch and review PRs"
  mission — delegate every review, or ask the user to stand one up if none
  exists.
- **Don't self-wake into work.** An idle moment, a delegation result, or a
  background notification is not a license to pick up substantive work on your
  own. Acknowledge, route/delegate if there's a real owned task, then go quiet.

When in doubt: delegate, or ask the user who should own it. You are the front
door and the dispatcher — never the worker."""


def build_system_prompt(
    config_path: str,
    workspace_id: str,
    loaded_skills: list[LoadedSkill],
    peers: list[dict],
    prompt_files: list[str] | None = None,
    plugin_rules: list[str] | None = None,
    plugin_prompts: list[str] | None = None,
    platform_instructions: str = "",
    a2a_mcp: bool = True,
    platform_guardrail: bool = False,
) -> str:
    """Build the complete system prompt.

    Loads prompt files in order from config_path. If prompt_files is specified
    in config.yaml, those files are loaded in order. Otherwise falls back to
    system-prompt.md for backwards compatibility.
    If MEMORY.md or USER.md exist alongside the config, they are appended as a
    frozen memory snapshot without needing to list them explicitly.

    This allows different agent frameworks to use their own file structures:
    - OpenClaw: SOUL.md, BOOTSTRAP.md, AGENTS.md, HEARTBEAT.md, TOOLS.md, USER.md
    - Claude Code: CLAUDE.md
    - Default: system-prompt.md
    """
    parts = []

    # Base platform identity — ALWAYS first, for EVERY workspace regardless of
    # runtime or template. The shared "you are a Molecule AI platform workspace"
    # frame; the prompt_files below layer the specific role on top of it, never
    # replace it. Single-sourced as BASE_PLATFORM_PROMPT.
    # ── Single-derivation prompt assembly (SSOT) ─────────────────────────────
    # Each labeled segment is appended to ``parts`` (the flattened system prompt
    # the LLM receives) AND recorded as a Langfuse trace component in ONE place,
    # via ``_seg``. This makes the decomposed ``generation.input`` view provably
    # COMPLETE and drift-proof: it can never omit — or diverge from — content
    # that is in the real prompt. (Previously a SECOND, deliberately
    # "self-contained" re-derivation at the end of this function re-read the
    # sources independently and silently dropped the capabilities preamble, the
    # MEMORY.md/USER.md snapshots, the delegation-failures block, and full skill
    # instructions, so the traced prompt misrepresented what the model saw.)
    _components: list[dict] = []

    def _seg(_label, *_texts):
        # Extend ``parts`` with the EXACT same strings the old inline appends
        # produced (so the flattened prompt stays byte-identical) and record
        # them as one labeled component. Fail-open — recording must never break
        # prompt construction.
        parts.extend(_texts)
        try:
            _joined = "\n".join(_texts).strip()
            if _joined:
                _components.append({"label": _label, "text": _joined})
        except Exception:
            pass

    _seg("base_platform_identity", BASE_PLATFORM_PROMPT)

    # Orchestrator-only guardrail — platform/concierge agents ONLY. Injected
    # high (right after the base identity, ahead of platform instructions and
    # the possibly-stale template prompt files) and worded to override any
    # later "do the work yourself" text, so a concierge on a stale template is
    # still gagged from self-executing. Normal worker workspaces pass
    # platform_guardrail=False and keep doing real work — never gag them.
    if platform_guardrail:
        _seg("orchestrator_guardrail", ORCHESTRATOR_ONLY_GUARDRAIL)

    # Platform instructions (global → team → workspace scope) go next so
    # they take highest precedence among the operational instructions.
    if platform_instructions:
        _seg("platform_instructions", "# Platform Instructions\n", platform_instructions)

    # Platform Capabilities preamble (#2332): tight inventory of every
    # native tool agents have access to, generated from the registry.
    # Goes BEFORE prompt files so the role-specific docs read against
    # a known toolkit, not a discovery problem. Detailed when_to_use
    # docs still appear later in the A2A and HMA sections — this
    # preamble is the elevator pitch ("you have these"); the later
    # sections are the manual ("here's when and how").
    capabilities = get_capabilities_preamble(mcp=a2a_mcp)
    if capabilities:
        _seg("platform_capabilities", capabilities)

    # Load prompt files in order
    files_to_load = list(prompt_files or [])
    if not files_to_load:
        # Backwards compatible: fall back to system-prompt.md
        files_to_load = ["system-prompt.md"]

    # Resolved paths already injected by the prompt_files loop. The auto-load
    # memory leg below dedups against THIS (not against the declared NAMES) so
    # that a declared memory basename and its durable mailbox copy — two
    # documents that merely share a basename — can both be considered, while
    # the SAME file is never injected twice (kernel OFF, where memory_source IS
    # config_path, stays byte-identical).
    _loaded_sources: set[str] = set()

    # basename -> the ROLE text just injected from /configs for a DECLARED
    # memory basename. The auto-load leg subtracts that text from the durable
    # mailbox copy (``_evolved_memory_residue``) instead of injecting the copy
    # whole, so the first-boot SNAPSHOT of the role file is never injected a
    # second time and never trails the freshly re-rendered role. Only populated
    # when the role text really came from /configs — never from the mailbox
    # fallback, where there is nothing to subtract.
    _declared_role_text: dict[str, str] = {}

    def _key(p: Path) -> str:
        try:
            return str(p.resolve())
        except OSError:
            return os.path.normpath(str(p))

    # Durable memory-snapshot READ source. Kernel ON -> the mailbox memory dir
    # every writer (agents_md, append-to-memory hook, consolidation) now targets;
    # kernel OFF -> config_path, byte-identical to the pre-migration behavior.
    # Computed ONCE and reused for BOTH the prompt_files loop below and the
    # auto-load loop, so the SSOT rule ("fresh mailbox memory wins over a stale
    # /configs copy") holds no matter WHERE a memory-snapshot file is referenced.
    memory_source = mailbox_dir.memory_dir() if mailbox_dir.kernel_enabled() else Path(config_path)

    # Trace each loaded prompt file under its TRUE category: a memory-snapshot
    # file (MEMORY.md/USER.md) NAMED in prompt_files is durable memory and must
    # be labeled ``memory_snapshots``, not ``role_prompt_files`` — otherwise an
    # operator auditing injected memory in /traces would misattribute it to the
    # role prompt. We flush contiguous same-category RUNS (not per-file), so the
    # common case (all role files, or role files then memory files) stays a
    # single component per category, while any interleaving is still labeled
    # correctly. Every content string is still appended to ``parts`` in file
    # order, so the flattened prompt is byte-identical.
    _run_parts: list = []
    _run_label = None

    def _flush_run():
        nonlocal _run_parts, _run_label
        if _run_parts:
            _seg(_run_label, *_run_parts)
            _run_parts = []
            _run_label = None

    for filename in files_to_load:
        # A memory-basename NAMED in prompt_files is a DECLARED ROLE FILE, not
        # durable memory: ``/configs`` is provisioner-authored and re-rendered
        # from the template on EVERY provision, so it MUST stay authoritative
        # for that slot.
        #
        # This is a deliberate PARTIAL revert of RC #203, which redirected a
        # declared memory-basename to its mailbox copy whenever that copy
        # existed. RC #203 assumed a memory basename in prompt_files is always
        # durable memory. That is false: the shipped openclaw template declares
        # SOUL.md / AGENTS.md / USER.md in prompt_files as its ROLE files. The
        # mailbox copy under /workspace/.molecule/memory is written
        # skip-if-exists (mailbox_dir._copy_0600) and the reconcile arm
        # deliberately skips the memory dir, so once /workspace is DURABLE on
        # every backend (cp#672 / molecule-controlplane #2777) that redirect
        # PINS the persona to its first-boot content forever — no re-provision,
        # template change, or param re-render could ever land again.
        #
        # RC #203's actual goal (fresh mailbox memory must never be SHADOWED by
        # a stale /configs copy) is preserved in full: the auto-load leg below
        # still injects the durable copy — but only the part of it a WRITER
        # produced (``_evolved_memory_residue``), never the first-boot snapshot
        # of the role file that the migrator seeded it with.
        #
        # The mailbox copy remains the FALLBACK when /configs has no copy at
        # all, so a declared section is never silently dropped.
        # Kernel OFF => memory_source IS config_path, so this is byte-identical.
        is_mem = filename in DEFAULT_MEMORY_SNAPSHOT_FILES
        configs_path = Path(config_path) / filename
        file_path = configs_path
        if is_mem and not file_path.exists():
            mailbox_copy = memory_source / filename
            if mailbox_copy.exists():
                file_path = mailbox_copy
        if file_path.exists():
            content = file_path.read_text().strip()
            if content:
                # A declared basename served from /configs is a provisioner-
                # authored ROLE file, so label it as one (N-R1): only the
                # mailbox-FALLBACK case is genuinely durable memory occupying
                # the role slot, and only that case may be traced as memory.
                from_mailbox = file_path != configs_path
                label = "memory_snapshots" if (is_mem and from_mailbox) else "role_prompt_files"
                if _run_parts and label != _run_label:
                    _flush_run()
                _run_label = label
                _run_parts.append(content)
                _loaded_sources.add(_key(file_path))
                if is_mem and not from_mailbox:
                    _declared_role_text[filename] = content
        else:
            print(f"Warning: prompt file not found: {file_path}")
    _flush_run()

    # Hermes-style memory snapshot files: load automatically when present.
    # These stay as thin markdown files so the runtime does not need a new
    # storage layer.
    #
    # MUST-FIX (memory WRITE-path reconciliation): with the mailbox kernel ON,
    # memory snapshots are READ from the durable mailbox memory dir
    # (/workspace/.molecule/memory) — the SAME directory every writer
    # (agents_md, append-to-memory hook, consolidation) now writes to. Because
    # a param-rendered /configs copy is NEVER read here in kernel mode, a STALE
    # /configs/MEMORY.md can never SHADOW a fresh mailbox copy. The /configs
    # dir stays authoritative only for the param-rendered NON-memory system-prompt
    # files loaded above.
    #
    # Two dedup rules, and they are different questions:
    #
    #  * SAME FILE — resolved-path identity (kernel OFF, or kernel ON with no
    #    mailbox copy so the role loop already fell back to /configs). Skip;
    #    nothing double-loads. Kernel OFF stays byte-identical.
    #  * SAME BYTES — a basename DECLARED in prompt_files loaded its /configs
    #    role copy above, and its mailbox copy is a different PATH holding a
    #    first-boot SNAPSHOT of those same bytes. Path identity cannot see
    #    that, so subtract the role text and inject only the writer-produced
    #    residue (``_evolved_memory_residue``). That keeps RC #203's anti-shadow
    #    guarantee — everything a writer put in the durable copy still reaches
    #    the prompt, layered after the role — without injecting the persona
    #    twice or letting a frozen v1 trail the re-rendered v2.
    #
    # ``memory_source`` was resolved above (shared with the prompt_files loop).
    _mem_parts = []
    # Read the seed provenance ONCE per build: the prompt is re-derived on
    # every turn's hot-reload (claude_sdk_executor), not just at boot.
    _seeds = mailbox_dir.seed_manifest() if _declared_role_text else {}
    for filename in DEFAULT_MEMORY_SNAPSHOT_FILES:
        file_path = memory_source / filename
        if not file_path.exists() and mailbox_dir.kernel_enabled():
            # Mirror the prompt_files redirect rule in the other direction:
            # the mailbox copy wins WHEN PRESENT, but when it is absent (first
            # boot before migration, or an unwritable mailbox volume where the
            # migrator could not run — core#4295) fall back to the legacy
            # /configs copy rather than silently dropping accumulated memory
            # from the prompt. A stale /configs copy still can never SHADOW a
            # fresh mailbox copy — this branch only fires when there is no
            # mailbox copy at all.
            legacy_copy = Path(config_path) / filename
            if legacy_copy.exists():
                file_path = legacy_copy
        if _key(file_path) in _loaded_sources:
            continue  # same FILE already injected by the prompt_files loop
        if file_path.exists():
            role_text = _declared_role_text.get(filename)
            if role_text is None:
                content = file_path.read_text().strip()
            else:
                # Same BYTES risk: subtract the role snapshot, keep the rest.
                content = _evolved_memory_residue(file_path, role_text, filename, _seeds)
            if content:
                _mem_parts.append(content)
    # Durable memory snapshots (MEMORY.md/USER.md) are part of what the model
    # sees — trace them so Langfuse does not misleadingly omit accumulated memory.
    if _mem_parts:
        _seg("memory_snapshots", *_mem_parts)

    # Inject plugin rules (always-on guidelines from ECC, Superpowers, etc.)
    if plugin_rules:
        _rule_parts = ["\n## Platform Rules\n"]
        for rule in plugin_rules:
            _rule_parts.append(rule)
            _rule_parts.append("")
        _seg("plugin_rules", *_rule_parts)

    # Inject plugin prompt fragments
    if plugin_prompts:
        _guideline_parts = ["\n## Platform Guidelines\n"]
        for fragment in plugin_prompts:
            _guideline_parts.append(fragment)
            _guideline_parts.append("")
        _seg("plugin_guidelines", *_guideline_parts)

    # Add skill instructions — trace the FULL section (names + descriptions +
    # instructions), the same text the model receives, not a names-only summary.
    if loaded_skills:
        _skill_parts = ["\n## Your Skills\n"]
        for skill in loaded_skills:
            _skill_parts.append(f"### {skill.metadata.name}")
            if skill.metadata.description:
                _skill_parts.append(skill.metadata.description)
            _skill_parts.append(skill.instructions)
            _skill_parts.append("")
        _seg("skills", *_skill_parts)

    # Platform tool instructions: A2A (inter-agent communication) and HMA
    # (persistent memory). These document how to call delegate_task,
    # commit_memory, etc — without them, agents see the tools registered
    # but have no instructions on when/how to use them. Placed between
    # Skills and Peers so the A2A docs precede the peer list (which is
    # the data shape the A2A tools operate over).
    #
    # a2a_mcp=True: MCP tool variant (claude-code, hermes, openclaw,
    # codex). a2a_mcp=False: CLI subprocess variant (custom
    # runtimes that don't speak MCP). Default True matches the
    # MCP-capable majority; CLI-only adapters override at the call site.
    _seg("a2a_instructions", get_a2a_instructions(mcp=a2a_mcp))
    _seg("hma_instructions", get_hma_instructions())

    # Desktop display control (computer-use). Default-on: every workspace can spin
    # a per-workspace desktop sidecar up on demand through the gateway, so any
    # action-capable agent is told it has a screen it can drive — no per-workspace
    # display opt-in. Gated on the display.control RBAC action so genuinely
    # read-only agents (which lack it) are not told about tools they cannot use;
    # the tools' own desktop_status probe reports live availability at call time.
    # (Previously get_display_instructions was defined but never wired into the
    # prod prompt, so agents reported themselves as "a server-side agent without a
    # display" even though the sidecar/gateway were available.)
    try:
        from molecule_runtime.builtin_tools.audit import (
            check_permission,
            get_workspace_roles,
        )

        _roles, _custom = get_workspace_roles()
        if check_permission("display.control", _roles, _custom):
            _seg("display_instructions", get_display_instructions())
    except Exception:  # never let prompt assembly fail on the optional section
        logger.debug("display-control prompt section skipped", exc_info=True)

    # Add peer capabilities with a single shared renderer.
    peer_section = build_peer_section(peers)
    if peer_section:
        _seg("peers", peer_section)

    # Add delegation failure handling
    _seg("delegation_failures", """
## Handling delegation failures
If a delegation fails:
1. Check if the task is blocking — if not, continue other work
2. Retry transient failures (connection errors) after 30 seconds
3. For persistent failures, report to the caller with context
4. Never silently drop a failed task
""")

    prompt = "\n".join(parts)

    # SSOT trace producer: record the consolidated prompt + its COMPLETE labeled
    # decomposition (``_components``, built inline above from the SAME segments
    # appended to ``parts`` — a single source of truth, so the traced view can
    # never drift from or drop content in the real prompt). Fail-open.
    try:
        from molecule_runtime import tracing as _tracing

        _tracing.record_system_prompt(workspace_id, prompt, _components)
    except Exception:
        pass
    return prompt
