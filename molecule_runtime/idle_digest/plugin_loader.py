"""Discover + load idle-digest providers contributed by installed plugins.

This is the runtime half of RFC molecule-core#4413 (digest-providers-as-native-
plugins). A plugin declares an in-process digest provider through the plugin-
manifest ``contributes.digestProviders`` surface (SDK plugin-manifest contract,
D0); this module reads those declarations off the already-loaded plugins,
imports each provider IN-PROCESS, trust-gates it, and hands the live
:class:`~molecule_runtime.idle_digest.provider.DigestProvider` objects back to
``build_default_providers``, which MERGES them into the roster by provider id
(``_merge_plugin_providers``): a contributed provider SUPERSEDES the built-in
one with the same id and is appended only when the id is new — appending
unconditionally would double every section, since the native digest plugins are
installed by default and contribute the ids the baked roster already builds. The
assembler sorts by the contribution's tier, so position does not affect render
order.

Design invariants (mirrors ``plugin_daemons`` + the idle-prompt failurePolicy):

* **Every declared provider loads — native or not. There is no env gate.**
  This platform is provider-agnostic: ``native`` means *the platform ships that
  capability built-in*, i.e. it is a capability-ORIGIN marker, not a trust
  boundary. A customer's own plugin is the customer's choice and must work by
  default, so ``MOLECULE_DIGEST_PROVIDER_PLUGINS`` — which gated discovery, then
  gated only third-party discovery — is REMOVED outright, with no vestigial kill
  switch. A stale value of it on a tenant is now simply inert.

  The removed gate's rationale ("a digest provider is live in-process Python in
  the wake path, therefore native-only") never held here: this runtime already
  executes third-party plugin code with no flag at all —
  ``molecule_runtime.plugin_daemons`` spawns manifest-declared subprocesses, and
  ``molecule_runtime.plugins_registry._instantiate`` imports plugin adaptor
  modules IN-PROCESS. In-process third-party execution was already the norm
  everywhere else, so singling out digest providers bought no isolation while
  blocking every customer-shipped provider.
* **Skip-not-reject.** Every failure — a malformed entry, a bad ``entrypoint``,
  an import error, a constructor error, a non-provider result — is logged and
  SKIPPED. Loading providers must never raise into the boot path; one broken
  plugin never takes the digest (or the workspace) down. This matters MORE now
  that any plugin's provider may load: isolation, not admission control, is what
  keeps a broken third-party provider from costing everyone the digest. The
  per-tick half lives in ``ProviderRunner`` (per-provider timeout + quarantine).
* **Name ownership survives: a non-native plugin may not claim a RESERVED
  provider id, nor self-grant ``official``.** This is the one thing ``native``
  still decides, and it is deliberately NOT code trust — it is impersonation
  defence. The reserved ids (``identity-capabilities``/``task-queue``/
  ``sent-folder``/``inbound-a2a``/``delegation-results``/``scheduler``/
  ``goal-state``) name PLATFORM-owned digest sections; a third-party provider
  claiming one would silently supersede the built-in section the agent relies
  on. ``official`` is refused for the same reason and no other: it is a
  self-declared class attribute whose ONLY consumer is the assembler's
  ``check_reserved_id``, so a forged ``official`` is precisely a forged licence
  to take a reserved id — and load time is the only place the forgery is
  catchable, since registration takes the attribute at face value.

  Refusal is PER-PROVIDER, not per-plugin: the offending contribution is
  skipped and the plugin's other providers still load. The plugin's module IS
  imported first (the check keys on the LOADED class, see below), which is
  correct now that importing third-party plugin code is no longer the thing
  being gated.

  The native set is injected (fail-safe default: empty → nothing may claim a
  reserved id). Its SSOT source is the **vendored native-plugins registry**
  (D2, landed — see ``contracts/PROVENANCE.md``); ``MOLECULE_NATIVE_PLUGIN_NAMES``
  can only EXTEND the registry set (union, never subtraction) for a self-host
  private native plugin or to control the set in a test.
* **``native`` is still surfaced as provenance.** It no longer gates loading,
  but every load prints ``(native=<bool>)`` so operators and the staging e2e can
  tell platform-shipped capability from customer-shipped capability in the logs.
* **Manifest is untrusted.** The id/ownership decision keys on the *loaded*
  object's ``provider_id``/``official``, never the manifest's self-declared
  values; a manifest whose declared ``provider_id`` disagrees with the loaded
  class is skipped.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .contract import RESERVED_PROVIDER_IDS
from .provider import DigestProvider

logger = logging.getLogger(__name__)

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
        # PUBLISH BEFORE EXEC. ``module_from_spec`` does NOT put the module in
        # ``sys.modules`` — the import system normally does that in a separate
        # step, and a by-path loader calling ``exec_module`` itself must do it
        # too. A module's own body may legitimately need to find itself at
        # ``sys.modules[__name__]``, and CPython's ``dataclasses`` does exactly
        # that with an UNGUARDED lookup while resolving *string* annotations
        # (``_is_type``: ``sys.modules.get(cls.__module__).__dict__``). Under
        # ``from __future__ import annotations`` every annotation is a string,
        # so any ``@dataclass`` in an unpublished module died with
        # ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
        # All four first-party digest plugins are that shape, so this made the
        # ENTIRE plugin-provider path a no-op in production (import-fail=5,
        # loaded=0) while the suite stayed green — its shims were thin wrappers
        # that declared no dataclass of their own. Same class of defect as
        # ``plugins_registry`` issue #296.
        #
        # The name is namespaced by plugin dir (above), so publishing cannot
        # collide across plugins; the restore below keeps this total even so.
        previous = sys.modules.get(mod_name)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # Never leave a half-initialised module behind: the next importer
            # (this loader, another plugin, or the module itself on retry)
            # would get a silently incomplete namespace instead of an error.
            if previous is not None:
                sys.modules[mod_name] = previous
            else:
                sys.modules.pop(mod_name, None)
            raise
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

    EVERY declared provider is loaded, whether or not its plugin is native —
    there is no env gate and no ``allow_third_party`` switch to re-impose one.
    ``native_plugin_names`` is still required, but it now decides only WHO MAY
    CLAIM a reserved provider id / the ``official`` marker (see
    :func:`_collect_from_plugin`), never whether a plugin's code runs.

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
                plugin, context, native_plugin_names, reserved_ids, out,
            )
        except Exception as exc:  # noqa: BLE001 — defense in depth; never raise per plugin
            logger.warning(
                "digest-provider: unexpected error loading from %s: %s",
                getattr(plugin, "name", "?"), exc,
            )
    if contributions_seen == 0:
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


def _collect_from_plugin(
    plugin, context, native_plugin_names, reserved_ids, out
) -> int:
    """Load every well-formed provider a single plugin contributes, appending to
    ``out``. Extracted so the top-level loop can guard each plugin.

    NATIVE STATUS DOES NOT GATE LOADING. ``native_plugin_names`` is consulted
    only for the id/ownership rule below (a non-native plugin may not claim a
    reserved id nor self-grant ``official``) and to label the provenance line.

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
                # NAME OWNERSHIP, NOT CODE TRUST. Loading is no longer gated on
                # native status — this refuses only the CLAIM: a reserved id
                # names a platform-owned digest section, and `official` is the
                # self-declared attribute that would license taking one (its
                # only consumer is the assembler's check_reserved_id, which
                # takes it at face value, so load time is the only place a
                # forged marker is catchable). PER-PROVIDER: `continue`, so the
                # plugin's other contributions still load.
                logger.warning(
                    "digest-provider: %s is not a platform-native plugin, so its provider "
                    "%r may not claim a reserved provider id or official=True — that ONE "
                    "contribution is refused (its siblings still load). Rename it to an id "
                    "outside %s and leave official unset.",
                    name, actual_id, sorted(reserved_ids),
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
