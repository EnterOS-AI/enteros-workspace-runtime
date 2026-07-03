"""Plugin-declared channel daemons — manifest-declared long-running sidecars.

PR-1 of issue #215 (daemon lifecycle only). A plugin can declare a
long-running daemon — e.g. a channel bridge like ``lark-channel-molecule`` —
under ``contributes.daemons`` in its ``plugin.yaml``; the workspace runtime
spawns it at boot, restarts it on crash, and kills it with the workspace. The
connected workspace owns its channel processes — no CP supervision domain.
The local event socket / runtime-stamped provenance / turn-complete lane is
PR-2 and intentionally absent here.

Manifest shape (mirrors the ``mcpServerContribution`` descriptor —
``name`` + ``command``/``args?``/``env?`` — rather than inventing a new one;
``daemons`` is an ADDITIVE contribution point, tolerated by the SSOT schema's
``contributes.additionalProperties: true``, so it never fails the
molecule-core#3383 install/load gates)::

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
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DaemonSpec:
    """A single spawnable daemon resolved from a plugin manifest."""

    name: str
    command: list[str]  # full argv ([command, *args] from the manifest entry)
    plugin: str = ""  # owning plugin name (log context; PR-2 identity anchor)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    @property
    def key(self) -> str:
        """Stable supervisor key — namespaced by plugin so two plugins may
        both declare a daemon called ``bridge``."""
        return f"{self.plugin}/{self.name}" if self.plugin else self.name


def daemon_specs_from_manifest(
    plugin_name: str, plugin_path: str, raw_daemons: object
) -> list[DaemonSpec]:
    """Parse a manifest's ``contributes.daemons`` value into specs.

    Malformed input is SKIPPED with a warning — per entry where possible,
    wholesale when ``daemons`` itself isn't a list. Never raises: a broken
    daemon declaration must not take down boot (the plugin's other
    contributions already loaded normally).
    """
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
                command=[entry["command"], *entry.get("args", [])],
                env={k: str(v) for k, v in (entry.get("env") or {}).items()},
                cwd=cwd,
            )
        )
    return specs


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
) -> list[DaemonSpec]:
    """Collect daemon specs from every installed plugin's manifest.

    Reuses :func:`plugins.load_plugins` (the canonical installed-plugin scan)
    so priority/dedup/SSOT-enforcement semantics are identical to skills,
    rules, and MCP contributions — no parallel discovery convention.
    """
    from molecule_runtime.plugins import load_plugins

    loaded = load_plugins(
        workspace_plugins_dir=workspace_plugins_dir,
        shared_plugins_dir=shared_plugins_dir
        or os.environ.get("PLUGINS_DIR", "/plugins"),
    )
    specs: list[DaemonSpec] = []
    for plugin in loaded.plugins:
        raw = plugin.manifest.contributes.get("daemons")
        specs.extend(daemon_specs_from_manifest(plugin.name, plugin.path, raw))
    if specs:
        logger.info(
            "discovered %d plugin daemon(s): %s",
            len(specs), ", ".join(s.key for s in specs),
        )
    return specs


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
        try:
            proc = subprocess.Popen(
                spec.command,
                env={**os.environ, **spec.env},
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


async def start_supervisor_when_bound(
    server,
    supervisor,
    *,
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
    waited = 0.0
    while not getattr(server, "started", False):
        if waited >= max_wait_seconds:
            logger.warning(
                "plugin daemons: uvicorn not reported bound after %.0fs; "
                "starting daemons anyway", max_wait_seconds,
            )
            break
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    supervisor.start()
    return True
