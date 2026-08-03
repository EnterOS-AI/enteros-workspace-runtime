"""Canonical location of a trigger scheduler plugin's durable state.

The schedule grid, fire-state bookkeeping, poke queue, run history, and health
heartbeat all live in ONE per-workspace directory on a DURABLE volume.  Both the
runtime schedule API (in the runtime process) and the trigger daemon (a plugin
subprocess) resolve the SAME directory through this module so they always agree
— the API writes the grid the daemon reads, and reads the health/history the
daemon writes.

Why not ``<configs_dir>/schedules`` (molecule-ai-workspace-runtime#370, core#5036)
----------------------------------------------------------------------------------
That was the original root and it is NOT durable.  molecule-controlplane's
``internal/provisioner/local_docker_workspace.go`` ``workspaceTeardownVolumes``
removes the ``/configs`` and ``/workspace`` named volumes on EVERY teardown,
including a plain restart; only ``mol-ws-pstate-*`` / ``mol-ws-rtstate-*`` are
preserved (``keepRuntimeState=true``).  ``configs_dir`` documents the same thing
about itself: ``/configs`` is "rewritten on every provision… NOT a safe home for
evolving agent state".

A restart therefore destroyed the whole grid — every user-created
``source='runtime'`` schedule, the last-fire watermark (so surviving schedules
miss or double-fire), the poke queue and the run history — silently, with no
error and nothing to distinguish "the grid was wiped" from "nothing was due".
``config_dir: existing-volume`` in a restart response does NOT contradict this:
it only means core selected no template (``restart_template.go`` tier 6), while
the provisioner still tears the volume down and re-seeds it.

So the grid now roots on the provisioner-declared durable plugin-state root
(``plugin_state`` / the plugin-state contract), which is exactly the resolution
#370 called for once that seam landed.  This is a re-root, not a new mechanism:
``plugin_state`` is itself this module generalised, and it owns the durable-root
resolution, the downgrade-only durability probe and the fallback root.

Deliberately import-light (only ``os``/``shutil`` + the lazy ``configs_dir`` and
``plugin_state`` resolves) so it can be imported from the channel-events
daemon-env injector without dragging the schedule store's ``jsonschema`` /
``cronspec`` dependencies into that path.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Explicit override (also the env the runtime injects into the daemon subprocess
# so it resolves to exactly the directory the API resolved).
STATE_DIR_ENV = "MOLECULE_TRIGGER_STATE_DIR"

# Subdirectory of the persisted config volume the grid + state live under.
# LEGACY root — kept as the fallback for a control plane that declares no
# durable plugin-state root, and as the source of the one-time carry-over below.
TRIGGER_STATE_SUBDIR = "schedules"

# The grid file the API writes and both the daemon and the boot seeder read.
# Defined here (import-light) so the seeder can resolve it without importing the
# schedule API (starlette). internal_schedules re-exports this for back-compat.
GRID_FILENAME = "schedules.yaml"

# Name of the trigger grid's directory under the durable plugin-state root.
#
# RESERVED, and deliberately NOT a plugin name. The plugin-state contract keys a
# plugin's own directory on its VALIDATED MANIFEST NAME
# (``{state_root}/{plugin_name}``); this grid is not one plugin's private state —
# it is the WORKSPACE's schedule grid, written by the runtime API and read by
# whichever ``kind: trigger`` plugin is installed. Keying it on a manifest name
# would move the grid the day the installed scheduler plugin is swapped or
# renamed, silently orphaning every schedule. The leading dot keeps it out of the
# manifest-name space (a plugin install directory is derived from its source repo
# and cannot begin with a dot).
DURABLE_STATE_SUBDIR = ".trigger-state"

# Files the one-time carry-over moves off the legacy /configs root. The grid is
# the user-visible one; the watermark matters just as much, because losing it is
# what makes surviving schedules miss or double-fire.
_CARRYOVER_FILENAMES = (
    GRID_FILENAME,
    "schedule-state.json",
    "schedule-pokes.json",
    "schedule-history.json",
    ".schedule-tombstones.json",
)

# The carry-over is idempotent but only worth attempting once per process.
_migration_attempted = False


def reset_migration_state() -> None:
    """Forget that the legacy carry-over ran (tests only)."""
    global _migration_attempted
    _migration_attempted = False


def _durable_state_dir() -> Path | None:
    """The grid's directory on the provisioner-declared durable root, if any.

    Returns ``None`` when the plugin-state seam cannot supply a DURABLE root —
    the contract mirror is missing, the provisioner declared nothing, or the
    probe refuted the declaration. In every one of those cases the caller keeps
    the legacy ``/configs`` root: relocating the grid onto a root that is not
    actually durable would trade a known-lossy location for an unknown-lossy one
    while looking like a fix, which is the failure mode the plugin-state contract
    exists to prevent.
    """
    try:
        from molecule_runtime import plugin_state

        if not plugin_state.CONTRACT_AVAILABLE:
            return None
        directory, durable = plugin_state.resolve_plugin_state_dir(
            DURABLE_STATE_SUBDIR
        )
        if not durable:
            return None
        return directory
    except Exception as exc:  # noqa: BLE001 — never break boot over state-dir resolution
        logger.warning(
            "trigger-state: durable root unavailable (%s); keeping the legacy "
            "configs-volume grid", exc,
        )
        return None


def _carry_over_legacy_state(durable: Path) -> None:
    """One-time copy of a pre-existing ``/configs`` grid onto the durable root.

    Without this the re-root would itself wipe every live workspace's schedules
    exactly once — the defect it is fixing, in a new costume.

    Copy, never move, and NEVER overwrite: ``/configs`` is re-seeded from the org
    template on every provision, so a legacy grid can REAPPEAR long after the
    migration ran. A second copy would revert every edit made since, so an
    already-present destination file always wins. Best-effort by contract; a
    failure leaves the durable dir empty rather than blocking boot.
    """
    global _migration_attempted
    if _migration_attempted:
        return
    _migration_attempted = True
    try:
        from molecule_runtime.configs_dir import resolve as resolve_configs_dir

        legacy = resolve_configs_dir() / TRIGGER_STATE_SUBDIR
        if not legacy.is_dir() or legacy.resolve() == durable.resolve():
            return
        for filename in _CARRYOVER_FILENAMES:
            src = legacy / filename
            dst = durable / filename
            if not src.is_file() or dst.exists():
                continue
            shutil.copy2(src, dst)
            logger.info(
                "trigger-state: carried %s from the legacy configs grid onto the "
                "durable root (%s)", filename, durable,
            )
    except Exception as exc:  # noqa: BLE001 — a failed carry-over must not block boot
        logger.warning("trigger-state: legacy grid carry-over failed (%s)", exc)


def resolve_trigger_state_dir() -> Path:
    """Return the workspace's trigger state dir on a DURABLE volume.

    Resolution order:

    1. ``MOLECULE_TRIGGER_STATE_DIR`` — operator override / the value injected
       into the daemon subprocess. Unchanged, and still outranks everything.
    2. The provisioner-declared durable plugin-state root (see
       :func:`_durable_state_dir`), carrying a pre-existing legacy grid over on
       first use.
    3. ``<configs_dir>/schedules`` — the legacy root. Reached only when no
       durable root is declared, so a control plane that predates the
       plugin-state contract behaves exactly as it did before.
    """
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override)

    durable = _durable_state_dir()
    if durable is not None:
        _carry_over_legacy_state(durable)
        return durable

    from molecule_runtime.configs_dir import resolve as resolve_configs_dir

    return resolve_configs_dir() / TRIGGER_STATE_SUBDIR


def resolve_grid_path() -> Path:
    """Full path to the schedule grid file both the API and daemon use."""
    return resolve_trigger_state_dir() / GRID_FILENAME


__all__ = [
    "STATE_DIR_ENV",
    "TRIGGER_STATE_SUBDIR",
    "DURABLE_STATE_SUBDIR",
    "GRID_FILENAME",
    "reset_migration_state",
    "resolve_trigger_state_dir",
    "resolve_grid_path",
]
