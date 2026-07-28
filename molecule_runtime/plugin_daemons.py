"""Plugin-declared channel daemons — manifest-declared long-running sidecars.

PR-1 of issue #215 introduced the daemon lifecycle. A plugin can declare a
long-running daemon — e.g. a channel bridge like ``lark-channel-molecule`` —
under ``contributes.daemons`` in its ``plugin.yaml``; the workspace runtime
spawns it at boot, restarts it on crash, and kills it with the workspace. The
connected workspace owns its channel processes — no CP supervision domain.
PR-2 adds the private local A2A binding in :mod:`molecule_runtime.channel_events`;
this module's post-bind starter coordinates that binding before spawn. Only a
plugin whose manifest declares ``kind: channel`` receives that capability;
other supervised daemons never inherit or receive its reserved environment.

Manifest shape (mirrors the ``mcpServerContribution`` descriptor —
``name`` + ``command``/``args?``/``env?`` — rather than inventing a new one;
``daemons`` is an ADDITIVE contribution point, tolerated by the SSOT schema's
``contributes.additionalProperties: true``, so it never fails the
molecule-core#3383 install/load gates)::

    kind: channel
    contributes:
      daemons:
        - name: bridge
          command: python
          args: ["-m", "lark_channel_molecule.bridge"]
          env:            # optional, overlaid on the workspace process env
            LARK_DOMAIN: feishu
          cwd: "."        # optional, resolved against the plugin dir
                          # (default: the plugin dir itself)

Discovery reads installed plugin manifests via the SAME scan every other
plugin surface uses — :func:`molecule_runtime.plugins.load_plugins`
(per-workspace ``<configs>/plugins`` first, shared ``/plugins`` fallback,
dedup by name, SSOT enforcement applied) — so a daemon-declaring plugin lands
exactly like any other installed plugin. Malformed daemon entries are SKIPPED
with a loud log line; they never crash boot and never fail manifest
validation.

Supervision is deliberately minimal (issue #215 constraint 4: "spawn +
restart + env injection. Nothing else"): one monitor thread per daemon
(threads, matching the heartbeat/poller convention — daemon liveness must not
depend on event-loop health), each child in its own session/process group,
exponential restart backoff (1s → 2s → 4s … cap 60s), a circuit breaker after
10 consecutive fast failures, and SIGTERM-then-SIGKILL group termination on
``stop()``. Failures are logged, never fatal — the daemon is auxiliary to the
agent; the workspace keeps serving without it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from molecule_runtime import plugin_settings

logger = logging.getLogger(__name__)


@dataclass
class DaemonSpec:
    """A single spawnable daemon resolved from a plugin manifest."""

    name: str
    command: list[str]  # full argv ([command, *args] from the manifest entry)
    plugin: str = ""  # owning plugin name (log context; PR-2 identity anchor)
    kind: str = ""  # owning manifest kind; "channel" + "trigger" get a local A2A lane
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # Parsed ``contributes.state`` (RFC sdk#181). None == the plugin declared no
    # durable state. Deliberately NOT kind-gated: a skill plugin may keep state
    # just as a channel plugin does. All of a plugin's daemons share ONE
    # directory — they are one plugin and one trust domain.
    state: dict | None = None

    @property
    def key(self) -> str:
        """Stable supervisor key — namespaced by plugin so two plugins may
        both declare a daemon called ``bridge``."""
        return f"{self.plugin}/{self.name}" if self.plugin else self.name


def daemon_specs_from_manifest(
    plugin_name: str,
    plugin_path: str,
    raw_daemons: object,
    plugin_kind: str = "",
    settings: dict | None = None,
    settings_file: str | None = None,
    raw_state: object = None,
) -> list[DaemonSpec]:
    """Parse a manifest's ``contributes.daemons`` value into specs.

    Malformed input is SKIPPED with a warning — per entry where possible,
    wholesale when ``daemons`` itself isn't a list. Never raises: a broken
    daemon declaration must not take down boot (the plugin's other
    contributions already loaded normally).
    """
    state = _normalize_state(plugin_name, raw_state)
    if raw_daemons is None:
        return []
    if not isinstance(raw_daemons, list):
        logger.warning(
            "plugin %s: contributes.daemons is %s, expected a list — ignoring",
            plugin_name, type(raw_daemons).__name__,
        )
        return []
    specs: list[DaemonSpec] = []
    for entry in raw_daemons:
        problem = _entry_problem(entry)
        if problem:
            logger.warning(
                "plugin %s: skipping daemon entry %r: %s",
                plugin_name, entry, problem,
            )
            continue
        cwd = entry.get("cwd")
        if cwd is not None:
            # relative cwd is plugin-dir-relative; default is the plugin dir
            cwd = os.path.join(plugin_path, str(cwd))
        else:
            cwd = plugin_path
        specs.append(
            DaemonSpec(
                name=entry["name"],
                plugin=plugin_name,
                kind=plugin_kind,
                command=[entry["command"], *entry.get("args", [])],
                # Per-install plugin settings: ${config:key} in the manifest's
                # own env map resolves from the delivered settings file. This is
                # the zero-code-change adoption path — a plugin that already
                # reads an env var keeps working and simply sources it from
                # settings. settings=None leaves the map untouched, so every
                # existing plugin is byte-identical.
                env=(
                    plugin_settings.apply_to_env(
                        {k: str(v) for k, v in (entry.get("env") or {}).items()},
                        settings,
                        settings_file,
                    )
                    if settings is not None
                    else {k: str(v) for k, v in (entry.get("env") or {}).items()}
                ),
                cwd=cwd,
                state=state,
            )
        )
    return specs


def _normalize_state(plugin_name: str, raw_state: object) -> dict | None:
    """Coerce a manifest's ``contributes.state`` into a spec-ready mapping.

    Skip-not-reject, exactly like :func:`_entry_problem`: a non-mapping value is
    still a DECLARATION that the plugin keeps durable state, so it is coerced to
    the default posture and logged rather than dropped or raised on. Dropping it
    would silently deny the plugin a state dir over a typo — the same class of
    silent failure #360 was about — and raising would fail manifest validation,
    which the contract forbids (``never_fail_manifest_validation``).
    """
    if raw_state is None:
        return None
    if isinstance(raw_state, dict):
        return raw_state
    logger.warning(
        "plugin %s: contributes.state is %s, expected a mapping — treating it "
        "as a bare declaration (durability=required)",
        plugin_name, type(raw_state).__name__,
    )
    return {"durability": "required"}


def _entry_problem(entry: object) -> str | None:
    """Return a human-readable reason the entry is malformed, or None."""
    if not isinstance(entry, dict):
        return "not a mapping"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return "missing/empty name"
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return "missing/empty command"
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "args must be a list of strings"
    env = entry.get("env")
    if env is not None and not isinstance(env, dict):
        return "env must be a mapping"
    return None


def discover_daemon_specs(
    workspace_plugins_dir: str | None = None,
    shared_plugins_dir: str | None = None,
    loaded=None,
) -> list[DaemonSpec]:
    """Collect daemon specs from every installed plugin's manifest.

    Reuses :func:`plugins.load_plugins` (the canonical installed-plugin scan)
    so priority/dedup/SSOT-enforcement semantics are identical to skills,
    rules, and MCP contributions — no parallel discovery convention. Pass a
    pre-loaded ``LoadedPlugins`` (``loaded=``) to reuse a single boot-time scan
    across callers (e.g. the schedule seeder) instead of re-scanning disk.
    """
    if loaded is None:
        from molecule_runtime.plugins import load_plugins

        loaded = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=shared_plugins_dir
            or os.environ.get("PLUGINS_DIR", "/plugins"),
        )
    specs: list[DaemonSpec] = []
    channel_identity_paths: dict[str, str] = {}
    daemon_keys: set[str] = set()
    for plugin in loaded.plugins:
        raw = plugin.manifest.contributes.get("daemons")
        # Channel provenance is the validated manifest identity, not the
        # checkout/install directory (which is commonly the repository name
        # and may differ after a rename). Keep the established directory-name
        # identity for generic daemons, whose supervisor keys predate this API.
        daemon_owner = str(
            plugin.manifest.name
            if plugin.manifest.kind in ("channel", "trigger")
            else plugin.name
        ).strip()
        # Layer 1 (declared defaults) comes from the manifest ON THE BOX; layers
        # 2-5 arrive as the delivered settings file. The runtime is the only
        # side that holds both, which is why resolution happens here.
        # Opt-in: only plugins that declare configuration (or have a delivered
        # file) get settings resolution. Everything else keeps a byte-identical
        # daemon env.
        if plugin_settings.has_settings(plugin.name, plugin.manifest.contributes):
            resolved = plugin_settings.resolve(plugin.name, plugin.manifest.contributes)
            resolved_file = plugin_settings.settings_path(plugin.name)
        else:
            resolved = None
            resolved_file = None
        plugin_specs = daemon_specs_from_manifest(
            daemon_owner,
            plugin.path,
            raw,
            plugin_kind=plugin.manifest.kind,
            settings=resolved,
            settings_file=resolved_file,
            # Kind-agnostic BY DESIGN — read straight off contributes, with no
            # `kind in {channel, trigger}` gate. Gating it would rebuild exactly
            # the trigger-only limitation this contract generalises away.
            # `daemon_owner` is the same validated-manifest-name identity the
            # channel-provenance path uses, which is the contract's
            # `identity_source` — so there is no second identity convention.
            raw_state=plugin.manifest.contributes.get("state"),
        )
        if plugin_specs and plugin.manifest.kind == "channel":
            prior_path = channel_identity_paths.get(daemon_owner)
            if prior_path is not None and prior_path != plugin.path:
                raise ValueError(
                    f"duplicate channel plugin identity {daemon_owner!r}: "
                    f"{prior_path!r} and {plugin.path!r}"
                )
            channel_identity_paths[daemon_owner] = plugin.path
        for spec in plugin_specs:
            if spec.key in daemon_keys:
                raise ValueError(f"duplicate plugin daemon key {spec.key!r}")
            daemon_keys.add(spec.key)
        specs.extend(plugin_specs)
    if specs:
        logger.info(
            "discovered %d plugin daemon(s): %s",
            len(specs), ", ".join(s.key for s in specs),
        )
    return specs


# Boot-time signal: set to "1" by the boot seam when a trigger plugin is
# present, read by the heartbeat to advertise the ``scheduler`` capability
# (G2). A process-lifetime fact — plugins don't change mid-run — so a single
# env flag is authoritative and avoids re-scanning on every heartbeat.
NATIVE_SCHEDULER_ENV = "MOLECULE_RUNTIME_NATIVE_SCHEDULER"


def has_trigger_daemon(specs: "Iterable[DaemonSpec]") -> bool:
    """True iff any discovered daemon spec is a ``kind: trigger`` daemon.

    A present trigger plugin means this workspace schedules natively, so the
    platform's central scheduler must DEFER for it (G2 of the scheduler-as-
    trigger-plugin refactor) — otherwise both would fire and the agent would be
    double-triggered. The runtime advertises this via the ``scheduler``
    capability in its heartbeat; the platform reads it in NativeSchedulerCheck.
    """
    return any(
        str(getattr(s, "kind", "") or "").strip() == "trigger" for s in specs
    )


class DaemonSupervisor:
    """Minimal lifecycle supervisor for plugin daemons.

    ``start()`` spawns one monitor thread per spec and returns immediately
    (never blocks boot). Each child runs in its own session/process group and
    inherits the workspace process env overlaid with ``spec.env`` — so the
    workspace token, platform URL, and pulled secrets flow through exactly as
    they do to every other child process. ``stop()`` SIGTERMs every live
    group, escalates to SIGKILL after ``term_grace_seconds``, and joins the
    monitors — daemons die with the workspace.
    """

    def __init__(
        self,
        specs: list[DaemonSpec],
        *,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 60.0,
        max_fast_failures: int = 10,
        fast_failure_seconds: float = 30.0,
        term_grace_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
    ):
        self.specs = list(specs)
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.max_fast_failures = max_fast_failures
        self.fast_failure_seconds = fast_failure_seconds
        self.term_grace_seconds = term_grace_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._procs: dict[str, subprocess.Popen] = {}  # live children only
        # observable per-daemon state: starting|running|stopped|failed
        self.states: dict[str, str] = {}
        self.restart_counts: dict[str, int] = {}

    # -- boot -------------------------------------------------------------
    def start(self) -> None:
        """Spawn a monitor thread per daemon; returns immediately."""
        for spec in self.specs:
            if spec.key in self._threads:
                continue  # idempotence — never double-supervise
            self.states[spec.key] = "starting"
            self.restart_counts[spec.key] = 0
            thread = threading.Thread(
                target=self._monitor,
                args=(spec,),
                name=f"plugin-daemon:{spec.key}",
                daemon=True,
            )
            self._threads[spec.key] = thread
            thread.start()

    def supervise(self, new_specs: "Iterable[DaemonSpec]") -> list[DaemonSpec]:
        """Add daemons to a RUNNING supervisor and start monitors for them.

        The hot-install counterpart to ``start()``: when a ``kind: trigger`` (or
        channel) plugin is installed AFTER boot, its daemon must come up without
        restarting the whole workspace (the daemon lifecycle otherwise only runs
        at boot). Appends specs whose ``key`` is not already supervised, then
        calls ``start()`` — which is idempotent per key, so it spawns a monitor
        for each newly-added daemon and leaves existing ones untouched. Returns
        the specs actually added (empty when every key was already supervised, so
        the caller can treat a no-op reload as such). A stopped supervisor adds
        nothing — daemons are shutting down."""
        if self._stop.is_set():
            return []
        with self._lock:
            existing = {s.key for s in self.specs}
            added = [
                s for s in new_specs
                if s.key not in existing and s.key not in self._threads
            ]
            self.specs.extend(added)
        if added:
            self.start()
        return added

    # -- per-daemon monitor loop ------------------------------------------
    def _monitor(self, spec: DaemonSpec) -> None:
        key = spec.key
        backoff = self.backoff_base_seconds
        fast_failures = 0
        while not self._stop.is_set():
            started_at = time.monotonic()
            proc = self._spawn(spec)
            if proc is not None:
                with self._lock:
                    self._procs[key] = proc
                self.states[key] = "running"
                # wait for exit or supervisor stop
                while proc.poll() is None:
                    if self._stop.wait(self.poll_interval_seconds):
                        self.states[key] = "stopped"
                        # normally stop() has already snapshotted+terminated
                        # this child; the atomic pop below only wins for one
                        # registered AFTER stop()'s snapshot (spawn racing
                        # stop) — the monitor reaps that straggler itself.
                        with self._lock:
                            straggler = self._procs.pop(key, None)
                        if straggler is not None and straggler.poll() is None:
                            self._terminate(key, straggler)
                        return
                with self._lock:
                    self._procs.pop(key, None)
                lifetime = time.monotonic() - started_at
                logger.warning(
                    "plugin daemon %s: exited rc=%s after %.1fs",
                    key, proc.returncode, lifetime,
                )
                if lifetime >= self.fast_failure_seconds:
                    fast_failures = 0  # it ran properly — fresh slate
                    backoff = self.backoff_base_seconds
                else:
                    fast_failures += 1
            else:
                fast_failures += 1  # unspawnable counts as a fast failure
            if fast_failures >= self.max_fast_failures:
                self.states[key] = "failed"
                logger.error(
                    "plugin daemon %s: circuit breaker tripped — %d "
                    "consecutive fast failures (< %.0fs each); giving up "
                    "until the next workspace boot",
                    key, fast_failures, self.fast_failure_seconds,
                )
                return
            if self._stop.is_set():
                self.states[key] = "stopped"
                return
            self.restart_counts[key] += 1
            logger.warning(
                "plugin daemon %s: restart #%d in %.1fs",
                key, self.restart_counts[key], backoff,
            )
            if self._wait_backoff(backoff):
                self.states[key] = "stopped"
                return
            backoff = min(backoff * 2, self.backoff_cap_seconds)

    def _wait_backoff(self, seconds: float) -> bool:
        """Sleep the restart backoff; True when stop was requested.
        Seam for tests to record/skip the waits deterministically."""
        return self._stop.wait(seconds)

    def _spawn(self, spec: DaemonSpec) -> subprocess.Popen | None:
        """Popen the daemon in its own session/pgroup; None on failure."""
        # The local A2A socket is a runtime-issued capability, not an ordinary
        # workspace env var.  Never inherit a stale/operator-supplied parent
        # value; ChannelEventSocketManager publishes authoritative values into
        # spec.env only after its private listener is bound and chmodded.
        from molecule_runtime.channel_events import (
            CHANNEL_A2A_SOCKET_ENV,
            CHANNEL_A2A_TOKEN_ENV,
            CHANNEL_API_VERSION_ENV,
            CHANNEL_PLUGIN_ID_ENV,
            TRIGGER_A2A_SOCKET_ENV,
            TRIGGER_A2A_TOKEN_ENV,
            TRIGGER_API_VERSION_ENV,
            TRIGGER_PLUGIN_ID_ENV,
        )
        from molecule_runtime import plugin_state

        child_env = dict(os.environ)
        for reserved in (
            CHANNEL_API_VERSION_ENV, CHANNEL_A2A_SOCKET_ENV,
            CHANNEL_A2A_TOKEN_ENV, CHANNEL_PLUGIN_ID_ENV,
            TRIGGER_API_VERSION_ENV, TRIGGER_A2A_SOCKET_ENV,
            TRIGGER_A2A_TOKEN_ENV, TRIGGER_PLUGIN_ID_ENV,
            # The per-plugin state dir + its honest durability flag are
            # runtime-issued, exactly like the A2A socket above. Stripping them
            # from the inherited environment is what stops a stale or
            # operator-supplied value reaching a daemon we did not resolve one
            # for. Names come from the contract, never re-typed here.
            *plugin_state.reserved_env_names(),
        ):
            child_env.pop(reserved, None)
        child_env.update(spec.env)
        # AFTER the manifest's own env map, deliberately: a plugin must not be
        # able to name, forge or redirect its own state dir. `contributes.state`
        # has no path field and this overwrite is what enforces that — the
        # directory is keyed on the validated manifest name, and the untrusted
        # side gets no say in it.
        if spec.state is not None:
            state_env = plugin_state.state_env_for(spec.plugin or spec.name)
            child_env.update(state_env)
            if state_env:
                resolved_dir = state_env.get(plugin_state.STATE_DIR_ENV or "", "")
                plugin_state.log_degradation(
                    spec.plugin or spec.name,
                    state_env.get(plugin_state.DURABLE_ENV or "")
                    == plugin_state.DURABLE_TRUE,
                    Path(resolved_dir),
                    spec.state,
                )
        try:
            proc = subprocess.Popen(
                spec.command,
                env=child_env,
                cwd=spec.cwd or None,
                start_new_session=True,  # own pgroup — group kill reaps grandchildren
            )
        except Exception as e:  # noqa: BLE001 — supervisor must survive any spawn error
            logger.error("plugin daemon %s: spawn failed: %s", spec.key, e)
            return None
        logger.info(
            "plugin daemon %s: started pid=%d cmd=%s",
            spec.key, proc.pid, spec.command,
        )
        return proc

    def _terminate(self, key: str, proc: subprocess.Popen) -> None:
        """TERM the group, escalate to KILL after the grace window."""
        self._signal_group(proc, signal.SIGTERM, key)
        try:
            proc.wait(timeout=self.term_grace_seconds)
        except subprocess.TimeoutExpired:
            self._signal_group(proc, signal.SIGKILL, key)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                logger.error("plugin daemon %s: unkillable?", key)

    # -- shutdown ----------------------------------------------------------
    def stop(self) -> None:
        """Terminate all daemons (SIGTERM, SIGKILL after grace) and join.
        Idempotent; never raises."""
        self._stop.set()
        with self._lock:
            procs = dict(self._procs)
            self._procs.clear()
        for key, proc in procs.items():
            self._signal_group(proc, signal.SIGTERM, key)
        deadline = time.monotonic() + self.term_grace_seconds
        for key, proc in procs.items():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "plugin daemon %s: did not exit within %.0fs of SIGTERM "
                    "— SIGKILLing the process group",
                    key, self.term_grace_seconds,
                )
                self._signal_group(proc, signal.SIGKILL, key)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover — kernel-level oddity
                    logger.error("plugin daemon %s: unkillable?", key)
        for thread in self._threads.values():
            thread.join(timeout=self.poll_interval_seconds * 4 + 1)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int, key: str) -> None:
        """Signal the child's whole process group; fall back to the child."""
        try:
            os.killpg(proc.pid, sig)  # start_new_session=True → pgid == pid
        except ProcessLookupError:
            pass  # already gone
        except Exception as e:  # noqa: BLE001 — e.g. EPERM; still try the child itself
            logger.warning(
                "plugin daemon %s: group signal %s failed (%s) — "
                "signalling the child directly", key, sig, e,
            )
            try:
                proc.send_signal(sig)
            except Exception:  # noqa: BLE001
                pass


async def wait_until_server_bound(
    server,
    *,
    max_wait: float = 60.0,
    poll_interval: float = 0.25,
) -> bool:
    """Wait until ``uvicorn.Server`` reports its socket bound, FAIL-OPEN.

    ``uvicorn.Server`` flips ``started`` True once its socket is bound and
    accepting connections. This is the single "is uvicorn bound?" gate shared
    by every post-bind action (poll-delivery, daemon supervisor, initial
    prompt) so they answer that question identically instead of drifting apart
    (one of them once self-polled the agent-card over HTTP and FAIL-CLOSED
    dropped the work on timeout).

    Returns whether the server became bound within ``max_wait``. Crucially the
    caller must proceed EITHER way: ``max_wait`` is a defensive backstop, not a
    verdict — if startup somehow never reports bound the caller should still do
    its work (its own retry/backoff is the net) rather than silently drop it.
    A ``None``/attribute-less ``server`` is treated as never-bound (returns
    False after the wait) so a mis-wired caller degrades to fail-OPEN, never a
    crash.
    """
    waited = 0.0
    while not getattr(server, "started", False):
        if waited >= max_wait:
            return False
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    return True


async def start_supervisor_when_bound(
    server,
    supervisor,
    *,
    event_transport=None,
    poll_interval: float = 0.25,
    max_wait_seconds: float = 60.0,
) -> bool:
    """Start the daemon supervisor, but ONLY after uvicorn has bound.

    Daemons exist to talk to this workspace (a channel bridge posts inbound
    turns at the local A2A server), so spawning before the socket listens
    would race the bind — same gate as ``start_poll_delivery_when_bound``.
    ``max_wait_seconds`` is the same defensive backstop: a stalled startup
    still starts the daemons (their own retry loops are the net) rather than
    never starting them.
    """
    bound = await wait_until_server_bound(
        server, max_wait=max_wait_seconds, poll_interval=poll_interval
    )
    if not bound:
        logger.warning(
            "plugin daemons: uvicorn not reported bound after %.0fs; "
            "starting daemons anyway", max_wait_seconds,
        )
    # PR-2: bind the existing A2A app on the private per-plugin socket before
    # any channel daemon starts. A bind failure withholds the channel
    # capability but does not block generic daemon supervision or agent boot.
    if event_transport is not None:
        try:
            await event_transport.start()
        except Exception as e:  # noqa: BLE001 — daemon supervision + agent boot survive
            logger.error(
                "plugin daemons: local channel event socket unavailable (%s); "
                "starting without the local capability",
                e,
            )
            event_transport.clear_daemon_env()
    supervisor.start()
    return True
