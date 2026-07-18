"""The identity-capabilities digest provider — the pinned second-person header.

Renders the operator-ruled header (design ruling 2026-07-07): "You are …",
"Your responsibility: …", the priorities reminder, and "Your available MCP
tools (native):" grouped by origin. It is the one provider allowed the reserved
``pinned`` band, and it never counts toward digest emptiness.

Sourcing (one source, two renderings — never authored twice):
  * identity  — the canonical persona chain via
    :func:`persona_render.read_canonical_persona` (the same ``prompt_files`` →
    ``system-prompt.md`` sourcing ``build_system_prompt`` uses), so the header
    can never drift from the rendered system prompt;
  * tools     — :func:`platform_agent_identity.loaded_mcp_tools`, the live
    ``mcp__<server>__<tool>`` inventory, partitioned by the ``<server>``
    segment: ``molecule-platform`` = platform MCP, ``molecule`` = workspace
    MCP, anything else = plugin-contributed;
  * gating    — the platform-MCP group renders ONLY on the platform agent
    (:func:`loaded_mcp_tools_probe._is_platform_agent`), derived from the
    actually-loaded management-MCP inventory, not a declared flag.

The seams are injected (defaulting to the real runtime functions) so the
provider is unit-testable without a live workspace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..contract import (
    PINNED_PROVIDER_ID,
    AgeBand,
    Band,
    Contribution,
    Urgency,
)

# The MCP server-name segments that partition the native tool inventory.
PLATFORM_MCP_SERVER = "molecule-platform"  # == platform_agent_identity.MANAGEMENT_MCP_NAME
WORKSPACE_MCP_SERVER = "molecule"  # the workspace's own in-container MCP self-label

# Priorities line — mirrors the settled provider tier order (contract
# ``providers`` roster). Post-vendoring this is generated from the vendored
# roster; until then it is the SSOT mirror kept in lockstep with the contract.
PRIORITIES_LINE = (
    "Priorities: user first; then tasks · sent · inbox · results · schedules · "
    "plugin work · goal — urgent items jump the queue."
)

# Reply routing — the delivery contract for this self-initiated turn. The
# digest poster forwards the turn's reply text to the user's chat
# (idle_digest/reply_forwarder.py), restoring symmetry with request-response
# turns; without this line models "answer" housekeeping ticks into the void
# or, worse, narrate to nobody. The ``(idle)`` sentinel is the silence valve —
# keep it in lockstep with reply_forwarder.IDLE_SENTINEL.
REPLY_ROUTING_LINE = (
    "Reply routing: whatever you write as this turn's reply is delivered to "
    "the user's chat — write it TO the user, and don't also call "
    "send_message_to_user for the same content. Reply exactly (idle) to send "
    "nothing this turn."
)

# Names-only cap per tool group before the engine's byte cap also applies.
_MAX_NAMES_PER_GROUP = 8
_IDENTITY_LINE_MAX_CHARS = 140


def _cap(text: str, max_chars: int) -> str:
    """Cap a single header line by character count (readability cap; the
    engine separately enforces the summary byte budget). Whole-character
    truncation, so it never splits a codepoint."""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _strip_markdown(line: str) -> str:
    """Light markdown strip for a single line — leading heading/list/quote
    markers and surrounding emphasis/backticks. Not a full parser; the persona
    role line is plain prose in practice."""
    s = line.strip()
    while s[:1] in {"#", ">", "-", "*", "•"}:
        s = s[1:].lstrip()
    return s.strip("`*_ ").strip()


def _persona_lines(persona: str) -> list[str]:
    return [_strip_markdown(ln) for ln in persona.splitlines() if _strip_markdown(ln)]


def _split_server(tool_id: str) -> Optional[str]:
    """Extract ``<server>`` from ``mcp__<server>__<tool>``; None if not that
    shape."""
    if not tool_id.startswith("mcp__"):
        return None
    rest = tool_id[len("mcp__") :]
    sep = rest.find("__")
    if sep <= 0:
        return None
    return rest[:sep]


def _short_tool(tool_id: str) -> str:
    """The bare tool name after ``mcp__<server>__``."""
    rest = tool_id[len("mcp__") :]
    sep = rest.find("__")
    return rest[sep + 2 :] if sep >= 0 else rest


def _names_line(label: str, count: int, names: Sequence[str]) -> str:
    shown = list(names[:_MAX_NAMES_PER_GROUP])
    body = " · ".join(shown)
    if count > len(shown):
        body = f"{body} · …" if body else "…"
    return f"  {label} ({count}): {body}" if body else f"  {label} ({count})"


@dataclass
class IdentityCapabilitiesProvider:
    """Pinned header provider. ``provider_id`` is the reserved id; the assembler
    lets only this id declare band=pinned."""

    provider_id: str = field(default=PINNED_PROVIDER_ID, init=False)
    # official platform-owned provider — legitimately owns the reserved
    # ``identity-capabilities`` id and the pinned band (assembler trust rules).
    official: bool = field(default=True, init=False)

    config_path: str = ""
    prompt_files: Sequence[str] = ()
    workspace_name: str = "this workspace"
    runtime_kind: str = "claude-code"

    # injected seams (default to the real runtime functions; lazily imported so
    # the pure engine tests never pull in the heavy platform modules)
    persona_reader: Optional[Callable[[str, Sequence[str]], str]] = None
    tools_source: Optional[Callable[[], Optional[Sequence[str]]]] = None
    platform_agent_check: Optional[Callable[[], bool]] = None

    def _read_persona(self) -> str:
        reader = self.persona_reader
        if reader is None:
            from molecule_runtime import persona_render

            reader = persona_render.read_canonical_persona
        try:
            return reader(self.config_path, self.prompt_files) or ""
        except Exception:  # noqa: BLE001 — a bad persona file must never crash the tick
            return ""

    def _read_tools(self) -> list[str]:
        source = self.tools_source
        if source is None:
            from molecule_runtime import platform_agent_identity

            source = platform_agent_identity.loaded_mcp_tools
        try:
            return list(source() or [])
        except Exception:  # noqa: BLE001
            return []

    def _is_platform_agent(self) -> bool:
        check = self.platform_agent_check
        if check is None:
            from molecule_runtime import loaded_mcp_tools_probe

            check = loaded_mcp_tools_probe._is_platform_agent
        try:
            return bool(check())
        except Exception:  # noqa: BLE001
            return False

    def _identity_lines(self, persona: str) -> tuple[str, Optional[str]]:
        lines = _persona_lines(persona)
        if not lines:
            you_are = _cap(
                f"You are {self.workspace_name} — {self.runtime_kind} workspace.",
                _IDENTITY_LINE_MAX_CHARS,
            )
            return you_are, None
        you_are = _cap(
            f"You are {self.workspace_name} — {lines[0]}", _IDENTITY_LINE_MAX_CHARS
        )
        responsibility = None
        if len(lines) > 1:
            responsibility = _cap(
                f"Your responsibility: {lines[1]}", _IDENTITY_LINE_MAX_CHARS
            )
        return you_are, responsibility

    def _tool_groups(self, tools: list[str], is_platform: bool) -> tuple[list[str], int]:
        """Return (rendered group lines, total tool count)."""
        workspace: list[str] = []
        platform: list[str] = []
        plugin_servers: dict[str, int] = {}
        for tid in tools:
            server = _split_server(tid)
            if server == WORKSPACE_MCP_SERVER:
                workspace.append(_short_tool(tid))
            elif server == PLATFORM_MCP_SERVER:
                platform.append(_short_tool(tid))
            elif server:
                plugin_servers[server] = plugin_servers.get(server, 0) + 1
            # tools not in mcp__server__tool shape are ignored (never happens
            # for the normalized inventory)
        total = len(tools)

        group_lines: list[str] = []
        group_lines.append(_names_line("workspace MCP", len(workspace), sorted(workspace)))
        # platform MCP renders ONLY on the platform agent
        if is_platform and platform:
            group_lines.append(_names_line("platform MCP", len(platform), sorted(platform)))
        if plugin_servers:
            names = " · ".join(sorted(plugin_servers))
            group_lines.append(f"  from plugins: {names}")
        return group_lines, total

    async def contribute(self) -> list[Contribution]:
        persona = self._read_persona()
        you_are, responsibility = self._identity_lines(persona)
        tools = self._read_tools()
        is_platform = self._is_platform_agent()
        group_lines, total = self._tool_groups(tools, is_platform)

        parts = [you_are]
        if responsibility:
            parts.append(responsibility)
        parts.append(PRIORITIES_LINE)
        parts.append(REPLY_ROUTING_LINE)
        parts.append("Your available MCP tools (native):")
        parts.extend(group_lines)
        summary = "\n".join(parts)

        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.PINNED,
                tier=0,
                urgency=Urgency.NORMAL,
                count=total,
                summary=summary,
                age_band=AgeBand.NONE,  # nothing time-varying in the header
                item_ids=(),
                # No pull: the pinned header is a static capability REMINDER,
                # not a count+summary of pending items — there is nothing
                # bounded to fetch (the agent already sees its tool schemas and
                # installed plugins natively in context). Pointing a pull at a
                # plugin-listing tool would reference something not universally
                # available (the mgmt-MCP list tool exists only on the platform
                # agent and is not in the runtime tool registry). The pull-not-
                # dump rule governs the body providers that summarise many rows.
                pull=None,
            )
        ]

    def on_included(self, fired_at: float) -> None:  # stateless header
        return None
