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
from typing import Any

from molecule_runtime.schedule_store import ScheduleError, ScheduleStore, validate_entry
from molecule_runtime.trigger_state import resolve_grid_path


def trigger_plugin_schedule_files(
    workspace_plugins_dir: str | None = None,
    shared_plugins_dir: str | None = None,
    loaded=None,
) -> list[Path]:
    """Paths to the ``schedules.yaml`` each installed ``kind: trigger`` plugin ships.

    Reuses the canonical installed-plugin scan (``plugins.load_plugins``) so
    priority/dedup/SSOT semantics match every other plugin surface. Returns only
    files that exist, in discovery order. Pass a pre-loaded ``LoadedPlugins``
    (``loaded=``) to reuse the boot-time scan ``discover_daemon_specs`` already
    ran, instead of re-scanning every plugin manifest from disk.
    """
    if loaded is None:
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
    loaded=None,
) -> int:
    """Reconcile every trigger plugin's template grid into the live volume grid.

    Additive upsert per source (source='runtime' edits preserved, user-deleted
    template schedules stay deleted). Returns the number of template files
    successfully applied. Never raises — a malformed/unreadable template grid, or
    a merged grid that would exceed the ``MAX_ENTRIES`` cap, is logged and skipped
    so boot proceeds. Pass ``loaded`` to reuse the boot-time plugin scan.
    """
    sources = trigger_plugin_schedule_files(
        workspace_plugins_dir=workspace_plugins_dir,
        shared_plugins_dir=shared_plugins_dir,
        loaded=loaded,
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
        except ScheduleError as exc:
            # ScheduleError includes the MAX_ENTRIES overflow (the merged grid
            # would exceed the cap) and a malformed/invalid template grid. Both
            # mean the plugin's template schedules did NOT land — call it out
            # distinctly rather than as a generic "skipped file" so an operator
            # can see the grid is at capacity vs. the file being bad.
            print(
                f"schedule seed: template schedules from {src} NOT applied "
                f"(grid unchanged): {exc}",
                flush=True,
            )
        except OSError as exc:
            print(f"schedule seed: skipped {src} (unreadable, non-fatal): {exc}", flush=True)
    return applied


def seed_schedules_from_workspace_config(
    configs_dir: str | os.PathLike[str], store: ScheduleStore
) -> int:
    """Reconcile core-delivered template/org schedules from ``config.yaml`` into the grid.

    RFC §8A P3 template-seeding seam (runtime leg): core delivers schedules as a
    top-level ``schedules:`` list inside the provisioned ``<configs_dir>/config.yaml``
    — the same config/asset channel that lands prompt files on the volume BEFORE
    boot, so there is no core→runtime API race on first provision. Entries accept
    ``name`` (required), ``cron`` (or the authoring alias ``cron_expr``),
    ``prompt`` inline or ``prompt_file`` (resolved STRICTLY confined to
    ``configs_dir`` — absolute paths and traversal outside it are rejected),
    ``enabled`` (default true) and ``timezone`` (optional).

    Validate-and-skip per entry: a bad entry (invalid cron, missing name,
    oversize prompt, escaping prompt_file, ...) is logged and skipped, never
    raised, and the valid subset is seeded via
    :meth:`ScheduleStore.upsert_template` (template semantics: runtime edits
    survive, tombstones respected). A missing ``config.yaml`` or missing/empty
    ``schedules:`` key is a clean no-op — every pre-seam config simply lacks the
    key, so this is INERT until core ships it. Returns the number of entries
    seeded. FAIL-SOFT by contract: no exception escapes to block boot or reload.
    """
    try:
        return _seed_workspace_config(Path(configs_dir), store)
    except Exception as exc:  # noqa: BLE001 — seeding must never block boot/reload
        print(
            f"schedule seed: workspace config.yaml seeding failed (non-fatal): {exc}",
            flush=True,
        )
        return 0


def _seed_workspace_config(configs_dir: Path, store: ScheduleStore) -> int:
    config_path = configs_dir / "config.yaml"
    if not config_path.is_file():
        return 0
    import yaml

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        print(f"schedule seed: unreadable {config_path} (non-fatal): {exc}", flush=True)
        return 0

    schedules = raw.get("schedules") if isinstance(raw, dict) else None
    if not isinstance(schedules, list) or not schedules:
        if schedules not in (None, []):
            print(
                f"schedule seed: `schedules` in {config_path} is not a list; ignored",
                flush=True,
            )
        return 0

    valid: list[dict[str, Any]] = []
    for index, entry in enumerate(schedules):
        norm = _normalize_config_entry(entry, index, configs_dir)
        if norm is not None:
            valid.append(norm)
    if not valid:
        return 0

    try:
        store.upsert_template(valid)
    except ScheduleError as exc:
        # Includes the MAX_ENTRIES overflow on the merged grid — the delivered
        # schedules did NOT land, prior grid intact (upsert_template is atomic).
        print(
            f"schedule seed: config.yaml schedules NOT applied (grid unchanged): {exc}",
            flush=True,
        )
        return 0
    return len(valid)


def _normalize_config_entry(entry: Any, index: int, configs_dir: Path) -> dict[str, Any] | None:
    """Validate one delivered entry; return the contract-normalized form or None (skip)."""
    label = f"config.yaml schedules[{index}]"
    if not isinstance(entry, dict):
        print(f"schedule seed: skipped {label}: entry must be a mapping", flush=True)
        return None

    candidate = dict(entry)
    if "cron" not in candidate and "cron_expr" in candidate:
        candidate["cron"] = candidate["cron_expr"]
    if "prompt" not in candidate and "prompt_file" in candidate:
        prompt = _read_confined_prompt_file(candidate["prompt_file"], configs_dir, label)
        if prompt is None:
            return None
        candidate["prompt"] = prompt

    try:
        # Filter to contract fields (the entry schema is additionalProperties:
        # false); `source` is intentionally excluded — upsert_template stamps it.
        return validate_entry(
            {k: candidate[k] for k in ("name", "cron", "timezone", "prompt", "enabled") if k in candidate}
        )
    except ScheduleError as exc:
        print(f"schedule seed: skipped {label}: {exc}", flush=True)
        return None


def _read_confined_prompt_file(prompt_file: Any, configs_dir: Path, label: str) -> str | None:
    """Read a prompt file STRICTLY confined to ``configs_dir``; None = reject/skip.

    Absolute paths are rejected outright; relative paths are resolved (symlinks
    included) and must land inside ``configs_dir`` — ``../`` escapes are refused.
    """
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        print(f"schedule seed: skipped {label}: prompt_file must be a relative path", flush=True)
        return None
    rel = Path(prompt_file)
    if rel.is_absolute():
        print(
            f"schedule seed: skipped {label}: absolute prompt_file rejected "
            f"(must be relative to the configs dir)",
            flush=True,
        )
        return None
    base = configs_dir.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        print(
            f"schedule seed: skipped {label}: prompt_file escapes the configs dir",
            flush=True,
        )
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"schedule seed: skipped {label}: prompt_file unreadable: {exc}", flush=True)
        return None
