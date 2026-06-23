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
import os
from pathlib import Path

# The settings.json map key under which MCP servers live for the JSON runtimes
# (Claude Code, Gemini CLI). Tied to the cross-repo contract ``key``.
MCPSERVERS_KEY = "mcpServers"

# Codex reads MCP servers from this TOML table.
CODEX_MCP_TABLE = "mcp_servers"


def normalize_runtime(runtime: str) -> str:
    """Canonicalize a runtime identifier to the underscore dispatch key
    (``claude-code`` -> ``claude_code``)."""
    return (runtime or "").strip().lower().replace("-", "_")


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


# ===========================================================================
# Per-runtime dispatch — the production wiring.
# ===========================================================================
# The default BaseAdapter hook dispatches HERE on the active runtime name
# (self.name()), so a REAL codex concierge gets codex rendering through the
# normal install_plugins_via_registry path WITHOUT needing a per-template
# adapter override. Without this dispatch the default hook would render Claude
# settings.json for every runtime — which is the exact #3159 flaw (a codex run
# writing the management MCP to a file its runtime never reads).
#
# Each runtime entry declares:
#   path:    config_path -> absolute native MCP-config file for this runtime
#   render:  (Path, name, spec) -> None   native renderer
#   present: (Path) -> bool               does this native file declare `name`?
# The renderer for an unverified runtime raises NotImplementedError (fail-loud),
# which the privileged-plugin install path turns into a loud boot failure rather
# than a silently capability-less concierge.


def _claude_path(config_path: str | os.PathLike) -> Path:
    return Path(config_path) / ".claude" / "settings.json"


def _codex_path(config_path: str | os.PathLike) -> Path:
    # Codex reads ~/.codex/config.toml. config_path is unused (the codex CLI
    # resolves $HOME), but the signature is uniform across runtimes.
    return Path(os.path.expanduser("~")) / ".codex" / "config.toml"


def _gemini_path(config_path: str | os.PathLike) -> Path:
    return Path(os.path.expanduser("~")) / ".gemini" / "settings.json"


def _json_settings_has(settings_path: Path, name: str) -> bool:
    try:
        data = json.loads(Path(settings_path).read_text())
    except (OSError, ValueError):
        return False
    servers = data.get(MCPSERVERS_KEY) if isinstance(data, dict) else None
    return isinstance(servers, dict) and name in servers


def _codex_config_has(config_path: Path, name: str) -> bool:
    """True when the codex config.toml declares ``[mcp_servers.<name>]``.

    Uses stdlib ``tomllib`` (read-only; available 3.11+, our floor) so the
    present-check parses the real TOML rather than string-matching markers.
    """
    import tomllib

    try:
        data = tomllib.loads(Path(config_path).read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return False
    table = data.get(CODEX_MCP_TABLE)
    return isinstance(table, dict) and name in table


# runtime -> (path_resolver, renderer, present_reader). Unverified runtimes get
# the fail-loud renderer; their present_reader returns False (never silently
# "present"). claude_code is also the default for any unknown runtime so the
# base behavior is unchanged when a new runtime hasn't been mapped yet.
_RUNTIME_SPECS: dict[str, tuple] = {
    "claude_code": (_claude_path, render_claude_settings, _json_settings_has),
    "codex": (_codex_path, render_codex_config, _codex_config_has),
    "gemini_cli": (_gemini_path, render_gemini_settings, _json_settings_has),
    # hermes: native path unverified; fail-loud render, never falsely present.
    "hermes": (_claude_path, render_hermes_config, lambda p, n: False),
}

# The runtime used when the active runtime isn't mapped above. Claude Code is
# the base runtime, so an unmapped runtime keeps today's behavior rather than
# crashing — EXCEPT the privileged management MCP, whose install path fails loud
# on a runtime it can't prove it rendered for.
_DEFAULT_RUNTIME = "claude_code"


# Runtimes whose renderer is a deliberate fail-loud stub (format unverified).
_UNVERIFIED_RUNTIMES = frozenset({"gemini_cli", "hermes"})


def _spec_for(runtime: str) -> tuple:
    return _RUNTIME_SPECS.get(normalize_runtime(runtime), _RUNTIME_SPECS[_DEFAULT_RUNTIME])


def is_runtime_supported(runtime: str) -> bool:
    """True when this runtime has a CONCRETE (non-stub) renderer mapped.

    Unmapped runtimes fall back to the claude renderer (supported); the
    gemini/hermes stubs are mapped but their renderer raises, so they are
    reported unsupported."""
    return normalize_runtime(runtime) not in _UNVERIFIED_RUNTIMES


def mcp_settings_path_for(runtime: str, config_path: str | os.PathLike) -> Path:
    """Absolute native MCP-config file the given runtime reads from."""
    return _spec_for(runtime)[0](config_path)


def render_for_runtime(runtime: str, config_path: str | os.PathLike, name: str, spec: dict) -> Path:
    """Render ``name -> spec`` into the given runtime's native MCP config.

    Returns the path written. Raises NotImplementedError for an unverified
    runtime (gemini/hermes) — the caller decides whether that's fatal (it is for
    the privileged management MCP)."""
    path_fn, render_fn, _ = _spec_for(runtime)
    target = path_fn(config_path)
    render_fn(target, name, spec)
    return target


def management_mcp_present_for(runtime: str, config_path: str | os.PathLike, name: str) -> bool:
    """True when ``name`` is declared in the given runtime's native MCP config."""
    path_fn, _, present_fn = _spec_for(runtime)
    return present_fn(path_fn(config_path), name)
