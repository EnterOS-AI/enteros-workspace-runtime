"""Per-install plugin settings — the smallest end-to-end slice.

WHAT THIS ANSWERS
-----------------
The plugin-config RFC has failed four review rounds on one question that no
amount of specification settled: **can a settings change reach a running daemon
without a restart, and who writes the file?** This module is the executable
answer to the first half, and it makes the second half observable.

THE SHAPE
---------
Core resolves a workspace's merged plugin settings and delivers ONE file per
plugin::

    /configs/plugin-settings/<plugin-name>.json

It lives OUTSIDE ``/configs/plugins/<name>/`` on purpose: the install pipeline
owns that directory and re-stages it (EIC does a literal ``rm -rf``), so
settings written there are destroyed on the next reconcile.

A plugin declares what it accepts in its own manifest::

    contributes:
      configuration:
        properties:
          timezone: { type: string, default: UTC }

and consumes it either by reading ``MOLECULE_PLUGIN_CONFIG_FILE``, or — the
zero-code-change path — by interpolating into its own daemon/MCP ``env`` map::

    contributes:
      daemons:
        - name: scheduler
          command: python
          args: [scheduler.py]
          env:
            SCHEDULER_TZ: "${config:timezone}"

FAILURE POLICY (decided, and enforced here)
-------------------------------------------
**Drop bad keys, keep the plugin.** A value that is absent, unreadable, or not
declared falls back to the declared ``default``; an unresolvable reference with
no default resolves to empty. The plugin ALWAYS loads. This mirrors the
``daemons``/``digestProviders`` skip-and-degrade posture and is the same
blast-radius argument that made ``contributes.configuration`` an open schema:
a settings value must never be able to delete a plugin's skills, rules and
tools.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
The WRITER. Nothing in this module creates or updates the settings file — that
is core's provisioning/delivery leg, and the RFC's open question is precisely
which core path writes it post-provision. This module makes the answer
observable: change the file, re-run discovery, and the daemon spec changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Where core delivers resolved settings. One file per plugin, keyed by the
#: plugin's INSTALL name (the ``/configs/plugins/<name>/`` directory key).
PLUGIN_SETTINGS_DIRNAME = "plugin-settings"

#: Env var handed to daemons and MCP servers so a plugin can read its own
#: settings directly rather than via ``${config:}`` interpolation.
PLUGIN_CONFIG_FILE_ENV = "MOLECULE_PLUGIN_CONFIG_FILE"

#: ``${config:key}`` — the interpolation form usable inside a manifest's own
#: ``env`` map. Deliberately narrow: a bare key, no nesting, no defaults inline.
_CONFIG_REF = re.compile(r"\$\{config:([A-Za-z0-9_.-]+)\}")

#: A settings file larger than this is refused rather than parsed — the same
#: hostile-input posture the template config loader takes.
MAX_SETTINGS_BYTES = 256 * 1024


def settings_dir(configs_dir: str | None = None) -> str:
    """Directory core delivers plugin settings into."""
    base = configs_dir or os.environ.get("CONFIGS_DIR", "/configs")
    return os.path.join(base, PLUGIN_SETTINGS_DIRNAME)


def settings_path(plugin_name: str, configs_dir: str | None = None) -> str:
    """Path to one plugin's delivered settings file."""
    return os.path.join(settings_dir(configs_dir), f"{plugin_name}.json")


def declared_defaults(manifest_contributes: dict) -> dict[str, Any]:
    """Extract ``contributes.configuration.properties.*.default``.

    This is precedence LAYER 1. It lives in the plugin manifest, on the box —
    which is exactly why the runtime can supply it and core (which has no
    manifest at provision time) cannot.

    Tolerant by construction: the declaration is an OPEN schema, so anything
    malformed yields no defaults rather than an error.
    """
    if not isinstance(manifest_contributes, dict):
        return {}
    config = manifest_contributes.get("configuration")
    if not isinstance(config, dict):
        return {}
    props = config.get("properties")
    if not isinstance(props, dict):
        return {}
    out: dict[str, Any] = {}
    for key, decl in props.items():
        if isinstance(decl, dict) and "default" in decl:
            out[str(key)] = decl["default"]
    return out


def load_delivered(plugin_name: str, configs_dir: str | None = None) -> dict[str, Any]:
    """Read one plugin's delivered settings. Absent/unreadable/malformed -> {}.

    Never raises. A broken settings file must degrade to declared defaults, not
    prevent the plugin from loading.
    """
    path = settings_path(plugin_name, configs_dir)
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    if size > MAX_SETTINGS_BYTES:
        logger.warning(
            "plugin %s: settings file %s is %d bytes (cap %d) — ignoring",
            plugin_name, path, size, MAX_SETTINGS_BYTES,
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the plugin
        logger.warning("plugin %s: unreadable settings %s: %s — using defaults", plugin_name, path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "plugin %s: settings %s is %s, expected an object — using defaults",
            plugin_name, path, type(data).__name__,
        )
        return {}
    return data


def has_settings(plugin_name: str, manifest_contributes: dict, configs_dir: str | None = None) -> bool:
    """True when this plugin declares configuration or has a delivered file.

    Guards the opt-in: a plugin with neither must keep a byte-identical daemon
    environment. Injecting MOLECULE_PLUGIN_CONFIG_FILE into every daemon on the
    box would be a behaviour change for every existing plugin, in exchange for
    nothing — caught by test_discover_reads_contributes_daemons on the first run.
    """
    if declared_defaults(manifest_contributes):
        return True
    config = (manifest_contributes or {}).get("configuration")
    if isinstance(config, dict) and isinstance(config.get("properties"), dict):
        return True
    return os.path.exists(settings_path(plugin_name, configs_dir))


def resolve(plugin_name: str, manifest_contributes: dict, configs_dir: str | None = None) -> dict[str, Any]:
    """Effective settings = declared defaults (layer 1) <- delivered (layers 2-5).

    Shallow per-key override, matching the RFC's merge rule. An undeclared
    delivered key is KEPT with a warning rather than dropped: core is the side
    that validates against the declaration, and silently discarding here would
    make the two sides disagree about what shipped.
    """
    effective = declared_defaults(manifest_contributes)
    delivered = load_delivered(plugin_name, configs_dir)
    for key, value in delivered.items():
        if key not in effective:
            logger.warning(
                "plugin %s: delivered setting %r is not declared in contributes.configuration "
                "— keeping it, but the plugin will not know about it",
                plugin_name, key,
            )
        effective[key] = value
    return effective


def interpolate(value: str, settings: dict[str, Any]) -> str:
    """Substitute ``${config:key}`` from resolved settings.

    An unresolvable reference becomes the empty string, with a warning — the
    drop-bad-keys policy. Returning the literal ``${config:foo}`` would ship a
    nonsense value into a daemon's environment, which is worse.
    """
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in settings:
            logger.warning(
                "unresolved ${config:%s} — no delivered value and no declared default; using empty string",
                key,
            )
            return ""
        return str(settings[key])

    return _CONFIG_REF.sub(_sub, value)


def apply_to_env(env: dict[str, str], settings: dict[str, Any], settings_file: str | None = None) -> dict[str, str]:
    """Interpolate every value in a manifest ``env`` map, and add the file path.

    This is the zero-code-change adoption lever: an existing plugin that already
    reads an env var keeps working, and simply sources that var from settings.
    """
    out = {k: interpolate(str(v), settings) for k, v in (env or {}).items()}
    if settings_file:
        out.setdefault(PLUGIN_CONFIG_FILE_ENV, settings_file)
    return out
