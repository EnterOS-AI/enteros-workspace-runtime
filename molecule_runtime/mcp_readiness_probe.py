"""Active management-MCP readiness probe (core#3082 / runtime#181).

WHY THIS EXISTS
---------------
The controlplane online/degraded gate (molecule-core
``workspace-server/internal/handlers/registry.go``) decides a ``kind=platform``
concierge is healthy — and the companion verified-online work promotes it
``provisioning -> online`` — ONLY when its heartbeat's ``loaded_mcp_tools``
carries ``mcp__molecule-platform__provision_workspace``: proof the management
MCP's tools are actually loadable, not merely *declared* (``mcp_server_present``).

The ORIGINAL producer of that field (``claude_sdk_executor``'s per-turn ``init``
system-message capture -> ``platform_agent_identity.set_loaded_mcp_tools``) only
fires when a TURN runs. So a just-booted or idle concierge never reports the
tools on its own:

  * the #3082 gate false-degrades it after the 180s grace window, and
  * the verified-online gate would HOLD it in 'provisioning' (the "warming"
    state) FOREVER, because the field never arrives.

The init capture is also fragile and racy — the CP had to bolt on a one-shot
"warmup turn" (``registry.go`` ``fireConciergeWarmup``) purely to coax the
capture into firing, and a de-baked concierge whose init-time MCP enumeration
fails reports nothing at all (runtime#181: the producer "had a scaffold but zero
reliable callers").

WHAT THIS DOES
--------------
Makes the signal reliable and turn-INDEPENDENT. On a cadence (a dedicated daemon
thread, NOT the heartbeat POST thread — so a slow spawn never delays the alive
signal) it:

  1. resolves how to launch the declared ``molecule-platform`` MCP server EXACTLY
     as the agent's runtime does — same ``command``/``args``/``env`` from the
     claude ``settings.json`` ``mcpServers`` entry, or the baked-image
     ``molecule-platform-mcp`` command;
  2. spawns it over stdio and performs the MCP JSON-RPC handshake
     (``initialize`` -> ``notifications/initialized``);
  3. issues a **side-effect-FREE** ``tools/list`` call. ``tools/list`` only
     enumerates the registered tool *schemas* — unlike calling
     ``provision_workspace`` it provisions NOTHING;
  4. reports the discovered tool names as ``mcp__molecule-platform__<tool>``
     (the exact id convention the Claude dispatcher uses and the core gate
     matches) via the same ``set_loaded_mcp_tools`` holder the heartbeat already
     reports.

Result: every heartbeat carries the real loaded-tools list within seconds of the
management MCP landing, with no dependency on a turn ever running — the gate sees
``provision_workspace`` and the concierge holds/reaches online.

DESIGN NOTES
------------
* A probe that SUCCEEDS is authoritative: it overwrites the holder with the live
  tool list (even one MISSING provision_workspace — reported truthfully so the
  gate degrades correctly).
* A probe that FAILS (server not declared / not runnable, spawn error,
  handshake/timeout/parse failure) returns ``None`` and the prober leaves the
  holder untouched. A transient miss therefore can't flap the gate — the core
  180s grace window absorbs it, and a genuine "management MCP removed" is caught
  independently by ``mcp_server_present`` going false (hard fail-closed).
* This probe targets the claude-code delivery surfaces (plugin-delivered
  settings.json + baked binary). Other runtimes (codex/openclaw/hermes) keep relying on
  the existing per-turn capture; this is a strict improvement for the dominant
  concierge path, never a regression for the others.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

from molecule_runtime.platform_agent_identity import (
    MANAGEMENT_MCP_COMMAND,
    MANAGEMENT_MCP_NAME,
    MANAGEMENT_MCP_SPEC,
    MANAGEMENT_PROVISION_TOOL_ID,
    MCPSERVERS_KEY,
    PLATFORM_AGENT_IDENTITY_LOGGER,
    SETTINGS_PATH,
    resolve_mcp_launch_env,
    set_loaded_mcp_tools,
    set_mcp_tools_ready,
)

# Share the platform-agent identity logger namespace so a stuck-readiness
# investigation can ``grep platform-agent.identity`` and see BOTH the declared
# (mcp_server_present) and the loaded (this probe) signals side by side.
logger = logging.getLogger(PLATFORM_AGENT_IDENTITY_LOGGER)

# ── MCP JSON-RPC protocol constants ─────────────────────────────────────────
# Newline-delimited JSON-RPC 2.0 over stdio (the MCP stdio transport framing).
_MCP_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "molecule-runtime-readiness-probe", "version": "1"}
_INITIALIZE_ID = 1
_TOOLS_LIST_ID = 2


# ── Cadence / toggle (env-overridable) ──────────────────────────────────────
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


# Steady-state re-verification cadence once the tools have been observed. The
# holder retains the last result between probes, so EVERY heartbeat still reports
# the tools — this only bounds how often we re-confirm.
PROBE_INTERVAL_SECONDS = _env_float("MOLECULE_MCP_PROBE_INTERVAL_SECONDS", 120.0)
# Tighter cadence until the FIRST success, so a concierge whose management MCP is
# delivered a few seconds after boot (post-online plugin reconcile) publishes the
# tools well within the core 180s grace window.
PROBE_RETRY_SECONDS = _env_float("MOLECULE_MCP_PROBE_RETRY_SECONDS", 15.0)
# Hard bound on a single probe (spawn + handshake + tools/list). A cold node
# start is a few seconds; 30s is comfortably longer yet always bounded.
PROBE_TIMEOUT_SECONDS = _env_float("MOLECULE_MCP_PROBE_TIMEOUT_SECONDS", 30.0)

# Kill-switch. Default ON; set to 0/false/no/off to disable (e.g. a runtime that
# wants the legacy per-turn-only behavior).
PROBE_ENABLED_ENV = "MOLECULE_MCP_READINESS_PROBE"


def probe_enabled() -> bool:
    val = os.environ.get(PROBE_ENABLED_ENV, "").strip().lower()
    return val not in ("0", "false", "no", "off")


# ── Launch resolution ───────────────────────────────────────────────────────
def _base_env_with_launch_overlay() -> dict:
    """The child's base env: ``os.environ`` with the active adapter's launch-env
    overlay merged ON TOP (but still UNDER the per-server ``env`` layered by the
    caller — the descriptor always wins).

    This is the readiness-prober analogue of the boot path's
    ``loaded_mcp_tools_probe._merge_launch_env_into_spec``: a runtime that bundles
    its own interpreter OFF the system PATH (e.g. hermes' Node under
    ``$HERMES_HOME/node/bin``) contributes a ``PATH`` overlay via
    ``BaseAdapter.mcp_launch_env`` (registered in main.py, read here via
    ``resolve_mcp_launch_env``), so the spawned ``npx @molecule-ai/mcp-server``
    resolves node/npx. CHILD-ONLY: it returns a COPY of ``os.environ`` — the
    parent process env is never mutated. The overlay is opaque (this function
    reads no runtime name and no path out of it). ``{}`` (the base default, on a
    runtime whose interpreter is already on PATH) is a no-op.
    """
    env = dict(os.environ)
    overlay = resolve_mcp_launch_env()
    if overlay:
        env.update(overlay)  # adapter overlay over os.environ; spec.env still wins
    return env


def _read_settings_entry() -> Optional[dict]:
    """Return the ``molecule-platform`` entry from the claude settings.json, or
    None when it isn't declared / the file is missing or malformed."""
    try:
        with open(SETTINGS_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    servers = data.get(MCPSERVERS_KEY)
    if not isinstance(servers, dict):
        return None
    entry = servers.get(MANAGEMENT_MCP_NAME)
    return entry if isinstance(entry, dict) else None


def resolve_management_launch() -> Optional[tuple[list[str], dict]]:
    """Resolve ``(argv, env)`` to launch the management MCP exactly as the agent
    runtime does. Returns None when neither delivery path yields a runnable
    command (the probe then no-ops and leaves ``loaded_mcp_tools`` untouched).

    Two delivery paths, mirroring ``mcp_server_present``:

      1. plugin-delivered: the ``molecule-platform`` entry in the claude
         ``settings.json`` ``mcpServers`` map carries ``{command, args?, env?}``.
      2. baked image: ``molecule-platform-mcp`` resolves on PATH; launched with
         ``MANAGEMENT_MCP_SPEC``'s env (``MOLECULE_MCP_MODE=management``).

    In both cases the container's own env (``os.environ`` — org key, CP URL, …)
    is the base; the active adapter's ``mcp_launch_env`` overlay (registered in
    main.py, read via ``resolve_mcp_launch_env`` — e.g. a ``PATH`` carrying the
    runtime's bundled interpreter bin dir when it is off the system PATH) layers
    on top of that; and finally the entry/spec env layers ON TOP of the overlay,
    so the descriptor always wins and the probed server is the same management
    surface the agent's live instance loads. This mirrors the boot enumeration
    path (``loaded_mcp_tools_probe``), closing the follow-up #49 gap where the
    heartbeat prober re-failed to resolve ``npx``/``node`` on a runtime whose
    interpreter is off the system PATH (hermes). CHILD-ONLY: the returned env is a
    fresh dict; the parent process env is never mutated.
    """
    entry = _read_settings_entry()
    if entry is not None:
        command = entry.get("command")
        if isinstance(command, str) and command.strip():
            args = entry.get("args")
            arg_list = [str(a) for a in args] if isinstance(args, list) else []
            env = _base_env_with_launch_overlay()
            entry_env = entry.get("env")
            if isinstance(entry_env, dict):
                env.update({str(k): str(v) for k, v in entry_env.items()})
            # Resolve the command on PATH so a bare name (e.g. ``molecule-mcp``)
            # spawns reliably; fall back to the bare name if which() misses.
            argv0 = shutil.which(command) or command
            return [argv0, *arg_list], env

    # Baked-image fallback: the platform-agent image symlinks the management MCP
    # binary onto PATH as ``molecule-platform-mcp``.
    resolved = shutil.which(MANAGEMENT_MCP_COMMAND)
    if resolved:
        env = _base_env_with_launch_overlay()
        spec_env = MANAGEMENT_MCP_SPEC.get("env") or {}
        env.update({str(k): str(v) for k, v in spec_env.items()})
        return [resolved], env

    return None


# ── JSON-RPC exchange (transport-injected → hermetically testable) ──────────
def _await_response(
    read_line: Callable[[float], Optional[str]],
    want_id: int,
    deadline: float,
) -> Optional[dict]:
    """Read newline-delimited JSON-RPC messages until one carries ``id == want_id``.

    Skips notifications, unrelated ids, blank lines, and non-JSON noise (a server
    that stray-logs to stdout instead of stderr). Returns the matching message,
    or None on a JSON-RPC error, timeout, or EOF.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        line = read_line(remaining)
        if line is None:
            return None  # timeout or EOF
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # stray non-JSON line on stdout — ignore
        if not isinstance(msg, dict) or msg.get("id") != want_id:
            continue  # notification / unrelated response — keep reading
        if msg.get("error"):
            return None
        return msg


def exchange_tools_list(
    send_line: Callable[[str], None],
    read_line: Callable[[float], Optional[str]],
    deadline: float,
) -> Optional[list[str]]:
    """Run ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` over an
    injected line transport and return the discovered tool NAMES (un-prefixed),
    or None on any protocol failure.

    Pure (no subprocess) so tests can drive it with in-memory fakes.
    """
    send_line(json.dumps({
        "jsonrpc": "2.0",
        "id": _INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        },
    }))
    if _await_response(read_line, _INITIALIZE_ID, deadline) is None:
        return None

    # The spec requires the initialized notification before further requests.
    send_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    send_line(json.dumps({
        "jsonrpc": "2.0",
        "id": _TOOLS_LIST_ID,
        "method": "tools/list",
        "params": {},
    }))
    resp = _await_response(read_line, _TOOLS_LIST_ID, deadline)
    if resp is None:
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    tools = result.get("tools")
    if not isinstance(tools, list):
        return None
    names: list[str] = []
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def to_tool_ids(names: list[str]) -> list[str]:
    """Map bare management tool names to the ``mcp__<server>__<tool>`` ids the
    Claude dispatcher exposes and the core gate matches on."""
    return [f"mcp__{MANAGEMENT_MCP_NAME}__{n}" for n in names]


def management_provision_ready(tool_ids: Optional[list[str]]) -> bool:
    """True when the probed tool ids expose the required provision_workspace tool
    — i.e. the management MCP is verified-loaded, the signal the gate consumes."""
    return MANAGEMENT_PROVISION_TOOL_ID in (tool_ids or [])


# ── Subprocess transport ────────────────────────────────────────────────────
class _LineReader:
    """Background-thread line pump so reads honor a timeout cross-platform
    (``select`` doesn't work on pipes on Windows). Each ``read_line`` returns the
    next stdout line, or None on timeout or EOF (a None sentinel is enqueued when
    the stream closes)."""

    def __init__(self, stream):
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stream = stream
        self._t = threading.Thread(target=self._pump, name="mcp-probe-reader", daemon=True)
        self._t.start()

    def _pump(self) -> None:
        try:
            for line in self._stream:
                self._q.put(line)
        except Exception:  # noqa: BLE001 — stream closed under us, etc.
            pass
        finally:
            self._q.put(None)  # EOF sentinel

    def read_line(self, timeout: float) -> Optional[str]:
        try:
            return self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None


def _terminate(proc: "subprocess.Popen") -> None:
    """Best-effort teardown of the probed server subprocess."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def probe_management_mcp_tools(timeout: Optional[float] = None) -> Optional[list[str]]:
    """Launch the declared management MCP, ``tools/list`` it, and return the loaded
    ``mcp__molecule-platform__<tool>`` ids.

    Returns:
      * ``list[str]`` on a successful ``tools/list`` (possibly WITHOUT
        provision_workspace if the server genuinely doesn't expose it — reported
        truthfully so the gate degrades correctly). May be empty.
      * ``None`` when the management MCP isn't declared/runnable, or the probe
        fails (spawn / handshake / timeout / parse). None ⇒ "leave the holder
        unchanged."

    Never raises — readiness probing must not crash the caller (the prober loop).
    """
    if timeout is None:
        timeout = PROBE_TIMEOUT_SECONDS
    launch = resolve_management_launch()
    if launch is None:
        logger.debug(
            "mcp readiness probe: management MCP not declared/runnable — skipping"
        )
        return None
    argv, env = launch

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except (OSError, ValueError) as exc:
        logger.warning("mcp readiness probe: failed to spawn %s: %s", argv[0], exc)
        return None

    try:
        reader = _LineReader(proc.stdout)

        def send_line(payload: str) -> None:
            proc.stdin.write(payload + "\n")
            proc.stdin.flush()

        deadline = time.monotonic() + timeout
        names = exchange_tools_list(send_line, reader.read_line, deadline)
        if names is None:
            logger.warning(
                "mcp readiness probe: %s did not return a usable tools/list within %ss",
                MANAGEMENT_MCP_NAME, timeout,
            )
            return None
        return to_tool_ids(names)
    except Exception as exc:  # noqa: BLE001 — never raise into the prober
        logger.warning("mcp readiness probe: exchange error: %s", exc)
        return None
    finally:
        _terminate(proc)


# ── Prober daemon thread ────────────────────────────────────────────────────
class MCPReadinessProber:
    """Daemon thread that periodically probes the management MCP and publishes the
    loaded tool ids via ``set_loaded_mcp_tools`` so the heartbeat reports a
    reliable, turn-INDEPENDENT readiness signal.

    Self-gating: on a runtime that doesn't declare the management MCP, every probe
    returns None (a cheap settings.json read + ``which`` check — NO subprocess
    spawn) and the holder is left untouched, so it is harmless to start
    unconditionally. The retry cadence keeps trying so a management MCP delivered
    AFTER boot (post-online plugin reconcile) is still picked up.
    """

    def __init__(
        self,
        interval: Optional[float] = None,
        retry_interval: Optional[float] = None,
        timeout: Optional[float] = None,
        probe: Optional[Callable[[Optional[float]], Optional[list[str]]]] = None,
    ):
        self._interval = interval if interval is not None else PROBE_INTERVAL_SECONDS
        self._retry = retry_interval if retry_interval is not None else PROBE_RETRY_SECONDS
        self._timeout = timeout if timeout is not None else PROBE_TIMEOUT_SECONDS
        self._probe = probe or probe_management_mcp_tools
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._succeeded_once = False

    def start(self) -> Optional[threading.Thread]:
        if not probe_enabled():
            logger.info(
                "mcp readiness prober: disabled via %s — relying on per-turn capture only",
                PROBE_ENABLED_ENV,
            )
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mcp-readiness-prober", daemon=True
        )
        self._thread.start()
        return self._thread

    def run_once(self) -> Optional[list[str]]:
        """Run a single probe cycle and publish on success. Returns the probe
        result. Exposed for tests + as the loop body."""
        try:
            result = self._probe(self._timeout)
        except Exception:  # noqa: BLE001 — a probe must never kill the loop
            logger.exception("mcp readiness prober: probe raised")
            return None
        if result is not None:
            set_loaded_mcp_tools(result)
            # EV2: publish the POSITIVE readiness event on the FIRST successful
            # tools/list. Turn-independent, so core can flip provisioning->online
            # without the retired fireConciergeWarmup synthetic turn. set_mcp_tools_ready
            # is sticky-once for the first_ready_at stamp, so re-publishing on later
            # probes is harmless.
            set_mcp_tools_ready(True)
            self._succeeded_once = True
            logger.info(
                "mcp readiness prober: published %d management tool(s); "
                "provision_workspace_loaded=%s mcp_tools_ready=True",
                len(result), management_provision_ready(result),
            )
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            # Retry on a tight cadence UNTIL the first success: a management MCP
            # delivered a few seconds after boot (post-online plugin reconcile)
            # is an external settings.json write with no in-process event, so a
            # bounded retry is the correct capture-first mechanism.
            #
            # STOP once we've published at least once. The holder is STICKY —
            # every heartbeat keeps reporting the last observed list between
            # probes — and the only two edges that matter are already covered:
            #   * "added"   → this retry loop catches it (up to first success);
            #   * "removed" → caught INDEPENDENTLY by ``mcp_server_present()``
            #                 going false (hard fail-closed, see DESIGN NOTES).
            # The old steady-state 120s re-probe re-published a signal that only
            # changes on those two edges, cold-spawning npx + a full
            # handshake/tools-list every cycle FOREVER for nothing. Once
            # succeeded, there is nothing left to capture — exit the loop.
            if self._succeeded_once:
                return
            self._stop.wait(self._retry)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5)
        self._thread = None
