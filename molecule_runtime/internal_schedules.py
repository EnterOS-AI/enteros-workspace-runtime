"""Runtime-exposed schedule API (P3, Option A).

The write side of the schedule grid that a ``kind: trigger`` scheduler plugin
fires from.  Canvas / admin reach these ``/internal/schedules*`` routes through
the platform proxy (same forward-auth as the other ``/internal/*`` routes: the
per-workspace ``platform_inbound_secret``).  Every write goes through
:class:`molecule_runtime.schedule_store.ScheduleStore`, so the SDK ``schedule``
contract + caps + cron grammar are enforced here, and the trigger daemon reads
the same volume grid file the API writes.

Scope of this module: List / Create / Update / Delete / Health — the routes that
sit purely on the durable grid + the daemon's health file.  RunNow, History, and
the webhook event-poke need a live daemon poke channel and are wired separately.

The grid + health file live under ``MOLECULE_TRIGGER_STATE_DIR`` — the SAME dir
the runtime injects into the trigger daemon — so the two agree on one location.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from molecule_runtime.platform_inbound_auth import get_inbound_secret, inbound_authorized
from molecule_runtime.schedule_store import ScheduleError, ScheduleStore
from molecule_runtime.trigger_state import (
    GRID_FILENAME,
    STATE_DIR_ENV,
    resolve_trigger_state_dir,
)

# GRID_FILENAME is the SSOT in trigger_state (import-light) and re-exported here
# for back-compat. HEALTH/POKES filenames mirror the trigger-plugin daemon (SDK
# templates/trigger/scheduler.py): the daemon writes the health file, the API
# reads it — kept in sync by the schedule contract's convention, not a shared
# import (the daemon is vendored per-plugin, not importable from the runtime).
HEALTH_FILENAME = "schedule-health.json"
POKES_FILENAME = "schedule-pokes.json"
HISTORY_FILENAME = "schedule-history.json"
# STATE_DIR_ENV is re-exported from trigger_state (the shared resolver) so the
# API and the injected daemon agree on one location.


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _enqueue_poke(state_dir: Path, name: str) -> None:
    """Add a schedule name to the poke set the daemon consumes next tick.

    Read-modify-write of a single JSON set. The daemon is the only clearer; a
    RunNow landing in the same ~poll-interval window the daemon clears is a rare,
    user-retriable loss, acceptable for a fire-now signal.
    """
    path = state_dir / POKES_FILENAME
    current: set[str] = set()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                current = {str(n) for n in raw}
        except (ValueError, OSError):
            current = set()
    current.add(name)
    _atomic_write_json(path, sorted(current))


class ScheduleStoreUnconfigured(RuntimeError):
    """No trigger state dir is configured, so there is no grid to serve."""


def default_state_dir() -> Path:
    """The trigger state dir on the persisted volume (shared with the daemon)."""
    return resolve_trigger_state_dir()


def _unauthorized(request: Request) -> bool:
    return not inbound_authorized(
        get_inbound_secret(), request.headers.get("Authorization", "")
    )


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ScheduleError("request body must be valid JSON")
    if not isinstance(payload, dict):
        raise ScheduleError("request body must be a JSON object")
    return payload


def make_handlers(
    state_dir_factory: Callable[[], Path] = default_state_dir,
) -> dict[str, Callable]:
    """Build the route handlers, with the state dir injectable for tests.

    The store and the health file both derive from ONE state dir so the API and
    the trigger daemon always agree on the grid + health locations.
    """

    def _dir_or_503() -> Path | JSONResponse:
        try:
            return state_dir_factory()
        except ScheduleStoreUnconfigured as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

    def _store_or_503() -> ScheduleStore | JSONResponse:
        state_dir = _dir_or_503()
        if isinstance(state_dir, JSONResponse):
            return state_dir
        return ScheduleStore(state_dir / GRID_FILENAME)

    async def list_schedules(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        store = _store_or_503()
        if isinstance(store, JSONResponse):
            return store
        try:
            return JSONResponse({"schedules": store.list()})
        except ScheduleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def create_schedule(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        store = _store_or_503()
        if isinstance(store, JSONResponse):
            return store
        try:
            entry = await _body(request)
            created = store.create(entry)
        except ScheduleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(created, status_code=201)

    async def update_schedule(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        store = _store_or_503()
        if isinstance(store, JSONResponse):
            return store
        name = request.path_params["name"]
        try:
            patch = await _body(request)
            updated = store.update(name, patch)
        except ScheduleError as exc:
            status = 404 if "no such schedule" in str(exc) else 400
            return JSONResponse({"error": str(exc)}, status_code=status)
        return JSONResponse(updated)

    async def delete_schedule(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        store = _store_or_503()
        if isinstance(store, JSONResponse):
            return store
        name = request.path_params["name"]
        try:
            removed = store.delete(name)
        except ScheduleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not removed:
            return JSONResponse({"error": f"no such schedule: {name!r}"}, status_code=404)
        return JSONResponse({"deleted": name})

    async def run_now(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        state_dir = _dir_or_503()
        if isinstance(state_dir, JSONResponse):
            return state_dir
        name = request.path_params["name"]
        store = ScheduleStore(state_dir / GRID_FILENAME)
        try:
            existing = store.get(name)
        except ScheduleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        if existing is None:
            return JSONResponse({"error": f"no such schedule: {name!r}"}, status_code=404)
        if not existing.get("enabled", True):
            return JSONResponse(
                {"error": f"schedule is disabled: {name!r}"}, status_code=409
            )
        _enqueue_poke(state_dir, name)
        # Accepted, not fired: the daemon consumes the poke on its next tick.
        return JSONResponse({"poked": name}, status_code=202)

    async def schedule_history(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        state_dir = _dir_or_503()
        if isinstance(state_dir, JSONResponse):
            return state_dir
        path = state_dir / HISTORY_FILENAME
        if not path.is_file():
            return JSONResponse({"history": []})
        try:
            log = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": f"unreadable history: {exc}"}, status_code=500)
        if not isinstance(log, list):
            log = []
        name = request.path_params.get("name")
        if name is not None:
            log = [row for row in log if isinstance(row, dict) and row.get("name") == name]
        return JSONResponse({"history": log})

    async def schedule_health(request: Request) -> JSONResponse:
        if _unauthorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        state_dir = _dir_or_503()
        if isinstance(state_dir, JSONResponse):
            return state_dir
        health_path = state_dir / HEALTH_FILENAME
        if not health_path.is_file():
            # No tick has run yet — the daemon has not written health. Report the
            # armed count from the grid so the surface is never blank.
            armed = 0
            try:
                armed = len(ScheduleStore(state_dir / GRID_FILENAME).list())
            except ScheduleError:
                armed = 0
            return JSONResponse({"last_tick": None, "armed": armed, "errors": {}})
        try:
            payload = json.loads(health_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": f"unreadable health: {exc}"}, status_code=500)
        return JSONResponse(payload)

    return {
        "list": list_schedules,
        "create": create_schedule,
        "update": update_schedule,
        "delete": delete_schedule,
        "run": run_now,
        "history": schedule_history,
        "health": schedule_health,
    }


def add_schedule_routes(app, state_dir_factory: Callable[[], Path] = default_state_dir):
    """Register the schedule API routes on a Starlette app."""
    handlers = make_handlers(state_dir_factory)
    app.add_route("/internal/schedules", handlers["list"], methods=["GET"])
    app.add_route("/internal/schedules", handlers["create"], methods=["POST"])
    # Fixed segments before the /{name} matcher so they are not shadowed.
    app.add_route("/internal/schedules/health", handlers["health"], methods=["GET"])
    app.add_route("/internal/schedules/history", handlers["history"], methods=["GET"])
    app.add_route(
        "/internal/schedules/{name}", handlers["update"], methods=["PATCH"]
    )
    app.add_route(
        "/internal/schedules/{name}", handlers["delete"], methods=["DELETE"]
    )
    app.add_route(
        "/internal/schedules/{name}/run", handlers["run"], methods=["POST"]
    )
    app.add_route(
        "/internal/schedules/{name}/history", handlers["history"], methods=["GET"]
    )


__all__ = [
    "GRID_FILENAME",
    "HEALTH_FILENAME",
    "POKES_FILENAME",
    "HISTORY_FILENAME",
    "STATE_DIR_ENV",
    "ScheduleStoreUnconfigured",
    "add_schedule_routes",
    "default_state_dir",
    "make_handlers",
]
