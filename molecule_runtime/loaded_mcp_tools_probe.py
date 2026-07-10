"""Init-time enumeration of the connected MCP servers' tool inventory (core#3082).

WHY this exists (the gap this closes)
-------------------------------------
``platform_agent_identity.mcp_server_present()`` proves the management MCP is
*declared* in the active runtime's native config. The platform's online/degraded
gate (core ``registry.go``) additionally wants to know the management MCP's tools
were *actually loaded* — a declared-but-dead server is the exact false-green
#3082 catches. The runtime reports that via ``loaded_mcp_tools`` on the
register/heartbeat payload (``platform_agent_identity.set_loaded_mcp_tools``).

Before this module, the ONLY producer of that signal was the claude-code
executor's per-turn ``init`` system-message capture (in the separate template
repo). That means a freshly-provisioned concierge that NO ONE has chatted with
sits ``degraded`` indefinitely (``loaded_mcp_tools()`` stays ``None`` →
heartbeat omits the field → gate fail-closes). That is task #85's open gap.

WHAT this does (mirrors the adk#34 / codex#142 prior art)
---------------------------------------------------------
Like codex#142, this enumerates the LOADED tool inventory AT INIT — independent
of any user turn — from the active adapter's declared MCP servers (the adapter
reads its OWN native config — ADR-004 relocated per-runtime config-reading into
the adapter; the engine no longer resolves servers by runtime name) and, for each,
spawning it as a stdio subprocess and performing the minimal MCP JSON-RPC
handshake:

    initialize  ->  notifications/initialized  ->  tools/list

Each returned ``tools[].name`` is normalized to ``mcp__<server>__<tool>`` (the
canonical dispatcher id the core gate keys on, e.g.
``mcp__molecule-platform__create_workspace``), deduped and sorted. The result is
published via ``set_loaded_mcp_tools(...)`` so the very FIRST heartbeat carries
the field and the gate can flip ``degraded -> online`` without waiting for a
turn.

This is runtime-agnostic: it works for claude-code, codex, and openclaw (every
runtime whose native config the readers understand) because it talks the MCP
wire protocol directly rather than depending on any one SDK's tool-list message.

BOOT-SAFETY CONTRACT (the must-fix — this enumeration CANNOT block boot)
-----------------------------------------------------------------------
The init capture runs synchronously on the boot path (``main.py``) BEFORE
``register_with_platform`` and ``heartbeat.start``. The server it spawns —
``npx @molecule-ai/mcp-server`` in management mode — connects to the control
plane at startup, the exact dependency that stalls when the CP is slow or
unreachable during an incident. A server that answers ``initialize`` then never
answers ``tools/list`` (holding stdout open, writing nothing) must NOT hang boot.

So the whole probe is built on ``asyncio``:
  * ``asyncio.create_subprocess_exec`` spawns each server.
  * EVERY stdout read is wrapped in ``asyncio.wait_for(reader.readline(),
    timeout=_MCP_READ_TIMEOUT_SECONDS)`` — a per-read I/O deadline. A stalled
    server trips the read timeout instead of blocking forever.
  * Each server also has a HARD per-server deadline
    (``_MCP_HANDSHAKE_TIMEOUT_SECONDS``) enforced via ``asyncio.wait_for`` around
    the whole handshake; on timeout the subprocess is killed.
  * The whole enumeration is bounded by a HARD overall deadline
    (``_MCP_ENUMERATION_TIMEOUT_SECONDS``) via ``asyncio.wait_for`` so even a
    pathological fleet of stalling servers cannot stall boot past that bound.
On ANY timeout/stall/error the subprocess is killed and that server contributes
nothing. The sync entry points run this async core under a hard wall-clock bound
(``asyncio.run`` of a ``wait_for``-wrapped coroutine), so a caller on a
non-async path is equally protected.

DEGRADE-SAFE CONTRACT (REQUIREMENT 3)
-------------------------------------
  * A broken / misconfigured / slow / STALLING server is treated as NOT loaded,
    never an exception into boot: every spawn + handshake is wrapped, a failure
    yields no ids for that server and we move on.
  * If NO server could be enumerated at all (all failed, or none declared on a
    non-platform runtime, or the runtime config is unreadable), we leave the
    producer ``None`` — the heartbeat then OMITS ``loaded_mcp_tools`` and core's
    90s grace window applies (fail-closed/degrade), exactly as before this hook.
    We NEVER publish a guessed/static list to paper over a failure.
  * An empty list ``[]`` is published ONLY when a server genuinely connected and
    advertised zero MCP tools — a meaningful "connected-but-no-tools" signal,
    distinct from "never observed" (``None``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle at runtime (adapter_base imports this module)
    from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter

logger = logging.getLogger("platform-agent.identity")

# Per-read I/O budget. EVERY stdout readline is wrapped in asyncio.wait_for with
# this timeout, so a server that answers `initialize` then stalls on `tools/list`
# (writes nothing, holds stdout open) trips this deadline instead of blocking
# the boot path forever. This is the core of the boot-safety fix.
_MCP_READ_TIMEOUT_SECONDS = 10.0

# Per-server handshake budget (a HARD ceiling over the whole
# initialize -> tools/list dance for one server, enforced via asyncio.wait_for).
# The slow path is the platform MCP's npx/node cold start + connect; kept tight
# so a single dead/stalling server can't stall boot for long. Mirrors codex#142's
# _MCP_HANDSHAKE_TIMEOUT.
_MCP_HANDSHAKE_TIMEOUT_SECONDS = 20.0

# Overall enumeration budget. Even a whole fleet of stalling servers cannot stall
# boot past this bound: the entire enumeration coroutine is wrapped in
# asyncio.wait_for(..., _MCP_ENUMERATION_TIMEOUT_SECONDS). Servers are probed
# sequentially (the management MCP is the only one that matters for the gate), so
# this is sized as a small multiple of the per-server budget.
_MCP_ENUMERATION_TIMEOUT_SECONDS = 45.0

# MCP protocol version we advertise on initialize. Matches the codex prior art
# and the version the @molecule-ai/mcp-server server speaks.
_MCP_PROTOCOL_VERSION = "2024-11-05"


# ── #1027 CRITICAL launch-failure alarm (GUARD D, task #229 / #228) ─────────
# The DEGRADE-SAFE contract above deliberately absorbs a *transient stall* (a
# server that spawns but is slow/unreachable) into core's grace window. But a
# HARD LAUNCH-FAILURE is a different animal and must NOT be treated the same:
# when the platform-mcp plugin pins @molecule-ai/mcp-server@<PIN> AHEAD of the
# version pre-baked into this image, the runtime's
#   npx --prefer-offline @molecule-ai/mcp-server@<PIN>
# MISSES the cache and cold-pulls <PIN>; under CF-WAF throttling that hard-fails
# `ETARGET` and the child process EXITS NON-ZERO before answering a single MCP
# message. That is deterministic — it will NEVER self-heal on this image — so
# silently degrading for the whole grace window (and then flapping) is exactly
# the #228 false-ready. Instead we (a) emit a LOUD #1027 CRITICAL alarm the
# moment we see it, and (b) record a REFUSE-ONLINE reason so the boot/heartbeat
# path can fail-closed loudly instead of waiting the grace window out.
#
# stderr is captured (PIPE, not DEVNULL) so npm/npx's error code is visible;
# these are the signatures that mark "the server binary died before handshaking
# because its version could not be resolved/executed", not "slow control plane".
_LAUNCH_FAILURE_SIGNATURES = (
    "ETARGET", "ENOTCACHED", "ERESOLVE", "E404",
    "no matching version", "could not determine executable",
    "npm error", "npm ERR!",
)

# Runtime-side REFUSE-ONLINE signal. Non-None once a hard MCP launch-failure has
# been observed on this image; consulted by the boot/heartbeat path to fail
# closed LOUDLY rather than sit degraded for the grace window.
_launch_failure_reason = None  # type: str | None


def record_launch_failure(reason):
    """Set (or clear, with None) the last hard MCP launch-failure reason."""
    global _launch_failure_reason
    _launch_failure_reason = reason


def launch_failure_reason():
    """Return the last hard MCP launch-failure reason (e.g. ``npx ETARGET``), or
    None. A non-None value means the management MCP could NOT be launched AT ALL
    on this image (its pinned version is unresolvable), so the concierge must
    REFUSE to report online — fail-closed loudly — rather than absorb it into the
    degrade grace window (#228). Idempotent read; ``record_launch_failure(None)``
    clears it (used by tests / a successful re-probe)."""
    return _launch_failure_reason


def _classify_launch_failure(returncode, stderr_text):
    """Classify a completed child as a hard launch-failure, or None if it isn't.

    A launch-failure is a child that EXITED NON-ZERO (returncode not None and
    != 0). A still-running child (returncode None — the stall case) is NOT a
    launch-failure and returns None so the grace window keeps handling it. When a
    known npm/npx signature is present it is named in the reason; a bare non-zero
    exit is still reported (the binary died before handshaking) with a truncated
    stderr snippet.
    """
    if returncode is None or returncode == 0:
        return None
    hay = stderr_text or ""
    low = hay.lower()
    for sig in _LAUNCH_FAILURE_SIGNATURES:
        if sig.lower() in low:
            return "exit=%s %s" % (returncode, sig)
    snippet = " ".join(hay.split())[:200]
    return "exit=%s" % returncode + ((" " + snippet) if snippet else "")


async def _maybe_alarm_launch_failure(proc, server: str) -> None:
    """When a handshake yielded None because the child DIED (not stalled), emit
    the #1027 CRITICAL alarm and record the refuse-online reason. No-op for a
    still-running (stalled) child or a clean exit — never a false alarm."""
    if proc is None:
        return
    stderr_text = ""
    try:
        if getattr(proc, "stderr", None) is not None:
            data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
            stderr_text = data.decode("utf-8", "replace") if data else ""
    except Exception:  # noqa: BLE001 — alarm path must never crash boot
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except Exception:  # noqa: BLE001
        pass
    reason = _classify_launch_failure(proc.returncode, stderr_text)
    if reason is None:
        return
    record_launch_failure("%s: %s" % (server, reason))
    logger.critical(
        "#1027 CRITICAL: management MCP %r FAILED TO LAUNCH (%s) — this is a HARD "
        "npx launch-failure (e.g. ETARGET: the platform-mcp plugin pins an "
        "@molecule-ai/mcp-server version AHEAD of the version baked into this "
        "image), NOT a transient control-plane stall. It will not self-heal on "
        "this image, so the concierge must REFUSE online (fail-closed) instead of "
        "sitting degraded for the grace window. FIX: rebuild this runtime image "
        "in lockstep with the plugin pin (Guard D lint) so the pinned mcp-server "
        "is pre-baked.",
        server, reason,
    )


def _normalize_tool_id(server: str, tool_name: str) -> str:
    """Return the canonical ``mcp__<server>__<tool>`` dispatcher id.

    A name already in ``mcp__*`` form (some servers self-namespace) is returned
    unchanged; a bare tool name is prefixed with this server's segment so the
    core gate's ``mcp__molecule-platform__create_workspace`` match works.
    """
    if tool_name.startswith("mcp__"):
        return tool_name
    return f"mcp__{server}__{tool_name}"


def _build_server_command(spec: dict) -> list[str] | None:
    """Resolve ``spec`` -> argv for spawning the server, or None if unspawnable.

    Accepts the runtime-agnostic descriptor shape ``{command, args?, env?}`` (the
    contract entry_shape the renderers consume). Returns None when there is no
    usable ``command`` (e.g. an http/url-transport server we can't stdio-spawn),
    so the caller skips it rather than crashing.
    """
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    argv = [command]
    args = spec.get("args")
    if isinstance(args, list):
        argv.extend(str(a) for a in args)
    return argv


async def _kill_proc(proc) -> None:
    """Best-effort kill of an asyncio subprocess; never raises, never hangs.

    Used on EVERY exit path (success, timeout, stall, error). A stalling server
    that ignores SIGTERM is escalated to SIGKILL, and even the post-kill wait is
    bounded so cleanup itself can't reintroduce a boot hang.
    """
    if proc is None:
        return
    try:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            try:
                # asyncio.TimeoutError is a subclass of Exception (3.11+ it IS
                # TimeoutError), so this also bounds a SIGKILL the OS is slow to
                # reap — cleanup can never reintroduce a boot hang.
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                # Reaped on a best-effort basis; we never block boot on cleanup.
                pass
    except Exception:  # noqa: BLE001
        pass


async def _send_jsonrpc(proc, message: dict) -> None:
    """Write a single newline-delimited JSON-RPC message to the child's stdin."""
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _read_jsonrpc_response(proc, *, expect_id) -> dict | None:
    """Read newline-delimited JSON from the child until the matching id arrives.

    Skips notifications / log lines / non-matching ids. The CRITICAL boot-safety
    property: EVERY ``readline`` is wrapped in ``asyncio.wait_for`` with a
    per-read timeout, so a server that holds stdout open without writing trips the
    read deadline (raising ``asyncio.TimeoutError``) instead of blocking forever.
    Returns None on EOF (child exited) or when the line budget is exhausted.

    Raises ``asyncio.TimeoutError`` on a stalled read so the per-server
    ``asyncio.wait_for`` (or this same exception, caught by the caller) maps the
    server to "not loaded".
    """
    assert proc.stdout is not None
    for _ in range(10_000):
        line_bytes = await asyncio.wait_for(
            proc.stdout.readline(), timeout=_MCP_READ_TIMEOUT_SECONDS
        )
        if line_bytes == b"":  # EOF — child exited
            return None
        line = line_bytes.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # non-JSON log noise on stdout
        if isinstance(msg, dict) and msg.get("id") == expect_id:
            return msg
    return None


async def _handshake(proc, server: str) -> list[str] | None:
    """Drive initialize -> notifications/initialized -> tools/list on a spawned
    server and return its normalized ``mcp__*`` ids (possibly empty), or None
    when the server is broken/stalled (no/invalid response).
    """
    # 1) initialize
    await _send_jsonrpc(proc, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "molecule-runtime-loaded-mcp-probe", "version": "1"},
        },
    })
    init_resp = await _read_jsonrpc_response(proc, expect_id=1)
    if init_resp is None or "result" not in init_resp:
        logger.warning(
            "loaded_mcp_tools: server %r did not return an initialize result — "
            "treating as not-loaded", server,
        )
        return None

    # 2) notifications/initialized (no response expected)
    await _send_jsonrpc(proc, {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    })

    # 3) tools/list — the read here is the stall vector (#3082 must-fix): a
    # server that answered initialize then never writes a tools/list response
    # would, with a blocking readline, hang boot forever. _read_jsonrpc_response
    # wraps it in a per-read asyncio.wait_for, so this returns/raises instead.
    await _send_jsonrpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    list_resp = await _read_jsonrpc_response(proc, expect_id=2)
    if list_resp is None:
        logger.warning(
            "loaded_mcp_tools: server %r did not answer tools/list — "
            "treating as not-loaded", server,
        )
        return None

    result = list_resp.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        # A connected server that returned a malformed tools payload is broken,
        # not "zero tools" — report None so the grace window applies.
        logger.warning(
            "loaded_mcp_tools: server %r tools/list result missing a tools list "
            "— treating as not-loaded", server,
        )
        return None

    ids: list[str] = []
    for t in tools:
        name = t.get("name") if isinstance(t, dict) else None
        if isinstance(name, str) and name:
            ids.append(_normalize_tool_id(server, name))
    logger.info(
        "loaded_mcp_tools: server %r advertised %d tool(s)", server, len(ids),
    )
    return ids


def _merge_launch_env_into_spec(spec: dict, launch_env: dict | None) -> dict:
    """Fold the adapter's ``launch_env`` overlay UNDER a spec's own ``env``.

    Returns a shallow copy of ``spec`` whose ``env`` is
    ``launch_env`` (the adapter overlay, e.g. a PATH carrying the runtime's bundled
    interpreter bin dir) with the spec's declared ``env`` layered ON TOP — so a
    server that pins its own env always wins, while the adapter overlay fills in
    what the spec doesn't set (typically PATH). A no-op returning ``spec`` unchanged
    when ``launch_env`` is falsy. Doing this at the per-server descriptor level (not
    at the spawn seam) keeps the single spawn function's signature stable — the seam
    the SDK conformance suite patches stays adapter-shape-agnostic.
    """
    if not launch_env:
        return spec
    merged = dict(spec)
    env = {str(k): str(v) for k, v in launch_env.items()}
    spec_env = spec.get("env")
    if isinstance(spec_env, dict):
        env.update({str(k): str(v) for k, v in spec_env.items()})  # descriptor wins
    merged["env"] = env
    return merged


async def _list_tools_from_mcp_server(server: str, spec: dict) -> list[str] | None:
    """Spawn one MCP server over stdio, handshake, and return its ``mcp__*`` ids.

    Returns:
        * a list of normalized ``mcp__<server>__<tool>`` ids (possibly empty
          when the server connected but advertised no tools), or
        * ``None`` when the server could not be enumerated (spawn failed,
          handshake timed out/stalled, bad/missing response) — a SIGNAL distinct
          from "connected with zero tools" so the caller can tell "this server is
          broken" from "this server is fine but toolless".

    The adapter's launch-env overlay (``BaseAdapter.mcp_launch_env`` — e.g. a
    ``PATH`` carrying the runtime's bundled interpreter bin dir when it is off the
    system PATH) is already folded into ``spec['env']`` by
    :func:`_merge_launch_env_into_spec` before this is called, so the child inherits
    it here via the normal ``spec.env`` merge below. The single spawn seam therefore
    keeps a stable ``(server, spec)`` signature.

    BOOT-SAFE: the entire spawn+handshake is wrapped in
    ``asyncio.wait_for(..., _MCP_HANDSHAKE_TIMEOUT_SECONDS)`` AND every read is
    independently per-read-timeout bounded; on ANY timeout/stall/error the
    subprocess is killed and we return None. Never raises.
    """
    argv = _build_server_command(spec)
    if argv is None:
        logger.info(
            "loaded_mcp_tools: server %r has no stdio command (spec keys=%s) — skipping",
            server, sorted(spec.keys()),
        )
        return None

    # Child env = our env overlaid with the server's declared env (str->str). The
    # adapter's launch-env overlay is already inside spec['env'] (folded UNDER the
    # descriptor by _merge_launch_env_into_spec), so this one merge carries it.
    child_env = dict(os.environ)
    spec_env = spec.get("env")
    if isinstance(spec_env, dict):
        for k, v in spec_env.items():
            child_env[str(k)] = str(v)

    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Capture stderr (was DEVNULL) so a hard npx launch-failure's
                # error code (ETARGET, ENOTCACHED, …) is visible to the #1027
                # alarm classifier below. npx's failure output is tiny and the
                # child exits fast, so the PIPE never back-pressures the handshake.
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "loaded_mcp_tools: failed to spawn server %r (%s) — treating as not-loaded",
                server, exc,
            )
            # The binary itself was not runnable (missing/exec error): a hard
            # launch-failure, not a stall. Alarm loudly + refuse-online.
            record_launch_failure("%s: spawn error: %s" % (server, exc))
            logger.critical(
                "#1027 CRITICAL: management MCP %r could not be SPAWNED (%s) — hard "
                "launch-failure; concierge must REFUSE online (Guard D / #228).",
                server, exc,
            )
            return None

        # HARD per-server deadline over the whole handshake. A server that
        # answers initialize then stalls on tools/list trips either the per-read
        # timeout (inside _handshake) or this outer ceiling, whichever first.
        result = await asyncio.wait_for(
            _handshake(proc, server), timeout=_MCP_HANDSHAKE_TIMEOUT_SECONDS
        )
        # A None result means the handshake produced no usable tools/list. That
        # is EITHER a transient stall (child still running — absorbed by the
        # grace window) OR a hard launch-failure (child already EXITED non-zero,
        # e.g. npx ETARGET). Only the latter fires the loud #1027 alarm +
        # refuse-online signal; _maybe_alarm_launch_failure discriminates.
        if result is None:
            await _maybe_alarm_launch_failure(proc, server)
        return result
    except asyncio.TimeoutError:
        logger.warning(
            "loaded_mcp_tools: server %r handshake timed out (per-read %.0fs / "
            "per-server %.0fs) — killing subprocess, treating as not-loaded",
            server, _MCP_READ_TIMEOUT_SECONDS, _MCP_HANDSHAKE_TIMEOUT_SECONDS,
        )
        return None
    except Exception:  # noqa: BLE001 — enumeration must never crash boot
        logger.warning(
            "loaded_mcp_tools: server %r handshake errored — treating as not-loaded",
            server, exc_info=True,
        )
        return None
    finally:
        await _kill_proc(proc)


async def _probe_specs_async(
    servers: dict, launch_env: dict | None = None
) -> list[str] | None:
    """Probe an already-resolved ``{name: spec}`` map, folding the tri-state.

    The generic, runtime-name-free stdio enumeration ENGINE, fed a resolved
    ``{name: spec}`` map by an adapter that read its OWN native MCP config (the
    runtime-owns-discovery contract, :meth:`BaseAdapter.enumerate_loaded_mcp_tools`).
    Given the ``{name: spec}`` map, spawn each STDIO server (each with the adapter's
    ``launch_env`` overlay applied), handshake, and fold the tri-state. Never raises;
    each per-server probe already maps failure to None. HTTP/url specs
    (``_build_server_command`` returns None) are skipped — an adapter that owns an
    HTTP server reports its tools another way.
    """
    if not servers:
        logger.info(
            "loaded_mcp_tools: no MCP servers to probe — leaving producer unset "
            "(grace window applies)",
        )
        return None

    any_connected = False
    collected: set[str] = set()
    for name, spec in servers.items():
        server_spec = spec if isinstance(spec, dict) else {}
        # Fold the adapter's launch-env overlay UNDER this server's own env, so the
        # spawn seam (patched by the SDK conformance suite) keeps a stable signature.
        server_spec = _merge_launch_env_into_spec(server_spec, launch_env)
        ids = await _list_tools_from_mcp_server(name, server_spec)
        if ids is None:
            continue  # broken/unreachable/stalled server — degrade-safe skip
        any_connected = True
        collected.update(ids)

    if not any_connected:
        logger.warning(
            "loaded_mcp_tools: %d declared server(s) but NONE could be enumerated "
            "(all spawn/handshake failures/stalls) — leaving producer unset so the "
            "platform grace window applies", len(servers),
        )
        return None

    result = sorted(collected)
    logger.info(
        "loaded_mcp_tools: enumerated %d MCP tool id(s) from %d declared server(s): %s",
        len(result), len(servers), result,
    )
    return result


async def enumerate_from_specs_async(
    servers: dict, launch_env: dict | None = None
) -> list[str] | None:
    """Boot-safe, bounded, never-raise enumeration of an adapter-supplied specs map.

    The public entry point an adapter override calls when IT read its runtime's
    native MCP config (e.g. the hermes adapter parsing its own config.yaml
    ``mcp_servers`` block). Same overall-deadline + tri-state + never-raise
    guarantees as :func:`enumerate_loaded_mcp_tools_async`; the only difference is
    the servers are supplied by the caller rather than read via the (legacy)
    per-runtime switch.

    ``launch_env`` is the adapter's env overlay
    (:meth:`BaseAdapter.mcp_launch_env`) applied to every spawned server child so a
    runtime bundling its own interpreter off the system PATH can still resolve
    ``npx``/``node`` (default ``None`` = inherit the process env unchanged). It is
    threaded opaquely — the engine spells no runtime name and reads no path from it.
    """
    try:
        return await asyncio.wait_for(
            _probe_specs_async(servers, launch_env),
            timeout=_MCP_ENUMERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "loaded_mcp_tools: overall specs enumeration exceeded %.0fs — leaving "
            "producer unset (grace window applies)", _MCP_ENUMERATION_TIMEOUT_SECONDS,
        )
        return None
    except Exception:  # noqa: BLE001 — enumeration must never crash boot
        logger.warning(
            "loaded_mcp_tools: specs enumeration errored — leaving producer unset",
            exc_info=True,
        )
        return None


# NOTE (ADR-004): the by-name enumeration entry points
# ``enumerate_loaded_mcp_tools_async(runtime, config_path)`` /
# ``enumerate_loaded_mcp_tools(runtime, config_path)`` and the ``read_mcp_servers``
# indirection over the deleted ``mcp_render.read_mcp_servers_for`` switch are GONE.
# The engine no longer resolves a runtime's declared servers by NAME — the per-
# runtime shape (which native config, which format) moved INTO the adapter. Each
# adapter reads its OWN native config and feeds the resolved ``{name: spec}`` map to
# :func:`enumerate_from_specs_async`, the surviving generic, runtime-name-free,
# boot-safe stdio probe engine (tri-state, bounded, never-raises). The BaseAdapter
# default (``adapter_base.enumerate_loaded_mcp_tools``) does exactly that for a
# not-yet-migrated / third-party adapter using the generic JSON reader.


def _is_platform_agent() -> bool:
    """True when this container should enumerate the management MCP's loaded tools.

    POST-DE-BAKE SIGNAL (the load-bearing fix). "Platform-ness" is a COMPOSITION
    — an ORDINARY runtime image plus the management ``molecule-platform`` MCP
    wired in — NOT a special baked image (rfc-platform-mcp-as-plugin §3.4;
    platform_agent_identity module docstring). The legacy
    ``on_platform_agent_image()`` marker (``MOLECULE_PLATFORM_AGENT_IMAGE_BAKED``)
    is set ONLY by Dockerfile.platform-agent, so it is **FALSE on a de-baked
    concierge** running the standard runtime image. Gating on it ALONE would skip
    enumeration on exactly the concierge this producer exists to fix — the fix
    would ship and silently leave every concierge ``degraded``.

    The de-bake-correct signal is ``mcp_server_present()``: the management
    ``molecule-platform`` MCP is wired into the ACTIVE runtime's native config
    (consulted via the registered probe — same source the online/degraded gate
    uses). It is TRUE for both baked (``/opt`` binary) and de-baked (plugin-wired)
    concierges, and FALSE for ordinary tenants (the plugin is org-root
    entitlement-gated, #50). We OR the legacy marker as belt-and-suspenders for
    the baked->de-baked transition window.

    Defaults to False (skip enumeration) if neither signal can be read.
    """
    try:
        from molecule_runtime.platform_agent_identity import (
            mcp_server_present,
            on_platform_agent_image,
        )

        # Primary: composition signal (management MCP present). Fallback: the
        # legacy baked-image marker. Either is sufficient.
        return on_platform_agent_image() or mcp_server_present()
    except Exception:  # noqa: BLE001
        return False


async def capture_loaded_mcp_tools_at_init(
    adapter: "BaseAdapter",
    config: "AdapterConfig",
    *,
    force: bool = False,
) -> list[str] | None:
    """Enumerate the loaded MCP tool inventory and PUBLISH it to the producer.

    This is the one-call entry point ``main.py`` invokes (awaits) right after the
    executor is created (after MCP wiring, before serving). It is a coroutine
    because the call site is already async; it enumerates and, ONLY when a real
    observation was made (non-``None``), calls ``set_loaded_mcp_tools`` so the
    first heartbeat carries the field. When enumeration yields ``None`` it leaves
    the producer untouched (``None``), preserving the fail-closed grace-window
    semantics.

    RUNTIME-OWNS-DISCOVERY CONTRACT: enumeration is ALWAYS delegated to
    ``adapter.enumerate_loaded_mcp_tools(config)`` — each runtime owns HOW it
    discovers its loaded MCP tools. The ``adapter`` and ``config`` are the only
    inputs needed: ``adapter.name()`` is the runtime and ``config.config_path`` is
    the configs dir, so there is no separate ``runtime``/``config_path`` to thread
    through. The base default reads the standard ``.claude``-style layout via the
    shared core probe (claude/codex/openclaw are byte-identical); hermes overrides
    it to read its own ``config.yaml``.

    kind=platform GATE (REQUIREMENT 3): unless ``force=True`` (tests), the
    enumeration only runs for the concierge (``_is_platform_agent()``). A
    non-platform workspace skips it entirely — no spawn, no enumeration, producer
    stays ``None`` — so tenants declaring MCP servers (e.g. image-gen) don't pay
    the cost and don't amplify the (now-bounded) hang blast-radius.

    Returns the value it observed (the same tri-state as
    :func:`enumerate_from_specs_async`) for logging/testing. Never raises.
    """
    from molecule_runtime.platform_agent_identity import set_loaded_mcp_tools

    if not force and not _is_platform_agent():
        logger.info(
            "loaded_mcp_tools: init capture skipped (kind!=platform) — only the "
            "concierge gates on the management MCP; producer left unset",
        )
        return None

    try:
        observed = await adapter.enumerate_loaded_mcp_tools(config)
    except Exception:  # noqa: BLE001 — must never block boot
        logger.warning(
            "loaded_mcp_tools: init capture errored — leaving producer unset",
            exc_info=True,
        )
        return None

    if observed is not None:
        set_loaded_mcp_tools(observed)
    return observed


async def capture_loaded_mcp_tools_with_retry(
    adapter: "BaseAdapter",
    config: "AdapterConfig",
    *,
    max_attempts: int = 40,
    interval_seconds: float = 2.0,
    max_interval_seconds: float = 30.0,
    backoff_factor: float = 1.7,
    force: bool = False,
) -> list[str] | None:
    """Retry :func:`capture_loaded_mcp_tools_at_init` until it observes tools,
    with DYNAMIC (exponential + jittered) backoff on the real signal.

    WHY (the timing fix). The one-shot init enumeration runs at early boot, but
    the management ``molecule-platform`` MCP isn't necessarily *connectable* yet
    at that moment — the gate signal ``mcp_server_present()`` may not have flipped
    true, and/or the ``npx @molecule-ai/mcp-server`` process is still starting. A
    single attempt therefore often finds nothing and the concierge would sit
    ``provisioning``/``degraded`` until a user turn triggers the per-turn capture.
    This retry waits for the MCP to become ready so a fresh concierge reaches
    ``online`` **without any turn** — the "heartbeat verifies it, not a user"
    behaviour.

    DYNAMIC BACKOFF (not a fixed cadence). With the runtime image now PRE-BAKING
    @molecule-ai/mcp-server into the npm cache, ``npx --prefer-offline`` resolves
    the management MCP from cache with ZERO network pull and the enumeration
    typically succeeds on the FIRST attempt in ~1s. So we poll on a FAST initial
    cadence (``interval_seconds``) to catch that common case immediately, then
    back off EXPONENTIALLY (``backoff_factor``, capped at ``max_interval_seconds``)
    with full jitter — robust against a genuinely-slow connect without hot-spinning
    the enumeration (each attempt spawns the boot-safe probe subprocess). The wait
    is driven by the REAL loaded_mcp_tools signal: it returns the instant a real
    observation lands, never on a wall-clock verdict.

    Each attempt re-evaluates the ``_is_platform_agent()`` gate (so a concierge
    whose MCP is declared a bit late is still picked up) and re-runs the bounded,
    boot-safe enumeration. Returns the observed ids on the first success (the
    producer is set as a side effect), or ``None`` when the attempt budget is
    exhausted — in which case the per-turn capture remains the fallback. This
    coroutine is meant to run as a BACKGROUND task (it must NOT block boot); it
    never raises (a failed attempt is swallowed and retried). NOTE: exhausting the
    attempt budget is NOT a readiness verdict — core owns the readiness terminal
    (health + liveness), so this task simply stops re-probing; it never fails the
    concierge.
    """
    import random

    delay = max(0.0, interval_seconds)
    for attempt in range(1, max_attempts + 1):
        try:
            observed = await capture_loaded_mcp_tools_at_init(
                adapter, config, force=force
            )
        except Exception:  # noqa: BLE001 — a background task must never crash the loop
            logger.warning(
                "loaded_mcp_tools: retry attempt %d errored — will retry", attempt,
                exc_info=True,
            )
            observed = None
        if observed is not None:
            logger.info(
                "loaded_mcp_tools: init enumeration succeeded on attempt %d/%d "
                "(%d tool id(s)) — concierge can reach online without a turn",
                attempt, max_attempts, len(observed),
            )
            return observed
        if attempt < max_attempts:
            # Equal-jitter exponential backoff: sleep [delay/2, delay] so we keep a
            # guaranteed minimum spacing (never hot-spin) while decorrelating
            # concurrent concierges' probes, then grow the delay for the next miss.
            await asyncio.sleep(random.uniform(delay * 0.5, delay) if delay > 0 else 0.0)
            delay = min(max_interval_seconds, delay * backoff_factor)
    logger.info(
        "loaded_mcp_tools: init enumeration attempt budget elapsed (%d attempts, "
        "backoff %.1fs→%.0fs) without a connectable management MCP — producer left "
        "unset; the per-turn capture remains the fallback (core owns the readiness "
        "terminal via health + liveness, so this is not a failure verdict)",
        max_attempts, interval_seconds, max_interval_seconds,
    )
    return None
