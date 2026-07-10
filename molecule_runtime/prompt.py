"""Build the system prompt for the workspace agent."""

import logging
import os
from pathlib import Path

import molecule_runtime.mailbox_dir as mailbox_dir
from molecule_runtime.executor_helpers import (
    get_a2a_instructions,
    get_capabilities_preamble,
    get_hma_instructions,
)
from molecule_runtime.skill_loader.loader import LoadedSkill
from molecule_runtime.shared_runtime import build_peer_section

logger = logging.getLogger(__name__)

# Durable memory-snapshot files auto-loaded into the system prompt every
# session if present in config_path (loaded only when they exist, and skipped
# when already listed in prompt_files to avoid duplication). MEMORY.md/USER.md
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


async def get_peer_capabilities(platform_url: str, workspace_id: str) -> list[dict]:
    """Fetch peer workspace capabilities from the platform."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{platform_url}/registry/{workspace_id}/peers",
                headers={"X-Workspace-ID": workspace_id},
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

        token = os.environ.get("MOLECULE_WORKSPACE_TOKEN", "")
        headers = {"X-Workspace-ID": workspace_id}
        if token:
            headers["Authorization"] = f"Bearer {token}"

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
    parts.append(BASE_PLATFORM_PROMPT)

    # Orchestrator-only guardrail — platform/concierge agents ONLY. Injected
    # high (right after the base identity, ahead of platform instructions and
    # the possibly-stale template prompt files) and worded to override any
    # later "do the work yourself" text, so a concierge on a stale template is
    # still gagged from self-executing. Normal worker workspaces pass
    # platform_guardrail=False and keep doing real work — never gag them.
    if platform_guardrail:
        parts.append(ORCHESTRATOR_ONLY_GUARDRAIL)

    # Platform instructions (global → team → workspace scope) go next so
    # they take highest precedence among the operational instructions.
    if platform_instructions:
        parts.append("# Platform Instructions\n")
        parts.append(platform_instructions)

    # Platform Capabilities preamble (#2332): tight inventory of every
    # native tool agents have access to, generated from the registry.
    # Goes BEFORE prompt files so the role-specific docs read against
    # a known toolkit, not a discovery problem. Detailed when_to_use
    # docs still appear later in the A2A and HMA sections — this
    # preamble is the elevator pitch ("you have these"); the later
    # sections are the manual ("here's when and how").
    capabilities = get_capabilities_preamble(mcp=a2a_mcp)
    if capabilities:
        parts.append(capabilities)

    # Load prompt files in order
    files_to_load = list(prompt_files or [])
    if not files_to_load:
        # Backwards compatible: fall back to system-prompt.md
        files_to_load = ["system-prompt.md"]

    seen_files = set(files_to_load)

    # Durable memory-snapshot READ source. Kernel ON -> the mailbox memory dir
    # every writer (agents_md, append-to-memory hook, consolidation) now targets;
    # kernel OFF -> config_path, byte-identical to the pre-migration behavior.
    # Computed ONCE and reused for BOTH the prompt_files loop below and the
    # auto-load loop, so the SSOT rule ("fresh mailbox memory wins over a stale
    # /configs copy") holds no matter WHERE a memory-snapshot file is referenced.
    memory_source = mailbox_dir.memory_dir() if mailbox_dir.kernel_enabled() else Path(config_path)

    for filename in files_to_load:
        # MUST-FIX (RC #203, SSOT): a memory-snapshot file NAMED in prompt_files
        # must resolve to its DURABLE mailbox copy when the kernel is on and that
        # copy exists — otherwise the param-rendered /configs copy loaded here
        # (and added to seen_files) SHADOWS fresh mailbox memory, contradicting
        # the memory-write-path SSOT. Only memory-snapshot NAMES are redirected,
        # and only when a mailbox copy is present; every other prompt file keeps
        # its /configs source. Kernel OFF => memory_source IS config_path, so
        # this is byte-identical.
        file_path = Path(config_path) / filename
        if filename in DEFAULT_MEMORY_SNAPSHOT_FILES:
            mailbox_copy = memory_source / filename
            if mailbox_copy.exists():
                file_path = mailbox_copy
        if file_path.exists():
            content = file_path.read_text().strip()
            if content:
                parts.append(content)
        else:
            print(f"Warning: prompt file not found: {file_path}")

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
    # files loaded above; a memory-snapshot filename listed in prompt_files is
    # redirected to its fresh mailbox copy IN the loop above (RC #203), so the
    # SSOT holds whether or not the file is named in prompt_files.
    # Kernel OFF => read from config_path exactly as before (byte-identical).
    # ``memory_source`` was resolved above (shared with the prompt_files loop).
    for filename in DEFAULT_MEMORY_SNAPSHOT_FILES:
        if filename in seen_files:
            continue
        file_path = memory_source / filename
        if file_path.exists():
            content = file_path.read_text().strip()
            if content:
                parts.append(content)

    # Inject plugin rules (always-on guidelines from ECC, Superpowers, etc.)
    if plugin_rules:
        parts.append("\n## Platform Rules\n")
        for rule in plugin_rules:
            parts.append(rule)
            parts.append("")

    # Inject plugin prompt fragments
    if plugin_prompts:
        parts.append("\n## Platform Guidelines\n")
        for fragment in plugin_prompts:
            parts.append(fragment)
            parts.append("")

    # Add skill instructions
    if loaded_skills:
        parts.append("\n## Your Skills\n")
        for skill in loaded_skills:
            parts.append(f"### {skill.metadata.name}")
            if skill.metadata.description:
                parts.append(skill.metadata.description)
            parts.append(skill.instructions)
            parts.append("")

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
    parts.append(get_a2a_instructions(mcp=a2a_mcp))
    parts.append(get_hma_instructions())

    # Add peer capabilities with a single shared renderer.
    peer_section = build_peer_section(peers)
    if peer_section:
        parts.append(peer_section)

    # Add delegation failure handling
    parts.append("""
## Handling delegation failures
If a delegation fails:
1. Check if the task is blocking — if not, continue other work
2. Retry transient failures (connection errors) after 30 seconds
3. For persistent failures, report to the caller with context
4. Never silently drop a failed task
""")

    return "\n".join(parts)
