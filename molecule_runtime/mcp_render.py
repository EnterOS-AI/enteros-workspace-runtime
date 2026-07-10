"""Per-runtime renderers for an MCP-server descriptor.

This module is the *lego* behind the MCP-wiring PORT (``InstallContext.
register_mcp_server`` → ``BaseAdapter.register_mcp_server_hook``). Each runtime
reads MCP servers from a different native config file in a different format:

  * Claude Code → ``<configs>/.claude/settings.json`` ``mcpServers`` map (JSON).
  * Codex       → ``~/.codex/config.toml`` ``[mcp_servers.<name>]`` tables (TOML).
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

# SSOT for the operator-default runtime — the ONLY value an unmapped runtime may
# fall back to (never a hand-set ``claude_code``). See live_runtimes.py.
from molecule_runtime.live_runtimes import DEFAULT_RUNTIME as _SSOT_DEFAULT_RUNTIME

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
# Hermes — platforms.* / entry-point descriptor (TODO: format unverified)
# ---------------------------------------------------------------------------

# Hermes reads its native MCP servers from ~/.hermes/config.yaml under a
# top-level ``mcp_servers:`` map (NOT claude's ``mcpServers``). Pinned against the
# config the hermes gateway consumes (the template's start.sh writes the a2a
# ``molecule`` sidecar into this same key; the hermes adapter's
# enumerate_loaded_mcp_tools reads it back).
HERMES_MCP_KEY = "mcp_servers"


def render_hermes_config(config_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into hermes' native
    ``~/.hermes/config.yaml`` ``mcp_servers`` map. Idempotent; preserves the rest
    of the file (the ``model`` block, the a2a ``molecule`` url sidecar, any other
    server or hand-written key).

    Mirrors :func:`render_openclaw_config`: a PURE filesystem renderer that writes
    the on-disk shape the hermes gateway reads — testable without the hermes
    binary. The stdio descriptor (``{command, args?, env?}``) is written verbatim
    under ``mcp_servers.<name>`` — the SAME runtime-agnostic entry_shape the plugin
    ships and the claude/codex/openclaw renderers consume. The privileged org-admin
    env is already merged into ``spec['env']`` by the base install funnel
    (``inject_privileged_env``) BEFORE this renderer is called, so this stays a
    dumb, credential-agnostic writer.
    """
    import yaml

    settings_path = Path(config_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.is_file():
        try:
            data = yaml.safe_load(settings_path.read_text())
        except (OSError, yaml.YAMLError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}

    servers = data.get(HERMES_MCP_KEY)
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = spec
    data[HERMES_MCP_KEY] = servers

    settings_path.write_text(yaml.safe_dump(data, sort_keys=False))


# ---------------------------------------------------------------------------
# Gemini / google-adk — native MCP-wiring convention UNVERIFIED (fail-loud stub)
# ---------------------------------------------------------------------------

def render_gemini_config(config_path: Path, name: str, spec: dict) -> None:
    """Fail-loud stub for the gemini / google-adk MCP-wiring convention.

    google-adk wires MCP through its own toolset config (an ADK ``McpToolset`` /
    connection descriptor), and the Gemini CLI reads ``mcpServers`` from its own
    settings file — but neither native location has been pinned against a live
    runtime in this repo. Rather than GUESS and silently render into a file the
    runtime never reads (the exact #3159 mis-attribution — which is what the old
    silent ``claude_code`` fallback did for these unmapped runtimes), this raises,
    matching the ``builtins.py`` install path that already anticipates a
    "gemini/hermes" MCP stub. Implement concretely once the native format is
    verified against a live gemini / google-adk CLI."""
    raise NotImplementedError(
        "gemini/google-adk MCP render not implemented — native MCP convention "
        "unverified (no silent claude_code fallback; pin against a live CLI)"
    )


# ---------------------------------------------------------------------------
# OpenClaw — ~/.openclaw/openclaw.json  mcp.servers.<name>  (phase P4)
# ---------------------------------------------------------------------------
# Native format PINNED against a live ``openclaw@2026.5.7`` CLI (phase P4).
# ``openclaw mcp set <name> '<json>'`` persists the server under
# ``mcp.servers.<name>`` in ``~/.openclaw/openclaw.json``. A STDIO server is the
# bare ``{command, args?, env?}`` form — the SAME descriptor shape the plugin
# ships and the codex/claude renderers consume — so ``@molecule-ai/mcp-server``
# registers DIRECTLY as a stdio child, no HTTP shim (unlike the a2a sidecar in
# adapter.py, which needs HTTP only because a2a_mcp_server's stdio main blocks).
# Verified round-trip: ``openclaw mcp set`` / ``mcp show`` echo back exactly
# this ``{command, args, env}`` block under ``mcp.servers``.

# The openclaw.json key path under which MCP servers live (mcp -> servers map).
OPENCLAW_MCP_PARENT = "mcp"
OPENCLAW_MCP_SERVERS = "servers"


def render_openclaw_config(config_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into ``~/.openclaw/openclaw.json``'s
    ``mcp.servers`` map. Idempotent; preserves every other key + server.

    This produces the exact on-disk shape ``openclaw mcp set <name> '<json>'``
    writes (verified against openclaw@2026.5.7): the stdio descriptor
    (``{command, args?, env?}``) nested under ``mcp.servers.<name>``. We write
    the JSON directly rather than shelling out so the renderer stays a PURE,
    side-effect-scoped filesystem renderer (testable without the CLI, matching
    the claude/codex renderers) and works even when the openclaw binary isn't on
    PATH at install time. The openclaw gateway re-reads this file on start, so a
    direct write is equivalent to the CLI mutation.
    """
    settings_path = Path(config_path)
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

    mcp = data.get(OPENCLAW_MCP_PARENT)
    if not isinstance(mcp, dict):
        mcp = {}
    servers = mcp.get(OPENCLAW_MCP_SERVERS)
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = dict(spec)
    mcp[OPENCLAW_MCP_SERVERS] = servers
    data[OPENCLAW_MCP_PARENT] = mcp

    settings_path.write_text(json.dumps(data, indent=2) + "\n")


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


def _openclaw_path(config_path: str | os.PathLike) -> Path:
    # OpenClaw reads ~/.openclaw/openclaw.json (the file `openclaw mcp set`
    # mutates). config_path is unused (openclaw resolves $HOME), but the
    # signature is uniform across runtimes — mirrors _codex_path.
    return Path(os.path.expanduser("~")) / ".openclaw" / "openclaw.json"


def _hermes_path(config_path: str | os.PathLike) -> Path:
    # Hermes reads ~/.hermes/config.yaml. HERMES_HOME overrides the dir; the
    # container sets HERMES_HOME=/tmp/.hermes with HOME=/tmp so both agree.
    # config_path (the /configs dir) is unused — hermes resolves its own home,
    # mirroring _openclaw_path. Kept in LOCKSTEP with the hermes adapter's
    # enumerate_loaded_mcp_tools reader (same HERMES_HOME-or-HOME resolution) so a
    # server the renderer writes is byte-for-byte the file the adapter enumerates.
    home = os.environ.get("HERMES_HOME") or os.path.join(
        os.path.expanduser("~"), ".hermes"
    )
    return Path(home) / "config.yaml"


def _gemini_path(config_path: str | os.PathLike) -> Path:
    # gemini / google-adk native MCP file is UNVERIFIED — this best-guess path
    # (the Gemini CLI ~/.gemini/settings.json convention) exists only so
    # mcp_settings_path_for returns a non-claude path; the renderer itself
    # fail-loud raises before this file is ever written. Do NOT rely on it.
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


def _openclaw_config_has(config_path: Path, name: str) -> bool:
    """True when ``~/.openclaw/openclaw.json`` declares ``mcp.servers.<name>``.

    Fail-closed by construction: a missing, unreadable, malformed, or
    structurally-unexpected config yields False, so a genuinely MCP-less
    openclaw concierge stays fail-closed (degraded) at the RCA#2970 gate."""
    try:
        data = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    mcp = data.get(OPENCLAW_MCP_PARENT)
    if not isinstance(mcp, dict):
        return False
    servers = mcp.get(OPENCLAW_MCP_SERVERS)
    return isinstance(servers, dict) and name in servers


def _hermes_config_has(config_path: Path, name: str) -> bool:
    """True when ``~/.hermes/config.yaml`` declares ``mcp_servers.<name>``.

    Fail-closed by construction: a missing, unreadable, malformed, or
    structurally-unexpected config yields False, so a genuinely MCP-less hermes
    concierge stays fail-closed (degraded) at the RCA#2970 gate."""
    import yaml

    try:
        data = yaml.safe_load(Path(config_path).read_text())
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get(HERMES_MCP_KEY)
    return isinstance(servers, dict) and name in servers


# runtime -> (path_resolver, renderer, present_reader). Unverified runtimes get
# the fail-loud renderer; their present_reader returns False (never silently
# "present"). claude_code is also the default for any unknown runtime so the
# base behavior is unchanged when a new runtime hasn't been mapped yet.
_RUNTIME_SPECS: dict[str, tuple] = {
    "claude_code": (_claude_path, render_claude_settings, _json_settings_has),
    "codex": (_codex_path, render_codex_config, _codex_config_has),
    # hermes: CONCRETE renderer + present-reader pinned against the hermes-native
    # ~/.hermes/config.yaml `mcp_servers` map (the file the gateway reads and the
    # template's start.sh writes the a2a sidecar into). Renders/probes the stdio
    # descriptor there; fail-closed present-check on absence.
    "hermes": (_hermes_path, render_hermes_config, _hermes_config_has),
    # gemini / google-adk: native MCP convention UNVERIFIED — fail-loud stub,
    # never falsely present. Registered EXPLICITLY (not left to the default
    # fallback) so an MCP install on these runtimes fails loud instead of
    # silently rendering into a file they never read (the old claude_code creep
    # for these unmapped runtimes — the #3159 class of bug). Matches the
    # builtins.py install path that already anticipates a "gemini/hermes" stub.
    "gemini": (_gemini_path, render_gemini_config, lambda p, n: False),
    "google_adk": (_gemini_path, render_gemini_config, lambda p, n: False),
    # openclaw (phase P4): CONCRETE renderer + present-reader pinned against a
    # live openclaw@2026.5.7. Renders the stdio descriptor into
    # ~/.openclaw/openclaw.json mcp.servers.<name> (the file `openclaw mcp set`
    # mutates); present-reader parses the same file, fail-closed on absence.
    "openclaw": (_openclaw_path, render_openclaw_config, _openclaw_config_has),
}

# The runtime an UNMAPPED runtime falls back to. Derived from the LIVE_RUNTIMES
# SSOT (``openclaw`` — the operator default), NEVER hand-set to ``claude_code``:
# a new/unknown runtime renders into the OPERATOR default's convention, so the
# "reference runtime" can't silently creep back to claude-code. Every LIVE
# runtime is still REQUIRED (by the Guard A meta-gate) to have its OWN concrete
# entry or a documented fail-loud exemption — the fallback is only a safety net
# for genuinely-unknown (non-live) runtimes, not a substitute for registration.
_DEFAULT_RUNTIME = _SSOT_DEFAULT_RUNTIME


# Runtimes whose renderer is a deliberate fail-loud stub (format unverified).
# openclaw graduated out (phase P4) and hermes graduated out (runtime#181): each
# now has a concrete renderer + present-reader verified against its native config
# (~/.openclaw/openclaw.json, ~/.hermes/config.yaml). gemini/google-adk remain
# fail-loud until their native MCP convention is pinned against a live CLI.
_UNVERIFIED_RUNTIMES = frozenset({"gemini", "google_adk"})


def _spec_for(runtime: str) -> tuple:
    """Resolve a runtime to its ``(path, render, present)`` spec.

    A mapped runtime returns its OWN concrete entry. An unmapped runtime falls
    back to the operator-default runtime (``_DEFAULT_RUNTIME`` = openclaw) — never
    to claude-code. Total by construction: if the default key itself is somehow
    absent (only reachable in a monkeypatched self-test that pops it), it degrades
    to the base ``claude_code`` entry so callers never hit a KeyError; this final
    degradation is exactly what the G5 mis-attribution self-test exercises."""
    key = normalize_runtime(runtime)
    spec = _RUNTIME_SPECS.get(key)
    if spec is not None:
        return spec
    return _RUNTIME_SPECS.get(_DEFAULT_RUNTIME) or _RUNTIME_SPECS["claude_code"]


def is_runtime_supported(runtime: str) -> bool:
    """True when this runtime has a CONCRETE (non-stub) renderer mapped.

    Unmapped runtimes fall back to the claude renderer (supported); the
    hermes stub is mapped but its renderer raises, so it is reported
    unsupported."""
    return normalize_runtime(runtime) not in _UNVERIFIED_RUNTIMES


def mcp_settings_path_for(runtime: str, config_path: str | os.PathLike) -> Path:
    """Absolute native MCP-config file the given runtime reads from."""
    return _spec_for(runtime)[0](config_path)


def render_for_runtime(runtime: str, config_path: str | os.PathLike, name: str, spec: dict) -> Path:
    """Render ``name -> spec`` into the given runtime's native MCP config.

    Returns the path written. Raises NotImplementedError for an unverified
    runtime (hermes) — the caller decides whether that's fatal (it is for
    the privileged management MCP)."""
    path_fn, render_fn, _ = _spec_for(runtime)
    target = path_fn(config_path)
    render_fn(target, name, spec)
    return target


def management_mcp_present_for(runtime: str, config_path: str | os.PathLike, name: str) -> bool:
    """True when ``name`` is declared in the given runtime's native MCP config."""
    path_fn, _, present_fn = _spec_for(runtime)
    return present_fn(path_fn(config_path), name)


# ===========================================================================
# Native MCP-server READERS — return the full {name: spec} server map from the
# active runtime's native config (the inverse of the renderers above).
# ===========================================================================
# The loaded_mcp_tools producer (core#3082) enumerates the connected MCP
# servers' tools AT INIT. To do that it first needs the list of declared servers
# (and their launch command/args/env) from the file THIS runtime actually reads
# — codex's config.toml, claude's settings.json, openclaw's openclaw.json. These
# readers are the exact inverse of the renderers and reuse the same path
# resolution + native parser as the present-checks, so a server the producer
# enumerates is byte-for-byte the one the runtime will launch. Every reader is
# fail-closed: a missing/unreadable/malformed/structurally-unexpected config
# yields ``{}`` so the producer degrades safely (never crashes boot).


def _read_json_mcp_servers(settings_path: Path) -> dict:
    """Read the ``mcpServers`` map from a JSON settings file (claude)."""
    try:
        data = json.loads(Path(settings_path).read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get(MCPSERVERS_KEY) if isinstance(data, dict) else None
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def _read_codex_mcp_servers(config_path: Path) -> dict:
    """Read the ``[mcp_servers.<name>]`` tables from codex's config.toml."""
    import tomllib

    try:
        data = tomllib.loads(Path(config_path).read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    table = data.get(CODEX_MCP_TABLE)
    return {k: v for k, v in table.items() if isinstance(v, dict)} if isinstance(table, dict) else {}


def _read_openclaw_mcp_servers(config_path: Path) -> dict:
    """Read the ``mcp.servers`` map from ``~/.openclaw/openclaw.json``."""
    try:
        data = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    mcp = data.get(OPENCLAW_MCP_PARENT)
    if not isinstance(mcp, dict):
        return {}
    servers = mcp.get(OPENCLAW_MCP_SERVERS)
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


# runtime -> native-config reader. Mirrors _RUNTIME_SPECS (same path resolver +
# native format), but returns the FULL {name: spec} map rather than a single
# present-bool. The loaded_mcp_tools producer's core path (enumerate_loaded_mcp_
# tools_async) uses these; gemini/google-adk return {} because their format is
# unpinned (fail-silent rather than guess).
#
# hermes returns {} DELIBERATELY — NOT because its format is unknown (it isn't;
# render/present above are concrete), but because hermes owns loaded-tools
# DISCOVERY in its ADAPTER (HermesAgentAdapter.enumerate_loaded_mcp_tools reads
# ~/.hermes/config.yaml directly and probes it — runtime#181). The adapter
# OVERRIDES the base enumerate contract, so core's reader switch is never
# consulted for hermes; keeping this {} makes that ownership explicit (the
# discovery is in adapter code, not this core switch) rather than duplicating it.
_RUNTIME_READERS: dict[str, callable] = {
    "claude_code": _read_json_mcp_servers,
    "codex": _read_codex_mcp_servers,
    "hermes": lambda _p: {},  # see note above — adapter owns hermes discovery
    # gemini/google-adk native MCP format unverified — report nothing rather
    # than guessing (same fail-loud-vs-fail-silent stance as their renderer).
    "gemini": lambda _p: {},
    "google_adk": lambda _p: {},
    "openclaw": _read_openclaw_mcp_servers,
}


def read_mcp_servers_for(runtime: str, config_path: str | os.PathLike) -> dict:
    """Return ``{server_name: spec}`` declared in the runtime's native MCP config.

    Reuses the SAME per-runtime path resolver as the renderers/present-checks, so
    the producer enumerates exactly the servers this runtime will launch. Fail-
    closed: returns ``{}`` for an unmapped/unverified runtime or any unreadable /
    malformed config.
    """
    path_fn = _spec_for(runtime)[0]
    reader = _RUNTIME_READERS.get(normalize_runtime(runtime), lambda _p: {})
    return reader(path_fn(config_path))
