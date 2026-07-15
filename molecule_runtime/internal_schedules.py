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
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from molecule_runtime.platform_inbound_auth import get_inbound_secret, inbound_authorized
from molecule_runtime.schedule_store import ScheduleError, ScheduleStore

# Filenames mirror the trigger-plugin daemon (SDK templates/trigger/scheduler.py).
# The daemon writes the health file; the API reads it. Kept in sync by the
# schedule contract's convention, not a shared import (the daemon is vendored
# per-plugin, not importable from the runtime process).
GRID_FILENAME = "schedules.yaml"
HEALTH_FILENAME = "schedule-health.json"
STATE_DIR_ENV = "MOLECULE_TRIGGER_STATE_DIR"


class ScheduleStoreUnconfigured(RuntimeError):
    """No trigger state dir is configured, so there is no grid to serve."""


def default_state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV, "").strip()
    if not configured:
        raise ScheduleStoreUnconfigured(f"{STATE_DIR_ENV} is not set")
    return Path(configured)


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
        "health": schedule_health,
    }


def add_schedule_routes(app, state_dir_factory: Callable[[], Path] = default_state_dir):
    """Register the schedule API routes on a Starlette app."""
    handlers = make_handlers(state_dir_factory)
    app.add_route("/internal/schedules", handlers["list"], methods=["GET"])
    app.add_route("/internal/schedules", handlers["create"], methods=["POST"])
    app.add_route("/internal/schedules/health", handlers["health"], methods=["GET"])
    app.add_route(
        "/internal/schedules/{name}", handlers["update"], methods=["PATCH"]
    )
    app.add_route(
        "/internal/schedules/{name}", handlers["delete"], methods=["DELETE"]
    )


__all__ = [
    "GRID_FILENAME",
    "HEALTH_FILENAME",
    "STATE_DIR_ENV",
    "ScheduleStoreUnconfigured",
    "add_schedule_routes",
    "default_state_dir",
    "make_handlers",
]
