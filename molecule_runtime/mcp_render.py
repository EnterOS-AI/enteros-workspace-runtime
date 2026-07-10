"""Generic, runtime-name-free helpers for rendering/reading an MCP descriptor.

ADR-004 (`docs/adr/ADR-004-sdk-owns-adapter-contract-and-registry.md`) moved the
**per-runtime shape** of the MCP-config seam — the renderers, their inverse
readers, and the present-probe — OUT of this shared engine and INTO each adapter.
The SDK owns the adapter *socket* contract + the official registry
(`molecule-ai-sdk:contracts/adapter/`); official adapters ship their own native
render/read/present in their template repos
(`molecule-ai-workspace-template-<runtime>/adapter.py`); third-party adapters ship
theirs wherever their author wants. **This module holds ZERO per-runtime
dispatch** — no `_RUNTIME_SPECS`, no `render_for_runtime`, no runtime-name
literals. It never spells a runtime name.

What remains here is the small set of GENERIC, name-free helpers ADR-004 §6 says
the engine keeps — reusable by any adapter (official or third-party):

  * :func:`normalize_runtime` — the ``-``→``_`` canonicalization every seam runs
    on its own input before use.
  * A generic **JSON ``mcpServers``** render / read / present triple
    (:func:`render_json_mcp_servers` / :func:`read_json_mcp_servers` /
    :func:`json_mcp_servers_has`). This is the ``{command, args?, env?}``
    descriptor written into a top-level JSON ``mcpServers`` map — the shape
    ``platform_agent_identity`` re-asserts and the shape the BaseAdapter default
    (``adapter_base``) uses for a not-yet-migrated / third-party adapter that
    doesn't override the seam. It carries no runtime *name*; the adapter decides
    which native path/format it renders (the official four all override to their
    own native format — codex TOML, hermes/openclaw YAML/JSON — in their template
    repos).

The descriptor these helpers speak is the runtime-agnostic entry shape pinned by
``contracts/mcp/mcp-plugin-delivery.contract.json`` (``entry_shape``):
``name -> {command, args?, env?}``. The plugin is the SSOT for the descriptor;
each renderer re-derives its native file from that one descriptor.

DO NOT re-add a per-runtime dispatch table here. A red-on-regression ratchet
(``tests/test_engine_no_runtime_dispatch_ratchet.py``) fails any change that
re-introduces a ``_RUNTIME_*`` table or a runtime-name literal into this module —
per ADR-004 the drift can only shrink (→ 0).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The top-level map key under which MCP servers live in a JSON native config
# (the generic ``mcpServers`` shape; also the cross-repo delivery-contract ``key``
# for JSON native configs). Not a runtime name — a descriptor/format constant,
# imported by ``mcp_readiness_probe`` / pinned against the delivery contract.
MCPSERVERS_KEY = "mcpServers"


def normalize_runtime(runtime: str) -> str:
    """Canonicalize a runtime identifier to the underscore dispatch key
    (``claude-code`` -> ``claude_code``).

    A pure string helper (no runtime-name knowledge): every seam that keys on a
    runtime id normalizes its own input through this before use, so hyphen and
    underscore spellings compare equal."""
    return (runtime or "").strip().lower().replace("-", "_")


# NOTE (ADR-004): the TOML scalar helpers (``_toml_escape`` / ``_toml_value``) and
# the ``CODEX_MCP_TABLE`` constant that the deleted codex renderer used are GONE —
# a TOML-native adapter (codex) owns its OWN TOML rendering in its template repo,
# so the engine keeps no TOML render helpers (nothing here consumed them, and
# ``CODEX_MCP_TABLE`` spelled a runtime name the drift-down ratchet forbids).


# ---------------------------------------------------------------------------
# Generic JSON ``mcpServers`` render / read / present — the BaseAdapter default.
# ---------------------------------------------------------------------------
# The name-free triple the BaseAdapter fallback (adapter_base) uses when an
# adapter does not override the MCP seam: additively merge / read / present a
# ``{command, args?, env?}`` descriptor under a top-level JSON ``mcpServers`` map.
# An adapter with a different native format (codex TOML, hermes/openclaw
# YAML/JSON) owns its own render/read/present in its template repo; this is only
# the sane default for a not-yet-migrated / third-party adapter.

def render_json_mcp_servers(settings_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into a JSON native config's top-level
    ``mcpServers`` map. Idempotent; preserves every other key + server.

    Additive (never evicts another server or a hand-written key) and byte-stable
    (re-rendering the same descriptor rewrites identical bytes:
    ``json.dumps(data, indent=2) + "\\n"``)."""
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


def json_mcp_servers_has(settings_path: Path, name: str) -> bool:
    """True when a JSON native config declares ``mcpServers.<name>``.

    Fail-closed by construction: a missing / unreadable / malformed /
    structurally-unexpected config yields ``False``."""
    try:
        data = json.loads(Path(settings_path).read_text())
    except (OSError, ValueError):
        return False
    servers = data.get(MCPSERVERS_KEY) if isinstance(data, dict) else None
    return isinstance(servers, dict) and name in servers


def read_json_mcp_servers(settings_path: Path) -> dict:
    """Read the ``mcpServers`` ``{name: spec}`` map from a JSON native config.

    The inverse of :func:`render_json_mcp_servers`; returns only dict-valued
    entries. Fail-closed ``{}`` on a missing / unreadable / malformed /
    structurally-unexpected file."""
    try:
        data = json.loads(Path(settings_path).read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get(MCPSERVERS_KEY) if isinstance(data, dict) else None
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def default_json_settings_path(config_path: str | os.PathLike) -> Path:
    """The default JSON native MCP-config path the BaseAdapter fallback resolves
    (``<config_path>/.claude/settings.json``).

    This is the base/reference JSON native config location (the same path
    ``platform_agent_identity.SETTINGS_PATH`` re-asserts). It is NOT a per-runtime
    resolver — an adapter whose runtime reads a different file (codex, openclaw,
    hermes) resolves its OWN path in its template repo. Kept only so the base
    default has a concrete file to render/read when an adapter does not override
    the seam."""
    return Path(config_path) / ".claude" / "settings.json"
