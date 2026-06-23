"""Per-runtime plugin adaptor registry with hybrid resolution.

Resolution order for ``(plugin_name, runtime)``:

  1. Platform registry  → ``workspace/plugins_registry/<plugin>/<runtime>.py``
  2. Plugin-shipped     → ``<plugin_root>/adapters/<runtime>.py``
  3. Raw filesystem     → :class:`RawDropAdaptor` (warns, drops files only)

Path #1 wins so the platform can override or hot-fix a third-party adaptor
without forking the upstream plugin repo. Path #2 is the SDK contract: a
single GitHub repo ships its own adaptors and is installable on day one.
Path #3 is the escape hatch — power users can still bring unsupported
plugins onto a workspace, they just don't get tools wired up.

A registered adaptor module must expose either:
  - ``Adaptor`` class implementing :class:`PluginAdaptor`, OR
  - ``def get_adaptor(plugin_name, runtime) -> PluginAdaptor``
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Optional

from .protocol import InstallContext, InstallResult, PluginAdaptor
from .raw_drop import RawDropAdaptor

logger = logging.getLogger(__name__)

# Where the platform-curated registry lives. Resolved relative to this file
# so it works regardless of CWD or how workspace-template is installed.
_REGISTRY_ROOT = Path(__file__).parent

__all__ = [
    "InstallContext",
    "InstallResult",
    "PluginAdaptor",
    "RawDropAdaptor",
    "resolve",
    "AdaptorSource",
]


class AdaptorSource:
    REGISTRY = "registry"
    PLUGIN = "plugin"
    MCP_SERVER = "mcp_server_default"
    AGENTSKILLS = "agentskills_default"
    RAW_DROP = "raw_drop"


def _load_module_from_path(module_name: str, path: Path):
    """Import a Python file by absolute path. Returns the module or None on failure."""
    # Ensure the plugins_registry package and its submodules are importable in the
    # fresh module namespace created by module_from_spec().  Plugin adapters
    # (molecule-skill-*/adapters/*.py) use "from plugins_registry.builtins import ..."
    # which requires plugins_registry and its submodules to already be in sys.modules.
    # We import and register them before exec_module so the plugin's own
    # from ... import statements resolve correctly.
    import sys
    import molecule_runtime.plugins_registry as plugins_registry
    sys.modules.setdefault("plugins_registry", plugins_registry)
    for _sub in ("builtins", "protocol", "raw_drop"):
        try:
            sub = importlib.import_module(f"plugins_registry.{_sub}")
            sys.modules.setdefault(f"plugins_registry.{_sub}", sub)
        except Exception:
            # Submodule may not exist in all versions; skip if absent.
            pass
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("Failed to load adaptor module %s: %s", path, exc)
        return None
    return module


def _instantiate(module, plugin_name: str, runtime: str) -> Optional[PluginAdaptor]:
    """Build a PluginAdaptor from an adaptor module.

    Two conventions are supported so plugin authors can pick whichever fits:
    a class named ``Adaptor`` (zero-arg constructor or ``(plugin_name, runtime)``),
    or a factory function ``get_adaptor(plugin_name, runtime)``.
    """
    factory = getattr(module, "get_adaptor", None)
    if callable(factory):
        try:
            return factory(plugin_name, runtime)
        except Exception as exc:
            logger.warning("get_adaptor() failed for %s/%s: %s", plugin_name, runtime, exc)
            return None

    cls = getattr(module, "Adaptor", None)
    if cls is None:
        return None
    try:
        try:
            return cls(plugin_name, runtime)
        except TypeError:
            return cls()
    except Exception as exc:
        logger.warning("Adaptor() construction failed for %s/%s: %s", plugin_name, runtime, exc)
        return None


def _resolve_registry(plugin_name: str, runtime: str) -> Optional[PluginAdaptor]:
    path = _REGISTRY_ROOT / plugin_name / f"{runtime}.py"
    if not path.is_file():
        return None
    module = _load_module_from_path(f"plugins_registry.{plugin_name}.{runtime}", path)
    if module is None:
        return None
    return _instantiate(module, plugin_name, runtime)


def _resolve_plugin_shipped(plugin_root: Path, plugin_name: str, runtime: str) -> Optional[PluginAdaptor]:
    path = plugin_root / "adapters" / f"{runtime}.py"
    if not path.is_file():
        return None
    module = _load_module_from_path(f"_plugin_adaptor.{plugin_name}.{runtime}", path)
    if module is None:
        return None
    return _instantiate(module, plugin_name, runtime)


def resolve(
    plugin_name: str,
    runtime: str,
    plugin_root: Path,
) -> tuple[PluginAdaptor, str]:
    """Resolve the adaptor for ``(plugin_name, runtime)``.

    Returns ``(adaptor, source)`` where ``source`` is one of
    :class:`AdaptorSource` (``"registry"``, ``"plugin"``, ``"raw_drop"``).
    Always returns an adaptor — the raw-drop fallback ensures plugin installs
    never hard-fail on missing adaptors; instead the warning is surfaced via
    :class:`InstallResult.warnings`.
    """
    adaptor = _resolve_registry(plugin_name, runtime)
    if adaptor is not None:
        return adaptor, AdaptorSource.REGISTRY

    adaptor = _resolve_plugin_shipped(plugin_root, plugin_name, runtime)
    if adaptor is not None:
        return adaptor, AdaptorSource.PLUGIN

    adaptor = _resolve_mcp_default(plugin_root, plugin_name, runtime)
    if adaptor is not None:
        return adaptor, AdaptorSource.MCP_SERVER

    adaptor = _resolve_agentskills_default(plugin_root, plugin_name, runtime)
    if adaptor is not None:
        return adaptor, AdaptorSource.AGENTSKILLS

    return RawDropAdaptor(plugin_name, runtime), AdaptorSource.RAW_DROP


def _resolve_mcp_default(
    plugin_root: Path, plugin_name: str, runtime: str
) -> Optional[PluginAdaptor]:
    """Default MCP-shaped plugins to :class:`MCPServerAdaptor` (#3159).

    Because the MCP-wiring PORT made ``MCPServerAdaptor`` runtime-AGNOSTIC (it
    calls ``ctx.register_mcp_server`` and the active adapter renders the native
    config), an MCP plugin no longer needs a hand-written ``adapters/<runtime>.py``
    per runtime. A plugin is MCP-shaped when it ships an ``mcpServers`` descriptor
    (``mcp-servers.json`` or a ``settings-fragment.json`` carrying ``mcpServers``).

    This is what lets a CODEX concierge resolve ``MCPServerAdaptor`` for the
    ``molecule-platform-mcp`` plugin without that plugin shipping a ``codex.py``
    — and then the PORT renders ``~/.codex/config.toml`` instead of
    ``.claude/settings.json`` (the #3159 fix, end-to-end). Checked BEFORE the
    agentskills default so an MCP plugin that also ships skills/rules still gets
    the MCP wired (``MCPServerAdaptor`` delegates skills/rules to
    ``AgentskillsAdaptor`` internally).
    """
    try:
        if (plugin_root / "mcp-servers.json").is_file():
            shaped = True
        else:
            frag = plugin_root / "settings-fragment.json"
            shaped = False
            if frag.is_file():
                import json as _json
                try:
                    data = _json.loads(frag.read_text())
                except (OSError, ValueError):
                    data = None
                shaped = isinstance(data, dict) and isinstance(
                    data.get("mcpServers"), dict
                ) and bool(data["mcpServers"])
    except OSError:
        return None
    if not shaped:
        return None
    from .builtins import MCPServerAdaptor

    return MCPServerAdaptor(plugin_name, runtime)


def _resolve_agentskills_default(
    plugin_root: Path, plugin_name: str, runtime: str
) -> Optional[PluginAdaptor]:
    """Default skill-shaped plugins to :class:`AgentskillsAdaptor` (RFC#2843 #32).

    ``AgentskillsAdaptor`` is documented as "the default adapter for the skills
    shape", but ``resolve()`` only ever checked the name-registry and a
    plugin-shipped ``adapters/<runtime>.py``, then fell straight to RawDrop — so
    a skill plugin with no hand-written adapter (the common case, e.g. the
    seo-all skill) was dropped to ``/configs/plugins/<name>`` but NEVER activated
    into ``/configs/skills/`` where Claude Code reads. That left AgentskillsAdaptor
    effectively dead code and skill-plugins functionally uninstalled.

    Treat a plugin as agentskills-shaped when it carries a ``skills/`` subdir OR a
    root ``SKILL.md`` (the single-skill-at-root shape). AgentskillsAdaptor's
    install() handles both. Anything else still falls through to RawDrop.
    """
    try:
        has_skills_dir = (plugin_root / "skills").is_dir()
        has_root_skill = (plugin_root / "SKILL.md").is_file()
    except OSError:
        return None
    if not (has_skills_dir or has_root_skill):
        return None
    # Local import avoids a top-level cycle (builtins imports from .protocol only).
    from .builtins import AgentskillsAdaptor

    return AgentskillsAdaptor(plugin_name, runtime)
