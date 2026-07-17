"""Discover + load idle-digest providers contributed by installed plugins.

This is the runtime half of RFC molecule-core#4413 (digest-providers-as-native-
plugins). A plugin declares an in-process digest provider through the plugin-
manifest ``contributes.digestProviders`` surface (SDK plugin-manifest contract,
D0); this module reads those declarations off the already-loaded plugins,
imports each provider IN-PROCESS, trust-gates it, and hands the live
:class:`~molecule_runtime.idle_digest.provider.DigestProvider` objects back to
``build_default_providers`` to append to the roster. The assembler sorts by the
contribution's tier, so append order does not affect render order.

Design invariants (mirrors ``plugin_daemons`` + the idle-prompt failurePolicy):

* **Skip-not-reject.** Every failure — a malformed entry, a bad ``entrypoint``,
  an import error, a constructor error, a non-provider result — is logged and
  SKIPPED. Loading providers must never raise into the boot path; one broken
  plugin never takes the digest (or the workspace) down.
* **Load-time trust gate (extends the ``official`` marker to load time).** The
  assembler already refuses at *contribution* time a reserved ``provider_id``
  claimed by a non-``official`` provider (``check_reserved_id``). Here we add the
  *load*-time half the RFC calls for: a provider whose loaded class asserts
  ``official = True`` OR claims a reserved id may only be loaded from a
  **native** plugin (one delivered through the native-plugins registry). A
  third-party plugin shipping such a class is refused at load — it can never get
  its official/reserved code running in-process. The native set is injected
  (fail-safe default: empty → nothing official loads), sourced today from
  ``MOLECULE_NATIVE_PLUGIN_NAMES``; D2 wires the vendored native-plugins
  registry as the SSOT source.
* **Flag-gated (D1).** Off by default (``MOLECULE_DIGEST_PROVIDER_PLUGINS``);
  flag-off is byte-identical to the hardcoded roster.
* **Manifest is untrusted.** The trust decision keys on the *loaded* object's
  ``provider_id``/``official``, never the manifest's self-declared values; a
  manifest whose declared ``provider_id`` disagrees with the loaded class is
  skipped.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .contract import RESERVED_PROVIDER_IDS
from .provider import DigestProvider

logger = logging.getLogger(__name__)

FLAG_ENV = "MOLECULE_DIGEST_PROVIDER_PLUGINS"
NATIVE_NAMES_ENV = "MOLECULE_NATIVE_PLUGIN_NAMES"

# The vendored native-plugins registry (SSOT mirror of molecule-ai-sdk
# contracts/plugin/native-plugins.registry.json — see contracts/PROVENANCE.md +
# the check-schemas-in-sync.sh drift gate). It is the SSOT for which plugins are
# NATIVE (platform-delivered first-party), and thus the trust source below.
NATIVE_REGISTRY_RESOURCE = "contracts/native-plugins.registry.json"


@dataclass(frozen=True)
class DigestProviderContext:
    """The runtime-injected seams a loaded provider may need at construction.

    The built-in providers take specific kwargs; a plugin-shipped provider gets
    this uniform context instead (its entrypoint is called ``entry(context)``,
    falling back to a zero-arg ``entry()`` for providers that need nothing). The
    fields mirror exactly what ``build_default_providers`` already has in scope,
    so no new state is threaded through boot.
    """

    config_path: str = ""
    prompt_files: tuple[str, ...] = ()
    workspace_name: str = "this workspace"
    runtime_kind: str = "claude-code"
    comms_source: object = None
    platform_url: str = ""
    workspace_id: str = ""


def digest_provider_plugins_enabled() -> bool:
    """D1 flag. Off (byte-identical hardcoded roster) unless explicitly enabled."""
    return os.environ.get(FLAG_ENV, "0").strip().lower() not in ("", "0", "false", "no")


def native_plugin_names_from_env() -> frozenset[str]:
    """Operator escape-hatch for the native allow-list (``MOLECULE_NATIVE_PLUGIN_NAMES``).

    No longer the primary source (that is the vendored registry below); it can
    only EXTEND the trusted set — for a self-host private native plugin, or to
    control the set in a test. Fail-safe default: EMPTY.
    """
    raw = os.environ.get(NATIVE_NAMES_ENV, "")
    return frozenset(n.strip() for n in raw.split(",") if n.strip())


def native_plugin_names_from_registry() -> frozenset[str]:
    """Native allow-list from the vendored native-plugins registry (the SSOT).

    The trust gate matches a *loaded* plugin's install-dir basename
    (``LoadedPlugin.name``) against this set. That basename is the plugin
    SOURCE's on-disk name (repo segment / subpath tail), which is NOT always the
    registry entry's ``name`` field — e.g. the scheduler's registry name is
    ``molecule-scheduler`` but it installs from repo ``molecule-ai-plugin-scheduler``.
    So each native name is derived by parsing the registry entry's ``source``
    with the runtime's OWN source parser (``parse_declared_plugins``), the exact
    logic that names an installed plugin's directory — guaranteeing the set
    matches what ``load_plugins`` calls a plugin.

    Fail-safe EMPTY on any read/parse error: a registry we cannot read trusts
    nothing, so no ``official``/reserved provider loads (same fail-safe direction
    as the env fallback). Read offline from the wheel via ``importlib.resources``.
    """
    try:
        from importlib import resources

        raw = (
            resources.files("molecule_runtime")
            .joinpath(NATIVE_REGISTRY_RESOURCE)
            .read_text(encoding="utf-8")
        )
        plugins = json.loads(raw).get("plugins", {})
        sources = [
            (entry or {}).get("source", "")
            for entry in plugins.values()
            if isinstance(entry, dict)
        ]
    except Exception as exc:  # noqa: BLE001 — an unreadable registry trusts nothing
        logger.warning(
            "digest-provider: could not read vendored native-plugins registry "
            "(%s); native trust set is EMPTY (no official/reserved provider will load)",
            exc,
        )
        return frozenset()

    try:
        from ..plugin_sources import parse_declared_plugins

        joined = ",".join(s for s in sources if isinstance(s, str) and s.strip())
        return frozenset(p.name for p in parse_declared_plugins(joined) if p.name)
    except Exception as exc:  # noqa: BLE001 — parse failure must not crash the digest
        logger.warning("digest-provider: could not parse native-plugins registry sources: %s", exc)
        return frozenset()


def native_plugin_names() -> frozenset[str]:
    """The effective native allow-list for the load-time trust gate.

    The vendored registry is the SSOT source; ``MOLECULE_NATIVE_PLUGIN_NAMES``
    only EXTENDS it (operator escape-hatch, same operator trust). Union, never
    subtraction — the env can add a self-host native plugin but can never
    un-trust a registry one.
    """
    return native_plugin_names_from_registry() | native_plugin_names_from_env()


def _entry_problem(entry: object) -> Optional[str]:
    """Return a human reason the entry is malformed, or None if well-formed.

    Mirrors ``plugin_daemons._entry_problem``: a mapping with non-blank string
    ``provider_id`` and ``entrypoint``. Everything else is skip-with-warning.
    """
    if not isinstance(entry, dict):
        return "not a mapping"
    for key in ("provider_id", "entrypoint"):
        val = entry.get(key)
        if not isinstance(val, str) or not val.strip():
            return f"missing/blank {key!r}"
    return None


def _resolve_entrypoint(plugin_root: Path, entrypoint: str):
    """Import ``module:Attr`` from within the plugin dir and return the attr.

    ``module`` is a dotted path resolved to a file under the plugin dir
    (``pkg.mod`` -> ``<plugin>/pkg/mod.py``). Returns None on any failure.
    """
    mod_path, sep, attr = entrypoint.partition(":")
    if not sep or not mod_path.strip() or not attr.strip():
        logger.warning("digest-provider: malformed entrypoint %r (want 'module:Attr')", entrypoint)
        return None
    parts = mod_path.strip().split(".")
    # Reject path-escaping components (``..``, empty, or embedded separators) so a
    # crafted entrypoint can never resolve a module OUTSIDE the plugin dir. The
    # trust gate does not depend on this (is_native stays keyed on the loading
    # plugin's dir), but the loader must not import arbitrary host files.
    if any((not p) or p in (".", "..") or "/" in p or "\\" in p or os.sep in p for p in parts):
        logger.warning("digest-provider: entrypoint module %r has an illegal component", mod_path)
        return None
    file = (plugin_root / Path(*parts)).with_suffix(".py")
    try:
        file.resolve().relative_to(plugin_root.resolve())
    except ValueError:
        logger.warning("digest-provider: entrypoint module %s escapes the plugin dir %s", mod_path, plugin_root)
        return None
    if not file.is_file():
        logger.warning("digest-provider: entrypoint module %s not found at %s", mod_path, file)
        return None
    # Namespace the synthetic module name by plugin dir so two plugins shipping
    # the same module path never collide in sys.modules.
    mod_name = f"molecule_digest_plugin.{plugin_root.name}.{mod_path.strip()}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, file)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — any import-time failure is skip-not-crash
        logger.warning("digest-provider: failed importing %s from %s: %s", entrypoint, file, exc)
        return None
    obj = getattr(module, attr.strip(), None)
    if obj is None:
        logger.warning("digest-provider: %s has no attribute %s", mod_path, attr)
    return obj


def _instantiate(entry_obj, context: DigestProviderContext):
    """Build a provider from the resolved entrypoint object.

    Convention (mirrors plugins_registry._instantiate): call it with the context,
    falling back to a zero-arg call for providers that need nothing.
    """
    if not callable(entry_obj):
        logger.warning("digest-provider: entrypoint object %r is not callable", entry_obj)
        return None
    try:
        try:
            return entry_obj(context)
        except TypeError:
            return entry_obj()
    except Exception as exc:  # noqa: BLE001
        logger.warning("digest-provider: constructing provider failed: %s", exc)
        return None


def _is_digest_provider(obj) -> bool:
    return isinstance(getattr(obj, "provider_id", None), str) and callable(getattr(obj, "contribute", None))


def load_digest_provider_plugins(
    loaded_plugins,
    context: DigestProviderContext,
    *,
    native_plugin_names: frozenset[str],
    reserved_ids: frozenset[str] = RESERVED_PROVIDER_IDS,
) -> list[DigestProvider]:
    """Return the live digest providers contributed by installed plugins.

    Never raises. ``loaded_plugins`` is a
    :class:`~molecule_runtime.plugins.LoadedPlugins`; each plugin's
    ``manifest.contributes['digestProviders']`` is read defensively.
    """
    out: list[DigestProvider] = []
    try:
        plugins = list(getattr(loaded_plugins, "plugins", None) or [])
    except Exception as exc:  # noqa: BLE001 — a broken LoadedPlugins must not crash boot
        logger.warning("digest-provider: cannot read plugins list: %s", exc)
        return out
    contributions_seen = 0
    for plugin in plugins:
        try:
            contributions_seen += _collect_from_plugin(
                plugin, context, native_plugin_names, reserved_ids, out
            )
        except Exception as exc:  # noqa: BLE001 — defense in depth; never raise per plugin
            logger.warning(
                "digest-provider: unexpected error loading from %s: %s",
                getattr(plugin, "name", "?"), exc,
            )
    if contributions_seen == 0 and digest_provider_plugins_enabled():
        # GATE-PROBED EVIDENCE (stdout via print, same reason as the loaded
        # line): makes "the loader ran and found nothing" distinguishable in
        # docker logs from "the loader was never invoked". Refused/malformed
        # contributions do NOT count as nothing — their warnings are the
        # evidence for those paths.
        print(
            f"digest-provider: scan complete — no digestProviders contributions "
            f"among {len(plugins)} installed plugin(s)",
            flush=True,
        )
    return out


def _collect_from_plugin(plugin, context, native_plugin_names, reserved_ids, out) -> int:
    """Load every well-formed, trust-passing provider a single plugin contributes,
    appending to ``out``. Extracted so the top-level loop can guard each plugin.

    Returns the number of ``digestProviders`` contributions the plugin DECLARED
    (whether or not they loaded), so the caller can tell a scan that found
    nothing from one that refused/skipped what it found."""
    name = getattr(plugin, "name", "") or ""
    contributes = getattr(getattr(plugin, "manifest", None), "contributes", None) or {}
    entries = contributes.get("digestProviders")
    if entries is None:
        return 0
    if not isinstance(entries, list):
        logger.warning("digest-provider: %s contributes.digestProviders is not a list — skipped", name)
        return 1  # a (malformed) contribution surface was declared — not "nothing found"
    plugin_root = Path(getattr(plugin, "path", "") or ".")
    is_native = name in native_plugin_names
    for entry in entries:
        # Guard each entry so one raising entry never skips a plugin's siblings.
        try:
            problem = _entry_problem(entry)
            if problem is not None:
                logger.warning("digest-provider: %s skipping malformed entry (%s)", name, problem)
                continue
            declared_id = entry["provider_id"].strip()
            provider = _instantiate(_resolve_entrypoint(plugin_root, entry["entrypoint"].strip()), context)
            if provider is None:
                continue
            if not _is_digest_provider(provider):
                logger.warning("digest-provider: %s entrypoint did not yield a DigestProvider — skipped", name)
                continue
            actual_id = provider.provider_id
            if actual_id != declared_id:
                # The manifest is untrusted: a class shipping a different id than the
                # manifest declares is a red flag (e.g. declaring a benign id while
                # the class claims a reserved one). Refuse it.
                logger.warning(
                    "digest-provider: %s manifest declared %r but class is %r — skipped",
                    name, declared_id, actual_id,
                )
                continue
            official = bool(getattr(provider, "official", False))
            if (official or actual_id in reserved_ids) and not is_native:
                logger.warning(
                    "digest-provider: %s is not a native plugin but its provider %r is "
                    "official/reserved — refused (load-time trust gate)",
                    name, actual_id,
                )
                continue
            # GATE-PROBED EVIDENCE — the staging e2e (sub-step 10e) greps docker
            # logs for "from plugin <name> (native=True)". It must go to stdout
            # via print(), not logging: the workspace process never configures
            # Python logging, so logger.info is dropped by the unconfigured root
            # logger and could never reach docker logs.
            print(
                f"digest-provider: loaded {actual_id!r} from plugin {name} (native={is_native})",
                flush=True,
            )
            out.append(provider)
        except Exception as exc:  # noqa: BLE001 — one bad entry must not skip its siblings
            logger.warning("digest-provider: %s error on an entry, skipping it: %s", name, exc)
            continue
    return len(entries)
