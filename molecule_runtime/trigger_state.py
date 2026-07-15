"""Canonical location of a trigger scheduler plugin's durable state.

The schedule grid, fire-state bookkeeping, poke queue, run history, and health
heartbeat all live in ONE per-workspace directory on the persisted config
volume.  Both the runtime schedule API (in the runtime process) and the trigger
daemon (a plugin subprocess) resolve the SAME directory through this module so
they always agree — the API writes the grid the daemon reads, and reads the
health/history the daemon writes.

Deliberately import-light (only ``os`` + the lazy ``configs_dir`` resolve) so it
can be imported from the channel-events daemon-env injector without dragging the
schedule store's ``jsonschema`` / ``cronspec`` dependencies into that path.
"""

from __future__ import annotations

import os
from pathlib import Path

# Explicit override (also the env the runtime injects into the daemon subprocess
# so it resolves to exactly the directory the API resolved).
STATE_DIR_ENV = "MOLECULE_TRIGGER_STATE_DIR"

# Subdirectory of the persisted config volume the grid + state live under.
TRIGGER_STATE_SUBDIR = "schedules"

# The grid file the API writes and both the daemon and the boot seeder read.
# Defined here (import-light) so the seeder can resolve it without importing the
# schedule API (starlette). internal_schedules re-exports this for back-compat.
GRID_FILENAME = "schedules.yaml"


def resolve_trigger_state_dir() -> Path:
    """Return the workspace's trigger state dir on the persisted volume.

    ``MOLECULE_TRIGGER_STATE_DIR`` wins when set (operator override / the value
    injected into the daemon); otherwise it is ``<configs_dir>/schedules``, where
    ``configs_dir`` is the provisioner-owned persisted volume (``/configs`` in a
    managed container).
    """
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    from molecule_runtime.configs_dir import resolve as resolve_configs_dir

    return resolve_configs_dir() / TRIGGER_STATE_SUBDIR


def resolve_grid_path() -> Path:
    """Full path to the schedule grid file both the API and daemon use."""
    return resolve_trigger_state_dir() / GRID_FILENAME


__all__ = [
    "STATE_DIR_ENV",
    "TRIGGER_STATE_SUBDIR",
    "GRID_FILENAME",
    "resolve_trigger_state_dir",
    "resolve_grid_path",
]
