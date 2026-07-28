"""Durable per-plugin state directory — the runtime leg of the plugin-state contract.

A plugin that must remember something across a container swap had no word for it
in its manifest, so it had to GUESS a path — and every guess made so far was
wrong.  ``gmail-channel-molecule`` guessed ``~/.gmail-channel`` (the ephemeral
root layer), then ``/workspace/.gmail-channel`` (measured ALSO wiped, because
local-docker's restart path destroys and re-seeds the ``/workspace`` volume), and
its Gmail cursor re-pinned to ``now()`` on every restart — silently swallowing
every older message.  Four daemon plugins reached four incompatible answers to
one question, which is what makes this a contract gap rather than four bugs
(molecule-ai-workspace-runtime#360 defect A, RFC molecule-ai-sdk#181).

This module resolves the ONE directory a plugin gets, and
:mod:`molecule_runtime.plugin_daemons` injects it into every daemon subprocess.

NOT A NEW MECHANISM.  This is :mod:`molecule_runtime.trigger_state` generalised:
that module already resolves a per-plugin-class state dir and the runtime already
injects it into a daemon subprocess.  Three things change — it becomes
kind-agnostic (any plugin declaring ``contributes.state``, not only
``kind: trigger``), plugin-keyed rather than lane-keyed, and rooted on a DURABLE
root instead of ``/configs``, which the local-docker teardown path destroys and
re-seeds on every restart.  ``trigger_state`` itself is deliberately untouched;
re-rooting the scheduler grid onto this seam is tracked in #370.

NO LITERALS LIVE HERE.  Every env-var name, root, template and mode is read from
the vendored ``plugin-state.contract.json`` SSOT — the same way
:mod:`molecule_runtime.mailbox_dir` reads ``workspace-data.contract.json``.  The
contract ``const``-pins them so its three consumers (the controlplane
provisioner, this runtime, and the plugin itself) read ONE constant instead of
three hand-typed copies of a path.  #360 produced three separate "fixed in one
place, not the other" regressions in a single day; its own closing note named the
remedy, and this module is built to it.

TWO INVARIANTS THAT ARE EASY TO GET BACKWARDS
---------------------------------------------
**Durability is DECLARED by the provisioner, and the probe may only DOWNGRADE.**
``mailbox_dir.probe_durability`` answers *"is this a distinct mount"*, which is
NOT the same question.  On local-docker ``/workspace`` **is** a distinct named
volume and probes ``DURABLE`` — yet ``workspaceTeardownVolumes`` removes it on
every restart.  That false positive is exactly how defect A stayed invisible for
a day.  Only the provisioner knows which of its own mounts its teardown path
preserves, so it declares durability by injecting the state root, and the probe
may only take that claim away (unwritable / ephemeral root disk), never grant it.

**Degrade LOUDLY, never fail closed.**  Refusing to resolve a directory would,
today, take every channel plugin on every local-docker box and every EC2
workspace with ``dataVolumeGB == 0`` from lossy-but-delivering to not running at
all.  A Gmail bridge that loses its cursor still delivers today's mail; one the
platform refuses to start delivers nothing.  The defect in #360 was never
degradation — it was SILENT degradation.  So this module never raises: it always
returns a writable directory and an HONEST durability flag the daemon can branch
on.

Deliberately import-light (stdlib only, with the ``mailbox_dir`` probe imported
lazily) so :mod:`molecule_runtime.plugin_daemons` can import it on the spawn path
without dragging heavier dependencies into it.
"""

from __future__ import annotations

import json
import logging
import os
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

#: Vendored plugin-state contract SSOT (byte-for-byte mirror of molecule-ai-sdk
#: contracts/plugin-state; see molecule_runtime/contracts/PROVENANCE.md).
_CONTRACT_RESOURCE = "contracts/plugin-state.contract.json"

_contract_cache: dict | None = None


def _contract() -> dict:
    """Load the vendored plugin-state contract instance (cached).

    Raises on a missing/corrupt mirror.  That is deliberate: unlike
    ``mailbox_dir``'s optional snapshot signal, EVERY name this module needs
    lives in the contract, so an unreadable contract is a broken wheel rather
    than a runtime condition.  The alternative — hardcoded fallback literals —
    was rejected because it recreates the exact multi-copy divergence the
    contract exists to prevent.  Callers degrade; see :data:`CONTRACT_AVAILABLE`.
    """
    global _contract_cache
    if _contract_cache is None:
        raw = (
            resources.files("molecule_runtime")
            .joinpath(_CONTRACT_RESOURCE)
            .read_text(encoding="utf-8")
        )
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError(f"{_CONTRACT_RESOURCE} is not a JSON object")
        _contract_cache = loaded
    return _contract_cache


def _load_constants() -> tuple[bool, dict]:
    """Pull every literal out of the contract. ``(available, values)``."""
    try:
        c = _contract()
        daemon_env = c["daemon_env"]
        box_env = c["box_env"]
        paths = c["paths"]
        values = {
            "STATE_DIR_ENV": str(daemon_env["state_dir"]),
            "DURABLE_ENV": str(daemon_env["durable"]),
            "DURABLE_TRUE": str(daemon_env["durable_true"]),
            "DURABLE_FALSE": str(daemon_env["durable_false"]),
            "STATE_ROOT_ENV": str(box_env["state_root"]),
            "DEFAULT_ROOT": str(paths["default_root"]),
            "FALLBACK_ROOT": str(paths["fallback_root"]),
            "DIR_TEMPLATE": str(paths["dir_template"]),
            "DIR_MODE": int(str(paths["mode"]), 8),
        }
        return True, values
    except Exception as e:  # noqa: BLE001 — a broken mirror must not break boot
        logger.error(
            "plugin-state contract unavailable (%s): per-plugin state dirs will "
            "NOT be injected; plugins keep their own paths as on an older runtime",
            e,
        )
        return False, {}


CONTRACT_AVAILABLE, _V = _load_constants()

#: runtime -> daemon, RESERVED. The ALREADY-NAMESPACED per-plugin directory —
#: NOT a root the plugin appends its own name to. Letting the plugin build the
#: subdirectory would hand directory identity back to the untrusted side, which
#: is the defect being closed.
STATE_DIR_ENV: str | None = _V.get("STATE_DIR_ENV")
#: runtime -> daemon, RESERVED. "1"/"0" — the honest durability flag. This is the
#: machine-readable half of "degrade loudly": the plugin branches on it (bounded
#: lookback + id-dedup instead of pinning a cursor to now()).
DURABLE_ENV: str | None = _V.get("DURABLE_ENV")
DURABLE_TRUE: str | None = _V.get("DURABLE_TRUE")
DURABLE_FALSE: str | None = _V.get("DURABLE_FALSE")
#: provisioner -> box. Its PRESENCE is the provisioner's declaration that this
#: root survives its own teardown path. Set only when the bind actually happened.
STATE_ROOT_ENV: str | None = _V.get("STATE_ROOT_ENV")
#: The root the provisioner is expected to supply (recorded for the pin test).
DEFAULT_ROOT: str | None = _V.get("DEFAULT_ROOT")
#: Where state goes when no root was declared. Always reported NOT durable.
FALLBACK_ROOT: str | None = _V.get("FALLBACK_ROOT")
DIR_TEMPLATE: str | None = _V.get("DIR_TEMPLATE")
DIR_MODE: int | None = _V.get("DIR_MODE")


def reserved_env_names() -> tuple[str, ...]:
    """The daemon env vars the runtime OWNS and a manifest must never supply.

    Popped from the inherited environment before a daemon's own ``env`` map is
    overlaid, exactly like the channel A2A socket vars.  Empty when the contract
    is unavailable — there is then nothing authoritative to strip, and nothing is
    injected either, so the daemon simply sees an older-runtime environment.
    """
    if not CONTRACT_AVAILABLE:
        return ()
    return (STATE_DIR_ENV, DURABLE_ENV)  # type: ignore[return-value]


def _declared_root() -> str:
    """The provisioner's declared durable root, or "" when it declared none."""
    if not CONTRACT_AVAILABLE:
        return ""
    return os.environ.get(STATE_ROOT_ENV or "", "").strip()


def _probe_downgrades(directory: Path) -> bool:
    """True iff the durability probe REFUTES a declared-durable root.

    Downgrade-only, by contract.  ``DURABLE`` and ``SNAPSHOT`` are ignored rather
    than treated as evidence — crediting them is precisely the false positive
    that hid defect A, since local-docker's ``/workspace`` is a distinct named
    volume that probes ``DURABLE`` and is still destroyed on every restart.
    """
    try:
        from molecule_runtime.mailbox_dir import (
            DURABILITY_EPHEMERAL,
            DURABILITY_UNWRITABLE,
            probe_durability,
        )

        return probe_durability(directory) in (
            DURABILITY_EPHEMERAL,
            DURABILITY_UNWRITABLE,
        )
    except Exception as e:  # noqa: BLE001 — a probe failure must never fail spawn
        logger.warning(
            "plugin-state: durability probe failed for %s (%s); "
            "keeping the provisioner's declaration", directory, e,
        )
        return False


def resolve_plugin_state_dir(plugin_name: str) -> tuple[Path, bool]:
    """Return ``(directory, durable)`` for ``plugin_name``.

    ``directory`` is the plugin's OWN, already-namespaced state directory and is
    always writable.  ``durable`` is honest: it is True only when the provisioner
    DECLARED a durable root and the probe did not refute it.

    Never raises.  Any OSError falls back to the contract's fallback root with
    ``durable=False``; if even that is unwritable the path is still returned, so
    the caller always has something to inject and the daemon always starts.
    """
    if not CONTRACT_AVAILABLE:
        raise RuntimeError("plugin-state contract unavailable")

    root = _declared_root()
    declared_durable = bool(root)
    if not root:
        root = FALLBACK_ROOT or ""

    directory = Path(
        (DIR_TEMPLATE or "{state_root}/{plugin_name}").format(
            state_root=root, plugin_name=plugin_name
        )
    )

    try:
        directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE or 0o700)
    except OSError as e:
        logger.warning(
            "plugin-state: could not create %s for plugin %s (%s); "
            "falling back to %s", directory, plugin_name, e, FALLBACK_ROOT,
        )
        declared_durable = False
        directory = Path(
            (DIR_TEMPLATE or "{state_root}/{plugin_name}").format(
                state_root=FALLBACK_ROOT or "", plugin_name=plugin_name
            )
        )
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE or 0o700)
        except OSError as e2:
            # Still return the path. The daemon starts, writes fail loudly in
            # the plugin, and DURABLE=0 already told it not to trust this dir.
            logger.error(
                "plugin-state: fallback %s is also unusable for plugin %s (%s)",
                directory, plugin_name, e2,
            )
            return directory, False

    if not declared_durable:
        return directory, False
    # Declared durable — the probe may only take that away.
    if _probe_downgrades(directory):
        logger.warning(
            "plugin-state: %s declared a durable root but %s probes "
            "ephemeral/unwritable — reporting NOT durable",
            STATE_ROOT_ENV, directory,
        )
        return directory, False
    return directory, True


def state_env_for(plugin_name: str) -> dict[str, str]:
    """The reserved env pair to inject into ``plugin_name``'s daemon.

    ``{}`` when the contract is unavailable — which the contract itself defines
    as valid: an unset state dir means "older runtime, keep your existing path",
    so a plugin still runs, it just does not get a platform-chosen directory.
    """
    if not CONTRACT_AVAILABLE:
        return {}
    try:
        directory, durable = resolve_plugin_state_dir(plugin_name)
    except Exception as e:  # noqa: BLE001 — never fail a spawn over state
        logger.error(
            "plugin-state: could not resolve a state dir for plugin %s (%s); "
            "injecting nothing", plugin_name, e,
        )
        return {}
    return {
        STATE_DIR_ENV: str(directory),  # type: ignore[dict-item]
        DURABLE_ENV: (DURABLE_TRUE if durable else DURABLE_FALSE),  # type: ignore[dict-item]
    }


def log_degradation(plugin_name: str, durable: bool, directory: Path, state: object) -> None:
    """Say out loud that a plugin's declared-durable state is NOT durable.

    This function is the entire point of the contract.  The platform is allowed
    to degrade; it is not allowed to degrade SILENTLY, because a plugin that
    cannot tell its cursor was wiped re-pins to ``now()`` and loses mail with no
    error anywhere.  ``required`` warns and names the plugin plus the manifest's
    own description of what breaks; ``preferred`` is a cache, so INFO.
    """
    if durable or not CONTRACT_AVAILABLE:
        return
    durability, description = _read_state_declaration(state)
    if durability == "preferred":
        logger.info(
            "plugin-state: %s has no durable state dir (using %s); declared "
            "durability=preferred, so starting cold costs work, not correctness",
            plugin_name, directory,
        )
        return
    logger.warning(
        "plugin-state: %s declares durability=%s but %s is NOT durable on this "
        "workspace — state will be LOST on restart. %s The daemon is being told "
        "%s=%s; it should branch on that and degrade visibly (bounded lookback + "
        "id-dedup) rather than trust the directory.",
        plugin_name, durability, directory,
        f"Plugin says: {description}" if description else
        "The plugin declared no description of what is kept.",
        DURABLE_ENV, DURABLE_FALSE,
    )


def _read_state_declaration(state: object) -> tuple[str, str]:
    """``(durability, description)`` from a manifest's ``contributes.state``.

    Tolerant by contract: a non-mapping declaration is a bare declaration with
    the default posture, never a validation failure.  A typo must not brick a
    plugin at the (future fail-closed) install gate.
    """
    if not isinstance(state, dict):
        return "required", ""
    durability = state.get("durability")
    if durability not in ("required", "preferred"):
        durability = "required"
    description = state.get("description")
    return durability, description if isinstance(description, str) else ""


__all__ = [
    "CONTRACT_AVAILABLE",
    "STATE_DIR_ENV",
    "DURABLE_ENV",
    "DURABLE_TRUE",
    "DURABLE_FALSE",
    "STATE_ROOT_ENV",
    "DEFAULT_ROOT",
    "FALLBACK_ROOT",
    "DIR_TEMPLATE",
    "DIR_MODE",
    "reserved_env_names",
    "resolve_plugin_state_dir",
    "state_env_for",
    "log_degradation",
]
