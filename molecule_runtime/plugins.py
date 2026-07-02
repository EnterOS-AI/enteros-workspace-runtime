"""Plugin system for loading per-workspace and shared plugins.

Plugins provide skills, rules, and prompt fragments to agent workspaces.
Each plugin is a directory containing:
  - plugin.yaml    — manifest (name, version, description, skills, rules)
  - rules/*.md     — always-on guidelines injected into every prompt
  - skills/        — skill directories with SKILL.md + tools/*.py
  - *.md           — prompt fragments (excluding README, CHANGELOG, etc.)

Loading priority:
  1. Per-workspace: /configs/plugins/<name>/  (installed via API)
  2. Shared fallback: /plugins/<name>/        (legacy bind mount)
  Deduplication by name — per-workspace wins.
"""

import logging
import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml

from molecule_runtime import manifest_ssot

logger = logging.getLogger(__name__)

WORKSPACE_PLUGINS_DIR = "/configs/plugins"
SHARED_PLUGINS_DIR = os.environ.get("PLUGINS_DIR", "/plugins")


@dataclass
class PluginManifest:
    name: str = ""
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    prompt_fragments: list[str] = field(default_factory=list)
    adapters: dict = field(default_factory=dict)
    runtimes: list[str] = field(default_factory=list)  # declared supported runtimes


@dataclass
class Plugin:
    name: str
    path: str
    manifest: PluginManifest = field(default_factory=PluginManifest)
    rules: list[str] = field(default_factory=list)  # rule content strings
    prompt_fragments: list[str] = field(default_factory=list)  # extra prompt content
    skills_dir: str = ""  # path to skills/ inside plugin


@dataclass
class LoadedPlugins:
    rules: list[str] = field(default_factory=list)
    prompt_fragments: list[str] = field(default_factory=list)
    skill_dirs: list[str] = field(default_factory=list)  # dirs to scan for extra skills
    plugin_names: list[str] = field(default_factory=list)
    plugins: list[Plugin] = field(default_factory=list)


def load_plugin_manifest(plugin_path: str) -> PluginManifest | None:
    """Parse plugin.yaml from a plugin directory.

    Returns an empty (name-only) manifest when plugin.yaml is MISSING —
    manifest-less plugins (bare-SKILL.md dirs) are common and legal, so that
    stays advisory-only. Returns ``None`` when an EXISTING plugin.yaml
    violates the SSOT schema (or is unparseable) AND enforcement is on
    (molecule-core#3383 PR-4, default ON, kill-switch
    ``MOLECULE_MANIFEST_SSOT_ENFORCE=off``) — the caller must skip the plugin.
    With enforcement off, the pre-existing advisory/degrade behaviour is
    unchanged.
    """
    manifest_file = os.path.join(plugin_path, "plugin.yaml")
    if not os.path.isfile(manifest_file):
        return PluginManifest(name=os.path.basename(plugin_path))
    try:
        with open(manifest_file) as f:
            raw = yaml.safe_load(f) or {}
        # molecule-core#3383 plugin-manifest SSOT gate: validate the parsed
        # manifest against the vendored SSOT schema. validate_manifest_ssot
        # returns a list (never raises); the advisory warning always fires,
        # and under enforcement a violating EXISTING manifest refuses to load.
        violations = manifest_ssot.validate_manifest_ssot(raw)
        if violations:
            logger.warning(
                "SSOT manifest validation (advisory): plugin manifest %s: %d violation(s): %s",
                manifest_file, len(violations), "; ".join(violations),
            )
            if manifest_ssot.enforcement_enabled():
                logger.error(
                    "SSOT manifest ENFORCEMENT: refusing to load plugin %s: %d violation(s): %s",
                    os.path.basename(plugin_path), len(violations), "; ".join(violations),
                )
                return None
        return PluginManifest(
            name=raw.get("name", os.path.basename(plugin_path)),
            version=raw.get("version", "0.0.0"),
            description=raw.get("description", ""),
            author=raw.get("author", ""),
            tags=raw.get("tags", []),
            skills=raw.get("skills", []),
            rules=raw.get("rules", []),
            prompt_fragments=raw.get("prompt_fragments", []),
            adapters=raw.get("adapters", {}),
            runtimes=raw.get("runtimes", []),
        )
    except Exception as e:
        # An EXISTING but unreadable/unparseable plugin.yaml IS an invalid
        # manifest document: fail-closed under enforcement. With enforcement
        # off, the pre-existing degrade path (warn + name-only manifest) is
        # byte-identical.
        if manifest_ssot.enforcement_enabled():
            logger.error(
                "SSOT manifest ENFORCEMENT: refusing to load plugin %s: invalid manifest %s: %s",
                os.path.basename(plugin_path), manifest_file, e,
            )
            return None
        logger.warning("Failed to parse plugin manifest %s: %s", manifest_file, e)
        return PluginManifest(name=os.path.basename(plugin_path))


def _load_single_plugin(plugin_path: str) -> Plugin | None:
    """Load a single plugin from a directory.

    Returns ``None`` (plugin skipped) when SSOT manifest enforcement refused
    the manifest — see ``load_plugin_manifest``.
    """
    name = os.path.basename(plugin_path)
    manifest = load_plugin_manifest(plugin_path)
    if manifest is None:
        return None
    plugin = Plugin(name=name, path=plugin_path, manifest=manifest)

    # Load rules
    rules_dir = os.path.join(plugin_path, "rules")
    if os.path.isdir(rules_dir):
        for rule_file in sorted(os.listdir(rules_dir)):
            if rule_file.endswith(".md"):
                content = Path(os.path.join(rules_dir, rule_file)).read_text().strip()
                if content:
                    plugin.rules.append(content)
                    logger.info("Plugin %s: loaded rule %s", name, rule_file)

    # Load prompt fragments (any .md in root of plugin)
    skip = {"readme.md", "changelog.md", "license.md", "contributing.md", "plugin.yaml"}
    for f in sorted(os.listdir(plugin_path)):
        if f.endswith(".md") and f.lower() not in skip and os.path.isfile(os.path.join(plugin_path, f)):
            content = Path(os.path.join(plugin_path, f)).read_text().strip()
            if content:
                plugin.prompt_fragments.append(content)
                logger.info("Plugin %s: loaded prompt fragment %s", name, f)

    # Register skills directory
    skills_dir = os.path.join(plugin_path, "skills")
    if os.path.isdir(skills_dir):
        plugin.skills_dir = skills_dir
        skill_count = len([d for d in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, d))])
        logger.info("Plugin %s: found %d skills", name, skill_count)

    return plugin


def load_plugins(
    workspace_plugins_dir: str | None = None,
    shared_plugins_dir: str | None = None,
) -> LoadedPlugins:
    """Scan per-workspace plugins first, then shared plugins. Deduplicate by name."""
    ws_dir = workspace_plugins_dir or WORKSPACE_PLUGINS_DIR
    shared_dir = shared_plugins_dir or SHARED_PLUGINS_DIR
    result = LoadedPlugins()
    seen_names: set[str] = set()

    # Scan both dirs: per-workspace first (higher priority)
    for base_dir in [ws_dir, shared_dir]:
        if not os.path.isdir(base_dir):
            continue
        for entry in sorted(os.listdir(base_dir)):
            plugin_path = os.path.join(base_dir, entry)
            if not os.path.isdir(plugin_path) or entry in seen_names:
                continue

            plugin = _load_single_plugin(plugin_path)
            seen_names.add(entry)
            if plugin is None:
                # SSOT manifest enforcement refused it (already logged). The
                # name stays in seen_names: a rejected per-workspace plugin
                # must not silently fall back to a same-named shared one.
                continue

            result.rules.extend(plugin.rules)
            result.prompt_fragments.extend(plugin.prompt_fragments)
            if plugin.skills_dir:
                result.skill_dirs.append(plugin.skills_dir)
            result.plugin_names.append(entry)
            result.plugins.append(plugin)

    if result.plugin_names:
        logger.info("Loaded %d plugins: %s", len(result.plugin_names), ", ".join(result.plugin_names))

    return result
