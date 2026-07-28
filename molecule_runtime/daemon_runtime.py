"""Plugin-daemon lifecycle holder — establish at boot, extend on hot-install.

The plugin-daemon supervisor and its private per-plugin A2A sockets are set up
once at boot (main.py). But a ``kind: trigger`` (or channel) plugin can be
installed AFTER boot — e.g. the platform installs ``molecule-scheduler`` into a
workspace the moment it gets its first schedule (scheduler-as-trigger-plugin
RFC, per-workspace delivery). Without this holder the new daemon would not run
until the next workspace restart, because the supervisor and sockets only ever
came up at boot.

:class:`DaemonRuntime` owns that lifecycle so it survives past ``main()``'s
frame and can be reached from an HTTP handler. Its single idempotent entry
point, :meth:`ensure_daemons`, is called:

  * once at boot (the COLD start — build supervisor + sockets, arm every
    manifest-declared daemon), and
  * by ``POST /internal/daemons/reload`` after a post-boot plugin install (the
    WARM path — bind lanes for and supervise only the newly-installed daemons,
    leaving the running ones untouched).

Both paths re-derive the desired daemon set from the installed plugins on disk,
so ``ensure_daemons`` converges the running daemons toward what is installed.
Hot-REMOVE (uninstall) is intentionally out of scope — uninstall still takes a
restart; the live gap this closes is hot-ADD.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class DaemonRuntime:
    """Owns the plugin-daemon supervisor + channel/trigger socket transport."""

    def __init__(
        self,
        app: Any,
        config_path: str,
        server: Any = None,
        *,
        log_level: str = "warning",
    ) -> None:
        self.app = app
        self.config_path = config_path
        self.server = server
        self.log_level = log_level
        self.supervisor = None
        self.event_transport = None
        self._lock = asyncio.Lock()

    async def ensure_daemons(self, *, wait_for_bind: bool = True) -> dict:
        """Converge running daemons toward the installed-plugin set. Idempotent.

        Returns a small summary — ``{armed, added, trigger}`` — so the reload
        endpoint (and boot log) can report what changed. Serialized by a lock so
        a reload racing another reload (or boot) never double-binds a socket or
        double-supervises a daemon.
        """
        async with self._lock:
            return await self._ensure_locked(wait_for_bind=wait_for_bind)

    async def _ensure_locked(self, *, wait_for_bind: bool) -> dict:
        from molecule_runtime import plugin_daemons
        from molecule_runtime.channel_events import ChannelEventSocketManager
        from molecule_runtime.plugins import load_plugins

        loaded = load_plugins(
            workspace_plugins_dir=os.path.join(self.config_path, "plugins"),
        )
        specs = plugin_daemons.discover_daemon_specs(loaded=loaded)

        # G2: a present trigger plugin means this workspace schedules natively,
        # so core must defer (NativeSchedulerCheck). Set-or-clear so a
        # (hot-installed) trigger flips it on and an uninstall-then-reload off.
        # Seed the template grid before the daemon reads it — same as boot.
        if plugin_daemons.has_trigger_daemon(specs):
            os.environ[plugin_daemons.NATIVE_SCHEDULER_ENV] = "1"
            try:
                from molecule_runtime import schedule_seed

                seeded = schedule_seed.seed_schedules_from_plugins(loaded=loaded)
                if seeded:
                    logger.info("Schedule seed: reconciled %d template grid(s)", seeded)
            except Exception as e:  # noqa: BLE001 — seeding must never break a reload
                logger.warning("Schedule seed failed (non-fatal): %s", e)
            # RFC §8A P3 seam (runtime leg): core also delivers template/org
            # schedules INSIDE the provisioned config.yaml (top-level
            # `schedules:`). Seed them AFTER the plugin grids, still before the
            # trigger daemon reads the grid — on cold boot and on the warm
            # /internal/daemons/reload path alike. Inert until core ships the
            # key: pre-seam configs simply lack it (clean no-op).
            try:
                from molecule_runtime import schedule_seed
                from molecule_runtime.schedule_store import ScheduleStore
                from molecule_runtime.trigger_state import resolve_grid_path

                store = ScheduleStore(resolve_grid_path())
                seeded_cfg = schedule_seed.seed_schedules_from_workspace_config(
                    self.config_path, store
                )
                if seeded_cfg:
                    logger.info(
                        "Schedule seed: %d workspace-config schedule(s)", seeded_cfg
                    )
                # Schedules delivered as a trigger plugin's own config (the M3
                # location). Inert until a template uses it — every plugin
                # without a `schedules` key is a clean no-op. Runs on the same
                # boot AND /internal/daemons/reload path as the leg above, so a
                # reload picks up a changed grid either way.
                seeded_plugin = schedule_seed.seed_schedules_from_plugin_settings(
                    self.config_path, store
                )
                if seeded_plugin:
                    logger.info(
                        "Schedule seed: %d plugin-config schedule(s)", seeded_plugin
                    )
            except Exception as e:  # noqa: BLE001 — seeding must never break a reload
                logger.warning(
                    "Workspace-config schedule seed failed (non-fatal): %s", e
                )
        else:
            os.environ.pop(plugin_daemons.NATIVE_SCHEDULER_ENV, None)

        if not specs:
            return {"armed": 0, "added": [], "trigger": False}

        # A lane daemon posts inbound turns at the local A2A server, so never
        # bind/spawn before uvicorn is listening (same gate boot uses). The
        # reload path runs long after bind, so this returns immediately there.
        if wait_for_bind and self.server is not None:
            await plugin_daemons.wait_until_server_bound(self.server)

        if self.supervisor is None:
            # COLD start — first daemon(s) this process has seen.
            self.supervisor = plugin_daemons.DaemonSupervisor(specs)
            self.event_transport = ChannelEventSocketManager(
                self.app, specs, log_level=self.log_level,
            )
            try:
                await self.event_transport.start()
            except Exception as e:  # noqa: BLE001 — agent + generic daemons survive
                logger.error(
                    "channel events: local socket unavailable (%s); "
                    "arming daemons without the local capability", e,
                )
                self.event_transport.clear_daemon_env()
            self.supervisor.start()
            added = list(specs)
        else:
            # WARM — bind lanes for and supervise only the NEW daemons.
            existing = {s.key for s in self.supervisor.specs}
            new_specs = [s for s in specs if s.key not in existing]
            if not new_specs:
                return {
                    "armed": len(self.supervisor.specs),
                    "added": [],
                    "trigger": plugin_daemons.has_trigger_daemon(self.supervisor.specs),
                }
            if self.event_transport is not None:
                try:
                    await self.event_transport.add_specs(new_specs)
                except Exception as e:  # noqa: BLE001 — supervise even if the lane failed
                    logger.error(
                        "channel events: hot-add socket failed (%s); "
                        "supervising new daemon(s) without the local capability", e,
                    )
            added = self.supervisor.supervise(new_specs)

        return {
            "armed": len(self.supervisor.specs),
            "added": [s.key for s in added],
            "trigger": plugin_daemons.has_trigger_daemon(self.supervisor.specs),
        }

    async def stop(self) -> None:
        """Tear down every daemon + socket. Idempotent; safe on a never-armed
        holder. Called from main()'s finally so daemons die with the workspace."""
        if self.supervisor is not None:
            try:
                self.supervisor.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("daemon supervisor stop failed: %s", e)
        if self.event_transport is not None:
            try:
                await self.event_transport.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("channel event transport stop failed: %s", e)


def add_daemon_routes(app, holder: "DaemonRuntime") -> None:
    """Mount ``POST /internal/daemons/reload`` — the platform's signal that a
    plugin was installed post-boot and its daemon should be armed now (no
    restart). Same forward-auth as the other ``/internal/*`` routes."""
    from starlette.responses import JSONResponse

    from molecule_runtime.platform_inbound_auth import (
        get_inbound_secret,
        inbound_authorized,
    )

    async def reload_daemons(request):
        if not inbound_authorized(
            get_inbound_secret(), request.headers.get("Authorization", "")
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            result = await holder.ensure_daemons()
        except Exception as e:  # noqa: BLE001 — a reload failure must not 500 the box down
            logger.error("daemon reload failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(result)

    app.add_route("/internal/daemons/reload", reload_daemons, methods=["POST"])


__all__ = ["DaemonRuntime", "add_daemon_routes"]
