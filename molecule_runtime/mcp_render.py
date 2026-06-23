"""Per-runtime renderers for an MCP-server descriptor.

This module is the *lego* behind the MCP-wiring PORT (``InstallContext.
register_mcp_server`` → ``BaseAdapter.register_mcp_server_hook``). Each runtime
reads MCP servers from a different native config file in a different format:

  * Claude Code → ``<configs>/.claude/settings.json`` ``mcpServers`` map (JSON).
  * Codex       → ``~/.codex/config.toml`` ``[mcp_servers.<name>]`` tables (TOML).
  * Gemini CLI  → ``~/.gemini/settings.json`` ``mcpServers`` map (JSON) — TODO,
                  format unverified.
  * Hermes      → ``platforms.*`` / entry-point descriptor — TODO, format
                  unverified.

The descriptor is runtime-agnostic — ``name -> {command, args?, env?}`` — and is
exactly the ``mcpServers`` entry shape pinned by
``contracts/mcp-plugin-delivery.contract.json`` (``entry_shape``). The plugin is
the SSOT for that descriptor (RFC §2b); ``settings-fragment.json`` is just the
Claude adapter's *rendering* of it, so the renderers here re-derive each native
file from the same descriptor rather than from a runtime-locked source file.

The functions are PURE filesystem renderers: they take a target file path, a
server name, and a spec dict, and additively merge the entry in (idempotent,
never evicting other servers). They do NOT decide *which* runtime — that's the
adapter's job (it picks the renderer for ``self.name()``).

RFC §5b Addition 1: a per-runtime render matrix unit test asserts each renderer
writes the RIGHT native file (and, critically, that codex does NOT write
``.claude/settings.json`` — the test that would have caught the #3159 bug where
a codex concierge silently got the MCP written to a file its runtime never
reads).
"""

from __future__ import annotations

import json
from pathlib import Path

# The settings.json map key under which MCP servers live for the JSON runtimes
# (Claude Code, Gemini CLI). Tied to the cross-repo contract ``key``.
MCPSERVERS_KEY = "mcpServers"

# Codex reads MCP servers from this TOML table.
CODEX_MCP_TABLE = "mcp_servers"


# ---------------------------------------------------------------------------
# Claude Code (also the BaseAdapter default) — JSON settings.json
# ---------------------------------------------------------------------------

def render_claude_settings(settings_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into the Claude ``settings.json``
    ``mcpServers`` map. Idempotent; preserves every other key + server.

    This is the historical behavior the MCPServerAdaptor relied on (the
    ``_merge_settings_fragment`` ``mcpServers`` path), now reachable as a named
    renderer so it can be shared by the default hook and asserted directly.
    """
    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
    else:
        data = {}

    servers = data.get(MCPSERVERS_KEY)
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = dict(spec)
    data[MCPSERVERS_KEY] = servers

    settings_path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Codex — ~/.codex/config.toml  [mcp_servers.<name>]
# ---------------------------------------------------------------------------

def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(v) -> str:
    """Render a scalar/list value as a TOML literal. Scoped to the value
    shapes an MCP spec carries (str, list[str], and nested str->str env dict
    handled by the caller)."""
    if isinstance(v, str):
        return f'"{_toml_escape(v)}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    # Fallback: stringify (defensive — MCP specs shouldn't reach here).
    return f'"{_toml_escape(str(v))}"'


def _render_codex_table(name: str, spec: dict) -> str:
    """Emit the ``[mcp_servers.<name>]`` TOML block for a single server.

    Codex's config.toml expresses an MCP server as::

        [mcp_servers.molecule-platform]
        command = "npx"
        args = ["-y", "@molecule-ai/mcp-server"]
        [mcp_servers.molecule-platform.env]
        MOLECULE_MCP_MODE = "management"

    We keep ``env`` (a str->str map) in its own sub-table, which is the
    unambiguous TOML form and avoids inline-table escaping edge cases.
    """
    lines = [f"[{CODEX_MCP_TABLE}.{name}]"]
    env = None
    for k, v in spec.items():
        if k == "env" and isinstance(v, dict):
            env = v
            continue
        lines.append(f"{k} = {_toml_value(v)}")
    if env:
        lines.append("")
        lines.append(f"[{CODEX_MCP_TABLE}.{name}.env]")
        for ek, ev in env.items():
            lines.append(f"{ek} = {_toml_value(ev)}")
    return "\n".join(lines) + "\n"


# Marker comment wrapping a managed server block so re-rendering is idempotent
# (we strip our prior block by marker, then re-append). Keeps any hand-edited
# user TOML outside the markers untouched.
def _codex_markers(name: str) -> tuple[str, str]:
    begin = f"# >>> molecule-mcp:{name} >>>"
    end = f"# <<< molecule-mcp:{name} <<<"
    return begin, end


def render_codex_config(config_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into the codex ``config.toml``
    ``[mcp_servers.<name>]`` table. Idempotent; preserves the rest of the file.

    Because we can't take a TOML-writer dependency, we manage each server as a
    marker-delimited block. On re-install we strip the prior block for *this*
    server name and re-append the freshly rendered one, leaving every other
    server (and any hand-written config) intact.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = config_path.read_text() if config_path.is_file() else ""
    begin, end = _codex_markers(name)

    # Strip any prior managed block for this server (idempotent re-install).
    if begin in existing and end in existing:
        head, _, rest = existing.partition(begin)
        _, _, tail = rest.partition(end)
        existing = head.rstrip("\n") + ("\n" + tail.lstrip("\n") if tail.strip() else "")

    block = f"{begin}\n{_render_codex_table(name, spec)}{end}\n"
    sep = "" if existing == "" or existing.endswith("\n") else "\n"
    config_path.write_text(existing + sep + block)


# ---------------------------------------------------------------------------
# Gemini CLI — ~/.gemini/settings.json  (TODO: format unverified)
# ---------------------------------------------------------------------------

def render_gemini_settings(settings_path: Path, name: str, spec: dict) -> None:
    """TODO(#3159): Gemini CLI's native MCP config shape is unverified.

    Public Gemini CLI docs describe a ``~/.gemini/settings.json`` with an
    ``mcpServers`` map that looks JSON-identical to Claude's, but the
    command/args/env field names have NOT been confirmed against a real
    install, and Gemini may use ``httpUrl``/``url`` transports we don't model
    here. Rather than guess and ship a config a gemini concierge silently can't
    read (the exact #3159 failure mode), this is left as a marked stub with a
    skipped render-matrix test. Implement concretely once the format is pinned
    against a live gemini-cli runtime.
    """
    raise NotImplementedError(
        "gemini-cli MCP render not implemented — format unverified (#3159 follow-up)"
    )


# ---------------------------------------------------------------------------
# Hermes — platforms.* / entry-point descriptor (TODO: format unverified)
# ---------------------------------------------------------------------------

def render_hermes_config(config_path: Path, name: str, spec: dict) -> None:
    """TODO(#3159): Hermes' native MCP wiring shape is unverified.

    Hermes wires capabilities through a ``platforms.*`` / entry-point
    descriptor rather than a Claude-style settings.json. The concrete file
    location and schema are not confirmed in this repo, so this is a marked
    stub with a skipped render-matrix test. Implement concretely once the
    hermes adapter's descriptor format is pinned.
    """
    raise NotImplementedError(
        "hermes MCP render not implemented — format unverified (#3159 follow-up)"
    )
