"""Reconcile-on-boot seeding of the schedule grid from template-delivered files.

Schedules are volume-authoritative (scheduler-as-trigger-plugin RFC, Option A):
the per-workspace grid on the persisted volume is the source of truth, and core
no longer seeds a ``workspace_schedules`` table (P4b). Instead, the runtime seeds
its own grid on boot from the ``schedules.yaml`` that the SDK ``kind: trigger``
scaffold ships inside the trigger plugin — delivered to
``<config_path>/plugins/<plugin>/schedules.yaml`` by the declared-plugins
boot-install, the same channel that lands skills and MCP configs.

Seeding is **additive and edit-preserving** (:meth:`ScheduleStore.upsert_template`):
a re-provision refreshes template-owned entries but never clobbers a user's own
runtime edits. It is fail-soft — a bad or missing template grid must not block
boot; the schedule API and daemon still run on whatever grid is present.

This must run BEFORE the trigger daemon starts reading the grid (see the boot
seam in ``main.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

from molecule_runtime.schedule_store import ScheduleError, ScheduleStore
from molecule_runtime.trigger_state import resolve_grid_path


def trigger_plugin_schedule_files(
    workspace_plugins_dir: str | None = None,
    shared_plugins_dir: str | None = None,
) -> list[Path]:
    """Paths to the ``schedules.yaml`` each installed ``kind: trigger`` plugin ships.

    Reuses the canonical installed-plugin scan (``plugins.load_plugins``) so
    priority/dedup/SSOT semantics match every other plugin surface. Returns only
    files that exist, in discovery order.
    """
    from molecule_runtime.plugins import load_plugins

    loaded = load_plugins(
        workspace_plugins_dir=workspace_plugins_dir,
        shared_plugins_dir=shared_plugins_dir or os.environ.get("PLUGINS_DIR", "/plugins"),
    )
    files: list[Path] = []
    for plugin in loaded.plugins:
        if plugin.manifest.kind != "trigger":
            continue
        candidate = Path(plugin.path) / "schedules.yaml"
        if candidate.is_file():
            files.append(candidate)
    return files


def seed_schedules_from_plugins(
    *,
    workspace_plugins_dir: str | None = None,
    shared_plugins_dir: str | None = None,
    grid_path: Path | None = None,
) -> int:
    """Reconcile every trigger plugin's template grid into the live volume grid.

    Additive upsert per source (source='runtime' edits preserved). Returns the
    number of template files successfully applied. Never raises — a malformed or
    unreadable template grid is logged and skipped so boot proceeds.
    """
    sources = trigger_plugin_schedule_files(
        workspace_plugins_dir=workspace_plugins_dir,
        shared_plugins_dir=shared_plugins_dir,
    )
    if not sources:
        return 0

    target = ScheduleStore(grid_path or resolve_grid_path())
    applied = 0
    for src in sources:
        try:
            delivered = ScheduleStore(src).load()  # validates the template grid
            target.upsert_template(delivered)
            applied += 1
        except (ScheduleError, OSError) as exc:
            print(f"schedule seed: skipped {src} (non-fatal): {exc}", flush=True)
    return applied
