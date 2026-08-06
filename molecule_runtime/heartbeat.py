"""Heartbeat loop — alive signal + delegation status checker.

Every 30 seconds:
1. Send heartbeat to platform (alive signal with current_task, error_rate)
2. Check pending delegations — any results back?
3. Store completed delegation results for the agent to pick up

Resilient: recreates HTTP client on failure, auto-restarts on crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from typing import TYPE_CHECKING

import httpx

import molecule_runtime.mailbox_dir as mailbox_dir
from molecule_runtime import kernel
from molecule_runtime.a2a_client import build_message_send_params
from molecule_runtime.platform_agent_identity import identity_gate_payload
from molecule_runtime.a2a_executor import (
    A2A_MESSAGE_SOURCE_TYPE,
    A2A_SOURCE_SELF_HARVESTER,
)
from molecule_runtime.platform_auth import auth_headers, refresh_cache, self_source_headers


def _kernel_allows_autonomous_injection(kind: str) -> bool:
    """Kernel-gated pre-injection circuit-breaker check for an autonomous
    self-wake (MUST-FIX 2).

    Kernel OFF -> always ``True`` (allow), so the self-wake fires EXACTLY as it
    did before the mailbox kernel — byte-identical. Kernel ON -> route through
    :func:`kernel.should_inject_autonomous_turn`, which consults the
    autonomous-loop circuit breaker (:func:`autonomous_loop_guard.should_halt`)
    and returns ``False`` when it is OPEN, so the runaway breaker gates the
    injection BEFORE another replay is enqueued. Fails OPEN (allow) on any
    error — a guard hiccup must never silence the delegation-result harvester.
    """
    try:
        if not mailbox_dir.kernel_enabled():
            return True
        return kernel.should_inject_autonomous_turn(kind)
    except Exception:  # noqa: BLE001
        return True

if TYPE_CHECKING:
    # SSOT typed payloads (molecule-contracts / RFC molecule-core#3285),
    # published as `molecule-ai-contracts` on the gitea PyPI registry and
    # generated from molecule-contracts/workspace-comms. Imported under
    # TYPE_CHECKING ONLY: these are TypedDicts (plain dicts at runtime),
    # `from __future__ import annotations` keeps every annotation a string, and
    # the runtime's `pip install` / wheel smoke-test resolves from plain PyPI
    # (which does not carry this package). So there is NO hard runtime
    # dependency — the wire payloads below stay byte-identical dicts, now
    # checked against the SSOT contract shapes by a type checker for
    # drift-prevention. Install the `contracts` extra (gitea index) to
    # type-check against it locally. This is the SAME pattern molecule-ai-sdk
    # adopted in #36; the runtime is the reference consumer of the contract.
    from molecule_ai_contracts.workspace_comms_gen import (
        HeartbeatRequest,
        HeartbeatRuntimeMetadata,
    )

# Typed marker for activity-log rows that represent backpressure echoes rather
# than genuine peer results. The platform sets this on "queued: target busy"
# rows; the harvester also skips on the legacy substring as a fallback.
ACTIVITY_MESSAGE_TYPE_BACKPRESSURE = "backpressure"


def _runtime_state_payload() -> dict:
    """Build the {runtime_state, sample_error} portion of the heartbeat
    body when SOME adapter executor has marked itself wedged. Returns
    an empty dict when the runtime is healthy so the heartbeat payload
    doesn't grow fields the platform doesn't need.

    Source of truth is runtime_wedge (lives in molecule-runtime,
    independent of any specific adapter). Pre task #87 this imported
    from claude_sdk_executor — that worked because the executor was
    bundled into molecule-runtime, but blocked moving it to the
    claude-code template repo. The runtime_wedge module is now the
    cross-cutting wedge-state holder; adapters mark/clear via it,
    heartbeat reads it.

    Imported lazily so a workspace whose runtime image somehow ships
    without runtime_wedge (corrupt install, mid-rolling-deploy state)
    keeps heartbeating — a missing import means "no wedge info; assume
    healthy."
    """
    try:
        from molecule_runtime.runtime_wedge import is_wedged, wedge_reason
    except Exception:
        return {}
    if not is_wedged():
        return {}
    return {
        "runtime_state": "wedged",
        # sample_error doubles as the human-readable banner text on the
        # canvas's degraded card — keep it short and actionable.
        "sample_error": wedge_reason(),
    }


def _runtime_metadata_payload() -> dict:
    """Build the {runtime_metadata} portion of the heartbeat body —
    adapter-declared capabilities + per-capability override values
    (idle timeout, etc.). The platform reads this to route capabilities
    to the right owner: native (adapter) vs fallback (platform).

    Returns an empty dict if the adapter can't be loaded or introspected.
    Heartbeat must NEVER fail because of capability discovery — observability
    is more important than capability accuracy. The platform falls through
    to its own defaults when fields are missing.

    See project memory `project_runtime_native_pluggable.md` and
    molecule_runtime/adapter_base.py:RuntimeCapabilities.
    """
    try:
        from molecule_runtime.adapters import get_adapter
        # ADAPTER_MODULE wins over the runtime arg in get_adapter — pass
        # an empty string to force the env-var path.
        adapter_cls = get_adapter("")
        adapter = adapter_cls()
        caps = adapter.capabilities()
        caps_dict = caps.to_dict()
        # G2 (scheduler-as-trigger-plugin): a loaded trigger plugin makes native
        # scheduling a RUNTIME-level fact independent of the adapter — like
        # channel_dispatch. Advertise it so the platform scheduler defers
        # (NativeSchedulerCheck) and never double-fires. The boot seam sets the
        # flag from actual daemon discovery.
        import os as _os
        from molecule_runtime.plugin_daemons import NATIVE_SCHEDULER_ENV
        if _os.environ.get(NATIVE_SCHEDULER_ENV) == "1":
            caps_dict["scheduler"] = True
        # Typed against the SSOT contract sub-shape (molecule-contracts
        # HeartbeatRuntimeMetadata) for drift-prevention; same plain dict on the wire.
        meta: HeartbeatRuntimeMetadata = {"capabilities": caps_dict}
        idle = adapter.idle_timeout_override()
        # Only include the override when it's a positive integer. None /
        # zero / negative falls through to the platform's global default
        # (env A2A_IDLE_TIMEOUT_SECONDS, default 5min) — that "absent
        # field = use default" contract is what keeps the wire small.
        if isinstance(idle, int) and idle > 0:
            meta["idle_timeout_seconds"] = idle
        return {"runtime_metadata": meta}
    except Exception as e:
        # debug-level: missing ADAPTER_MODULE in dev / test envs is normal
        logger.debug("runtime_metadata: failed to read adapter caps: %s", e)
        return {}


logger = logging.getLogger(__name__)


def _persist_inbound_secret_from_heartbeat(resp) -> None:
    """Persist ``platform_inbound_secret`` from a heartbeat response, if any.

    The platform's heartbeat handler (workspace-server PR #2421) returns
    the secret on every beat — mirrors /registry/register so a workspace
    whose secret was lazy-healed on the platform side picks it up within
    one heartbeat tick instead of requiring a runtime restart.

    Without this delivery path the chat-upload code path's "secret was
    just minted, will pick up on next heartbeat" 503 message is a lie
    and the workspace stays 401-forever until the operator restarts the
    runtime. Caught 2026-04-30 on the hongmingwang tenant — the
    standalone wrapper (mcp_cli.py) got the same change in #2421 but
    the in-container heartbeat (this file) was missed in the first
    pass.

    Failure is non-fatal: if the body isn't JSON, doesn't carry the
    field, or the disk write fails, the next heartbeat retries. This
    matches the cold-start register flow in main.py:319-323.
    """
    try:
        body = resp.json()
    except Exception:
        return
    if not isinstance(body, dict):
        return
    secret = body.get("platform_inbound_secret")
    if not secret:
        return
    try:
        from molecule_runtime.platform_inbound_auth import save_inbound_secret

        save_inbound_secret(secret)
    except Exception as exc:
        logger.warning(
            "heartbeat: persist inbound secret failed: %s", exc
        )


# Versioned-heartbeat / generation contract (facts-up / desired-state-down),
# matched byte-for-byte against SDK #169 which adds the same three optional
# fields to the workspace-comms SSOT schema. HEARTBEAT_SCHEMA_VERSION is the
# heartbeat envelope version this runtime speaks; emitted on every beat as
# request.schema_version so core can reason about producer capability across a
# rolling upgrade. Bump when the envelope shape changes in a way core must
# branch on.
HEARTBEAT_SCHEMA_VERSION = "1"

HEARTBEAT_INTERVAL = 30  # seconds — fallback default when no per-instance value is passed
MAX_CONSECUTIVE_FAILURES = 10
# Cap (seconds) for the exponential backoff applied after a heartbeat failure.
# Prevents a persistently-unreachable platform from becoming a CPU busy-loop.
HEARTBEAT_BACKOFF_MAX_SECONDS = 300
MAX_SEEN_DELEGATION_IDS = 200
SELF_MESSAGE_COOLDOWN = 60  # seconds — minimum between self-messages to prevent loops
# Statuses that warrant a user-visible canvas chat push. The agent self-
# message path (above each /notify loop) always fires so the agent can
# synthesize its own NL summary via send_message_to_user. The /notify
# chat push exists only to surface failures the agent might otherwise
# swallow — completed delegations are noise on the canvas (chloe-dong
# task #384). Keep this list tiny + explicit so adding a new status
# requires deliberate review.
NOTIFY_STATUSES = frozenset({"failed", "error", "timeout", "cancelled"})
_DELEGATION_PREFIX = "Delegation completed:"
# Shared path — adapter executors (in their template repos) read this
# same file via executor_helpers.read_delegation_results so heartbeat-
# delivered async delegation results land in the next agent turn.
#
# RC #203 (durability): the harvested result is appended here BEFORE the durable
# harvest tombstone is committed (_commit_harvested). The queue must therefore be
# at least as durable as the tombstone — a /tmp (tmpfs) queue while the tombstone
# lives on the mailbox volume would let a restart lose the queued result but keep
# the tombstone, suppressing the result forever. So when the mailbox kernel is ON
# the queue lives on the durable mailbox volume (mailbox_dir.delegation_results_file);
# kernel OFF keeps the legacy /tmp default (byte-identical). An explicit
# DELEGATION_RESULTS_FILE env — captured at import here — or a test monkeypatch of
# this module attribute still wins verbatim; when left at the import-time default
# _delegation_results_file() routes it to the durable mailbox queue under the kernel.
_LEGACY_DELEGATION_RESULTS_FILE = os.environ.get(
    "DELEGATION_RESULTS_FILE", "/tmp/delegation_results.jsonl"
)
DELEGATION_RESULTS_FILE = _LEGACY_DELEGATION_RESULTS_FILE


def _delegation_results_file() -> str:
    """Live path to the delegation-results queue (RC #203 durable when kernel-ON).

    An explicit override — ``DELEGATION_RESULTS_FILE`` set in the environment at
    import (reflected in the module attribute) or a test monkeypatch of that
    attribute — wins verbatim. Otherwise defer to
    :func:`mailbox_dir.delegation_results_file`, which returns the DURABLE mailbox
    queue when the kernel is on and the legacy ``/tmp`` default when it is off
    (byte-identical). Resolving live keeps the writer, the executor reader
    (``executor_helpers.read_delegation_results``) and the idle-loop guard
    (``main._check_delegation_results_pending``) all pointed at the SAME queue.
    """
    if DELEGATION_RESULTS_FILE != _LEGACY_DELEGATION_RESULTS_FILE:
        return DELEGATION_RESULTS_FILE
    return mailbox_dir.delegation_results_file()
# Cursor file for tracking activity_log IDs processed from the a2a_receive path
# (delegations fired via tool_delegate_task → POST /workspaces/:id/a2a proxy, not
# POST /workspaces/:id/delegate). Persisted to disk so heartbeat restarts
# don't re-process the same rows.
_ACTIVITY_DELEGATION_CURSOR_FILE = os.environ.get(
    "DELEGATION_ACTIVITY_CURSOR_FILE",
    "/tmp/delegation_activity_cursor",
)


def _activity_delegation_cursor_file() -> str:
    """Resolve the a2a_receive delegation cursor path (MUST-FIX 5).

    Kernel ON: durable mailbox dir (``/workspace/.molecule``) so a restart
    resumes where it left off instead of re-scanning ``/tmp`` (wiped on a fresh
    container) — unless ``DELEGATION_ACTIVITY_CURSOR_FILE`` overrides it.
    Kernel OFF: the module-level ``_ACTIVITY_DELEGATION_CURSOR_FILE`` (env at
    import, monkeypatchable) — byte-identical to the pre-migration behavior.
    """
    if mailbox_dir.kernel_enabled():
        explicit = os.environ.get("DELEGATION_ACTIVITY_CURSOR_FILE", "").strip()
        if explicit:
            return explicit
        return str(mailbox_dir.resolve() / ".delegation_activity_cursor")
    return _ACTIVITY_DELEGATION_CURSOR_FILE


# Durable (kernel-on) harvest tombstone file: one "delegation_id<TAB>status"
# per line. See HeartbeatLoop._is_harvested / _commit_harvested for the rationale.
def _harvest_tombstone_file() -> str:
    if mailbox_dir.kernel_enabled():
        return str(mailbox_dir.resolve() / ".delegation_tombstones")
    return ""


# Cap the durable tombstone set so a long-lived workspace can't grow it without
# bound; oldest lines are dropped on overflow.
_MAX_HARVEST_TOMBSTONES = 4096

# ── Active-await scoping for the delegation-result harvester ─────────────────
# Root fix for the recurring delegation-result RE-NARRATION loop (supersedes
# the watermark band-aid). The harvester (_check_delegations) used to surface
# EVERY completed/failed delegation row where source_id==self, deduped only by
# the IN-MEMORY _seen_delegation_ids set. Because that set is process-scoped,
# every restart re-read the tenant's full backlog of historical completed
# PR-review delegation rows (178 on the agents-team concierge) and fired a
# self-message that woke the concierge to NARRATE the whole backlog again —
# an endless re-narration loop across restarts, and a path that also harvested
# a peer's autonomous/scheduled sweep results (codex-reviewer's
# org-pr-review-sweep) the concierge never asked to be woken for.
#
# The concierge must auto-harvest + self-wake-narrate a delegation result ONLY
# when it has an OPEN IN-FLIGHT AWAIT for that delegation — a delegation IT
# initiated this process and is still tracking (genuinely awaiting on behalf of
# a live task). The "open await set" is the UNION of:
#   (a) the in-container in-flight delegation registry the concierge maintains
#       (builtin_tools.delegation._delegations), populated by delegate_task_async
#       — read defensively, never mutated, so this scoping needs no change to
#       the delegation tool; and
#   (b) ids explicitly registered via register_awaited_delegation() — the public
#       seam other delegation paths (or tests) use to opt a delegation into
#       heartbeat auto-harvest.
#
# Both layers are PROCESS-SCOPED (empty on a fresh boot), which is exactly what
# kills the loop at the SOURCE: a (re)booted concierge has ZERO open awaits, so
# the historical backlog is never re-read/re-narrated, and a peer's autonomous
# sweep — which the concierge holds no await for — is never harvested. A
# genuinely user-awaited delegation the concierge initiated IS still delivered
# exactly once (it is in the open-await set, and _seen_delegation_ids dedupes
# the within-process re-poll). Mirrors the one-platform-agent-per-process
# posture of runtime_wedge / autonomous_loop_guard.
_REGISTERED_AWAIT_IDS: "dict[str, None]" = {}
_REGISTERED_AWAIT_LOCK = threading.Lock()
# Cap so a long-lived process can't grow the registry unbounded; evicts the
# oldest-inserted half on overflow (dict preserves insertion order).
_MAX_REGISTERED_AWAIT_IDS = 512


def register_awaited_delegation(delegation_id: str) -> None:
    """Record that the concierge is actively AWAITING this delegation's result,
    so the heartbeat harvester is allowed to surface it (once) when it
    completes. Idempotent; no-op on an empty id. Bounded.

    The in-container builtin delegate path is covered automatically (the
    harvester also reads builtin_tools.delegation._delegations), so this seam is
    for delegation paths that do NOT register there — e.g. the MCP/platform
    delegate path — plus tests."""
    if not delegation_id:
        return
    with _REGISTERED_AWAIT_LOCK:
        _REGISTERED_AWAIT_IDS[delegation_id] = None
        if len(_REGISTERED_AWAIT_IDS) > _MAX_REGISTERED_AWAIT_IDS:
            drop = len(_REGISTERED_AWAIT_IDS) - _MAX_REGISTERED_AWAIT_IDS // 2
            for k in list(_REGISTERED_AWAIT_IDS)[:drop]:
                _REGISTERED_AWAIT_IDS.pop(k, None)


def discard_awaited_delegation(delegation_id: str) -> None:
    """Drop an await id once its result has been delivered (await satisfied)."""
    if not delegation_id:
        return
    with _REGISTERED_AWAIT_LOCK:
        _REGISTERED_AWAIT_IDS.pop(delegation_id, None)


def reset_awaited_delegations_for_test() -> None:
    """Test-only: clear the registered-await set between cases."""
    with _REGISTERED_AWAIT_LOCK:
        _REGISTERED_AWAIT_IDS.clear()


def _in_flight_delegation_ids() -> set[str]:
    """Delegation ids the concierge initiated this process and is tracking,
    read READ-ONLY from the in-container in-flight delegation registry
    (builtin_tools.delegation._delegations, populated by delegate_task_async).

    Defensive: returns an empty set if that module isn't importable (partial
    install / mid-deploy) — i.e. a missing registry fails CLOSED (no harvest),
    the safe direction for the loop fix (better to miss a rare async result the
    agent can still poll for than to re-enter the re-narration loop)."""
    try:
        from molecule_runtime.builtin_tools.delegation import _delegations

        return {tid for tid in _delegations if tid}
    except Exception:
        return set()


def _active_await_delegation_ids() -> set[str]:
    """The concierge's set of OPEN in-flight delegation awaits — the union of
    explicitly-registered awaits and the in-container in-flight registry. The
    harvester surfaces a delegation result ONLY when its delegation_id is in
    this set."""
    with _REGISTERED_AWAIT_LOCK:
        registered = set(_REGISTERED_AWAIT_IDS)
    return registered | _in_flight_delegation_ids()


class HeartbeatLoop:
    def __init__(
        self,
        platform_url: str,
        workspace_id: str,
        interval_seconds: int = HEARTBEAT_INTERVAL,
        agent_card: dict | None = None,
    ):
        self.platform_url = platform_url
        self.workspace_id = workspace_id
        self.agent_card = agent_card
        # Per-instance interval — main.py threads ObservabilityConfig.
        # heartbeat_interval_seconds (clamped to [5, 300] at parse time)
        # in here so operators can tune cadence per-workspace via the
        # `observability:` block in config.yaml. Defaults to the
        # legacy module constant so callers that haven't been updated
        # yet (and tests that construct HeartbeatLoop directly with the
        # 2-arg signature) keep their existing 30s behavior.
        self._interval_seconds = interval_seconds
        self.start_time = time.time()
        self.error_count = 0
        self.request_count = 0
        self.active_tasks = 0
        self.current_task = ""
        self.sample_error = ""
        self._task = None
        self._consecutive_failures = 0
        # #2723 (300s tool-chain-lost): the alive-signal heartbeat POST runs
        # on a DEDICATED OS THREAD, not the agent's shared asyncio event
        # loop. A long synchronous/CPU-bound tool step (bulk file
        # download/upload via a sync client, big inline subprocess, image
        # processing) blocks the event loop and starves an asyncio-task
        # heartbeat → no /heartbeat for minutes → the platform's canvas
        # idle watchdog (workspace-server applyIdleTimeout) sees broadcaster
        # silence and kills the turn. A real OS thread keeps POSTing
        # regardless of what the event loop is doing.
        #
        # Only the alive signal moves to the thread. Delegation/activity
        # polling (which itself fires async A2A self-message POSTs and disk
        # writes) stays on the async _loop — it is not what the idle
        # watchdog keys on, and keeping it async avoids cross-thread sharing
        # of the httpx.AsyncClient / asyncio primitives those POSTs use.
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()
        # core#3082 / runtime#181: a dedicated daemon thread that actively probes
        # the management MCP (side-effect-free tools/list) and publishes
        # loaded_mcp_tools, so the readiness signal the CP online/degraded gate
        # reads is reliable and turn-INDEPENDENT — it no longer depends on the
        # per-turn init capture ever firing. Created in start(), stopped in
        # stop(). None until started; self-gates to a no-op (no subprocess) on a
        # runtime that doesn't declare the management MCP.
        self._mcp_prober = None
        self._seen_delegation_ids: set[str] = set()
        self._last_self_message_time = 0.0
        self._parent_name: str | None = None  # Cached after first lookup
        # Seen activity IDs for a2a_receive polling (delegations via POST /a2a proxy path).
        # Loaded lazily from cursor file on first poll to avoid blocking startup.
        self._seen_activity_ids: set[str] = set()
        self._activity_cursor_loaded = False
        # MUST-FIX 4: durable (kernel-on) harvest tombstones keyed by
        # (delegation_id, status). Loaded lazily; only materialized when the
        # mailbox kernel is on (else _is_harvested/_commit_harvested are no-ops).
        self._harvest_tombstones: set[tuple[str, str]] = set()
        self._harvest_tombstones_loaded = False
        # Versioned-heartbeat / generation contract (consumer half). The last
        # desired-state generation core published in a heartbeat RESPONSE
        # (`response.generation`), captured in-memory. Echoed back as
        # `request.observed_generation` on the next beat so core can see which
        # desired-state the runtime has caught up to (k8s observedGeneration).
        # CAPTURE-ONLY for now: default 0 = "no generation seen yet"; wiring it
        # into actual reconcile behavior is a later step.
        self._last_seen_generation = 0

    @property
    def error_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.error_count / self.request_count

    def record_error(self, error: str) -> None:
        self.error_count += 1
        self.request_count += 1
        self.sample_error = error

    def record_success(self) -> None:
        self.request_count += 1

    def start(self) -> None:
        # #2723: alive-signal heartbeat on a dedicated daemon OS thread so it
        # survives a blocked asyncio event loop. The async _loop continues to
        # own delegation/activity polling.
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_thread_loop,
            name="heartbeat-alive",
            daemon=True,
        )
        self._hb_thread.start()
        # core#3082 / runtime#181: start the management-MCP readiness prober on
        # its OWN daemon thread (never the heartbeat POST thread — a cold MCP
        # spawn must not delay the alive signal). Defensive: a prober failure can
        # never break heartbeat startup.
        try:
            from molecule_runtime.mcp_readiness_probe import MCPReadinessProber

            self._mcp_prober = MCPReadinessProber()
            self._mcp_prober.start()
        except Exception:  # noqa: BLE001 — readiness probing is best-effort
            logger.exception("heartbeat: failed to start MCP readiness prober")
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception():
            logger.error("Heartbeat loop died: %s — restarting", task.exception())
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._on_done)

    def _is_busy(self) -> bool:
        """Live busy signal for the heartbeat body (RFC molecule-core#4402 B2).

        Sourced from the A2A inbox's ``turn_in_flight`` state via the single
        ``runtime_inbox.any_turn_in_flight()`` accessor — no duplicated
        turn-tracking. True while a turn (incl. its tool calls) is executing,
        False when idle. Imported lazily + defensively so a heartbeat can never
        be lost to an inbox import/lookup hiccup (best-effort telemetry): on any
        failure we report not-busy, and core's COALESCE(is_busy, active_tasks>0)
        honor-branch still has active_tasks as the fallback.
        """
        try:
            from molecule_runtime.runtime_inbox import any_turn_in_flight

            return any_turn_in_flight()
        except Exception:  # noqa: BLE001 — busy telemetry is best-effort
            logger.debug("heartbeat: is_busy probe failed; reporting not-busy")
            return False

    def _capture_generation_from_heartbeat(self, resp) -> None:
        """Capture the desired-state ``generation`` from a heartbeat response.

        Consumer half of the versioned-heartbeat / generation contract
        (desired-state-down). Core's heartbeat ack MAY carry ``generation`` =
        its CURRENT desired-state generation (k8s-style). We persist it
        IN-MEMORY as the last-seen desired-state generation so the NEXT beat
        echoes it back as ``request.observed_generation`` and core can tell
        which desired-state the runtime has caught up to.

        CAPTURE-ONLY for now: wiring the generation into actual reconcile
        behavior is a later step. Best-effort — a non-JSON body, a missing /
        non-int ``generation`` field, or any error leaves the last-seen value
        unchanged (the echo simply stays put). ``bool`` is explicitly rejected
        (it is an ``int`` subclass in Python, and a JSON ``true`` is never a
        generation).
        """
        try:
            body = resp.json()
        except Exception:
            return
        if not isinstance(body, dict):
            return
        gen = body.get("generation")
        if isinstance(gen, bool) or not isinstance(gen, int):
            return
        self._last_seen_generation = gen

    def _send_heartbeat(self, client: httpx.Client) -> None:
        """Send one alive-signal heartbeat POST (sync). Runs on the dedicated
        OS thread. Mirrors the original async POST: same /registry/heartbeat
        URL, same payload, same 401-refresh-and-retry-once, same
        platform_inbound_secret persistence. Raises on transport/HTTP error
        so the caller can track consecutive failures."""
        # Typed against the SSOT contract (molecule-contracts HeartbeatRequest)
        # for drift-prevention; the wire payload is unchanged. This is the
        # reference HeartbeatPayload: identity_gate_payload() layers in the
        # contract's mcp_server_present + loaded_mcp_tools tri-states, and the
        # _runtime_state_payload / _runtime_metadata_payload merges below add
        # runtime_state + runtime_metadata. (platform_mcp_diag is a runtime-only
        # diagnostic not yet promoted into the schema — see the contracts repo's
        # heartbeat divergence notes; a type checker surfacing it is correct drift
        # signal, not a regression.)
        body: HeartbeatRequest = {
            "workspace_id": self.workspace_id,
            "error_rate": self.error_rate,
            "sample_error": self.sample_error,
            "active_tasks": self.active_tasks,
            # RFC molecule-core#4402 B2: dual-write is_busy alongside active_tasks
            # (active_tasks kept for the migration window per §5). Sourced from the
            # live turn_in_flight state; activates core's COALESCE honor-branch.
            "is_busy": self._is_busy(),
            "current_task": self.current_task,
            "uptime_seconds": int(time.time() - self.start_time),
            # Versioned-heartbeat / generation contract (facts-up), matched to
            # SDK #169. schema_version = the envelope version this runtime
            # speaks; observed_generation = the last desired-state generation
            # captured from a heartbeat response (0 until core publishes one).
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "observed_generation": self._last_seen_generation,
            **identity_gate_payload(),
        }
        # #2421: backfill agent_card when the initial register failed. Only
        # sent when we have a card — the platform writes it only if the DB
        # row's agent_card is NULL.
        if self.agent_card is not None:
            body["agent_card"] = self.agent_card
        # Layer the runtime-wedge + metadata fields on top (status→degraded,
        # capability routing). Identical to the original async path.
        body.update(_runtime_state_payload())
        body.update(_runtime_metadata_payload())
        # Role-identity diagnostic. Emits `role_identity_diag` ONLY when this
        # workspace had to fall back to the branded platform default because its
        # persona file never arrived — absent field = healthy, so the wire shape
        # is unchanged for every normal workspace. Same runtime-only-diagnostic
        # posture as `platform_mcp_diag` above. Without this the fact exists only
        # in container stdout, and the CP cannot tell "FAILED to get its identity"
        # from "never needed a role prompt file".
        try:
            from molecule_runtime import identity_health

            body.update(identity_health.heartbeat_payload())
        except Exception as _ident_exc:  # noqa: BLE001 — never break a heartbeat
            logger.debug("role_identity_diag: skipped (%s)", _ident_exc)
        try:
            resp = client.post(
                f"{self.platform_url}/registry/heartbeat",
                json=body,
                headers=auth_headers(),
            )
            self.error_count = 0
            self.request_count = 0
            self._consecutive_failures = 0
            _persist_inbound_secret_from_heartbeat(resp)
            # Consumer half of the generation contract: capture core's
            # desired-state generation so the next beat echoes it back.
            self._capture_generation_from_heartbeat(resp)
            return
        except Exception as e:
            # Issue #1877: on 401, re-read the token from disk and retry once.
            is_401 = (
                isinstance(e, httpx.HTTPStatusError)
                and e.response.status_code == 401
            )
            if not is_401:
                raise
            logger.warning(
                "Heartbeat 401 for %s — refreshing token cache and retrying once",
                self.workspace_id,
            )
            refresh_cache()
            retry_body: dict = {
                "workspace_id": self.workspace_id,
                "error_rate": self.error_rate,
                "sample_error": self.sample_error,
                "active_tasks": self.active_tasks,
                # RFC molecule-core#4402 B2: dual-write is_busy (see primary body).
                "is_busy": self._is_busy(),
                "current_task": self.current_task,
                "uptime_seconds": int(time.time() - self.start_time),
                # Versioned-heartbeat / generation contract — kept in sync with
                # the primary body above (facts-up).
                "schema_version": HEARTBEAT_SCHEMA_VERSION,
                "observed_generation": self._last_seen_generation,
                **identity_gate_payload(),
            }
            if self.agent_card is not None:
                retry_body["agent_card"] = self.agent_card
            retry_body.update(_runtime_state_payload())
            retry_resp = client.post(
                f"{self.platform_url}/registry/heartbeat",
                json=retry_body,
                headers=auth_headers(),
            )
            self._consecutive_failures = 0
            self.request_count += 1
            _persist_inbound_secret_from_heartbeat(retry_resp)
            # Consumer half of the generation contract (retry path — kept in
            # sync with the primary path above).
            self._capture_generation_from_heartbeat(retry_resp)

    def _heartbeat_thread_loop(self) -> None:
        """Dedicated-OS-thread alive-signal loop (#2723). Keeps POSTing
        /registry/heartbeat on a plain time.sleep cadence using a SYNC
        httpx.Client, fully independent of the agent's asyncio event loop —
        so it does not stop when a long synchronous tool step blocks that
        loop. Recreates the client after a run of consecutive failures,
        mirroring the async loop's resilience."""
        while not self._hb_stop.is_set():
            client = None
            try:
                client = httpx.Client(timeout=10.0)
                while not self._hb_stop.is_set():
                    try:
                        self._send_heartbeat(client)
                    except Exception as e:
                        self._consecutive_failures += 1
                        if (
                            self._consecutive_failures <= 3
                            or self._consecutive_failures % MAX_CONSECUTIVE_FAILURES == 0
                        ):
                            logger.warning(
                                "Heartbeat failed (%d consecutive): %s",
                                self._consecutive_failures,
                                e,
                            )
                        # Exponential backoff (capped + jittered) after a failure.
                        # BUGFIX: the recreate `break` below used to skip the
                        # bottom-of-loop sleep, so once consecutive>=MAX every
                        # iteration recreated the client and re-attempted with NO
                        # delay (~100/sec, ~90% CPU/container when the platform is
                        # unreachable). Always back off here — including the
                        # recreate path — so a down platform can never busy-loop.
                        backoff = min(
                            self._interval_seconds
                            * (2 ** min(self._consecutive_failures - 1, 6)),
                            HEARTBEAT_BACKOFF_MAX_SECONDS,
                        )
                        backoff += random.uniform(0.0, min(backoff * 0.25, 5.0))
                        recreate = self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        if recreate:
                            logger.info(
                                "Heartbeat: recreating HTTP client after %d failures",
                                self._consecutive_failures,
                            )
                        # Interruptible sleep: wakes immediately on stop() so the
                        # thread joins promptly during shutdown.
                        self._hb_stop.wait(backoff)
                        if recreate:
                            break  # drop to outer loop → recreate client
                        continue
                    # Success — normal cadence.
                    self._hb_stop.wait(self._interval_seconds)
            except Exception as e:
                logger.error(
                    "Heartbeat thread error: %s — retrying in %ds",
                    e,
                    self._interval_seconds,
                )
                self._hb_stop.wait(self._interval_seconds)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    async def stop(self):
        # Stop the dedicated heartbeat thread first (signal + join), then
        # cancel the async delegation loop.
        self._hb_stop.set()
        # Stop the MCP readiness prober off the event loop (it may be mid-probe
        # in a subprocess wait). Best-effort — never let teardown raise.
        if self._mcp_prober is not None:
            try:
                await asyncio.to_thread(self._mcp_prober.stop)
            except Exception:  # noqa: BLE001
                logger.debug("heartbeat: MCP readiness prober stop raised", exc_info=True)
            self._mcp_prober = None
        if self._hb_thread is not None:
            # Join off the event loop so a blocked/slow thread shutdown does
            # not stall the async caller. Interval is small (<=300s) and the
            # stop Event interrupts the sleep, so this returns quickly.
            await asyncio.to_thread(self._hb_thread.join, 10.0)
            self._hb_thread = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # #2723: the alive-signal /registry/heartbeat POST moved to the
        # dedicated OS thread (_heartbeat_thread_loop) so it survives a
        # blocked event loop. This async loop now owns ONLY delegation /
        # activity polling — work that itself fires async A2A self-message
        # POSTs and is, by nature, event-loop-bound.
        while True:
            client = None
            try:
                client = httpx.AsyncClient(timeout=10.0)
                while True:
                    # 1. Check delegation status
                    try:
                        await self._check_delegations(client)
                    except Exception as e:
                        logger.debug("Delegation check failed: %s", e)

                    # 2. Check activity_logs for delegation results that arrived via
                    # the POST /a2a proxy path (tool_delegate_task → send_a2a_message).
                    # These are NOT written to the delegations table, so
                    # _check_delegations misses them. See issue #354.
                    try:
                        await self._check_activity_delegations(client)
                    except Exception as e:
                        logger.debug("Activity delegation check failed: %s", e)

                    await asyncio.sleep(self._interval_seconds)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "Heartbeat loop error: %s — retrying in %ds", e, self._interval_seconds
                )
                await asyncio.sleep(self._interval_seconds)
            finally:
                if client:
                    try:
                        await client.aclose()
                    except Exception:
                        pass

    def _load_harvest_tombstones(self) -> None:
        """Lazily load durable (delegation_id, status) harvest tombstones."""
        if self._harvest_tombstones_loaded:
            return
        self._harvest_tombstones_loaded = True
        path = _harvest_tombstone_file()
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    did, _, status = line.strip().partition("\t")
                    if did:
                        self._harvest_tombstones.add((did, status))
        except OSError:
            pass  # Absent / corrupt — start fresh (dedup degrades, never crashes)

    def _save_harvest_tombstones(self) -> None:
        """Persist harvest tombstones, evicting oldest half on overflow."""
        path = _harvest_tombstone_file()
        if not path:
            return
        if len(self._harvest_tombstones) > _MAX_HARVEST_TOMBSTONES:
            keep = list(self._harvest_tombstones)[_MAX_HARVEST_TOMBSTONES // 2:]
            self._harvest_tombstones = set(keep)
        try:
            with open(path, "w", encoding="utf-8") as f:
                for did, status in self._harvest_tombstones:
                    f.write(f"{did}\t{status}\n")
        except OSError:
            pass  # Best-effort; a lost tombstone only risks one extra harvest

    def _is_harvested(self, delegation_id: str, status: str) -> bool:
        """Kernel-gated DURABLE dedup CHECK keyed by (delegation_id, status).

        MUST-FIX 4: the completed/failed delegation-result tombstones go DURABLE
        (mailbox volume) so a restart never re-harvests a delegation the
        concierge already surfaced — belt-and-braces on top of the
        process-scoped active-await scoping. The ``(id, status)`` key makes a
        status-flip (completed -> failed) a DISTINCT tombstone, so the FAILED
        branch is harvested exactly once even after a COMPLETED harvest.

        RC #203 (correctness): this is CHECK-ONLY — it never records. The
        tombstone is committed by :meth:`_commit_harvested` ONLY after the
        result is durably queued AND the self-wake is sent/scheduled, so a
        transient delivery failure (result-file write or wake POST) leaves the
        tombstone UNcommitted and the result is re-harvested on the next pass /
        after restart instead of being permanently suppressed. No-op (returns
        False) when the mailbox kernel is off, so the default flow is
        byte-identical.
        """
        if not mailbox_dir.kernel_enabled():
            return False
        self._load_harvest_tombstones()
        return (delegation_id, status) in self._harvest_tombstones

    def _commit_harvested(self, keys: "list[tuple[str, str]]") -> None:
        """Durably record harvest tombstones AFTER delivery is confirmed.

        RC #203: called only once the harvested results were durably written to
        ``DELEGATION_RESULTS_FILE`` AND the self-wake was sent (or deliberately
        deferred by cooldown/breaker — both leave the result durably queued for
        the next turn's context injection). A result whose delivery RAISED is
        never passed here, so its tombstone stays uncommitted and it re-harvests
        on the next pass / restart — never silently dropped. No-op when the
        mailbox kernel is off (byte-identical).
        """
        if not mailbox_dir.kernel_enabled() or not keys:
            return
        self._load_harvest_tombstones()
        changed = False
        for key in keys:
            if key not in self._harvest_tombstones:
                self._harvest_tombstones.add(key)
                changed = True
        if changed:
            self._save_harvest_tombstones()

    async def _check_delegations(self, client: httpx.AsyncClient):
        """Check for completed delegations and store results for the agent."""
        try:
            resp = await client.get(
                f"{self.platform_url}/workspaces/{self.workspace_id}/delegations",
                headers=auth_headers(),
            )
            if resp.status_code != 200:
                return

            delegations = resp.json()
            if not isinstance(delegations, list):
                return

            # Active-await scoping (root loop fix): only surface results the
            # concierge has an OPEN in-flight await for. Computed once per poll.
            # On a fresh boot this is EMPTY, so a backlog of historical
            # completed rows is never re-read/re-narrated, and a peer's
            # autonomous/scheduled sweep (which the concierge holds no await
            # for) is never harvested. See the module-level rationale above
            # register_awaited_delegation().
            active_awaits = _active_await_delegation_ids()

            new_results = []
            # RC #203: (id, status) keys whose durable tombstone is committed
            # ONLY after delivery is confirmed (see _commit_harvested below), so
            # a failed delivery re-harvests instead of being suppressed.
            pending_tombstones: list[tuple[str, str]] = []
            for d in delegations:
                did = d.get("delegation_id", "")
                status = d.get("status", "")

                if not did or did in self._seen_delegation_ids:
                    continue

                # Skip any row the concierge is NOT actively awaiting: no
                # self-wake, no narration. Deliberately NOT marked seen, so a
                # result that lands just before its await is registered can
                # still be delivered on a later poll once the await exists.
                if did not in active_awaits:
                    continue

                if status in ("completed", "failed"):
                    # Fix B (Cycle 5): validate source_id before accepting delegation
                    # results. Only process delegations that THIS workspace created
                    # (source_id == self.workspace_id). Attacker-crafted delegation
                    # records with a foreign source_id cannot inject instructions.
                    source_id = d.get("source_id", "")
                    if source_id != self.workspace_id:
                        logger.warning(
                            "Heartbeat: skipping delegation %s — source_id %r does not "
                            "match this workspace %r; possible injection attempt",
                            did, source_id, self.workspace_id,
                        )
                        self._seen_delegation_ids.add(did)  # mark seen so we don't warn again
                        continue

                    self._seen_delegation_ids.add(did)
                    # Await satisfied — drop it from the registered-await set so
                    # the registry stays bounded (within-process re-poll is
                    # already deduped by _seen_delegation_ids).
                    discard_awaited_delegation(did)
                    # MUST-FIX 4: durable (delegation_id, status) dedup. No-op
                    # when the mailbox kernel is off (byte-identical); when on it
                    # prevents a restart or a duplicate delegate_result row from
                    # re-harvesting a result already surfaced for this status.
                    # RC #203: CHECK-ONLY here — the tombstone is committed by
                    # _commit_harvested AFTER delivery is confirmed, so a failed
                    # result-file write or self-wake POST re-harvests instead of
                    # being permanently suppressed.
                    if self._is_harvested(did, status):
                        continue
                    new_results.append({
                        "delegation_id": did,
                        "target_id": d.get("target_id", ""),
                        "source_id": source_id,
                        "status": status,
                        "summary": d.get("summary", ""),
                        "response_preview": d.get("response_preview", ""),
                        "error": d.get("error", ""),
                        "timestamp": time.time(),
                    })
                    pending_tombstones.append((did, status))

            # Evict old seen IDs if over limit
            if len(self._seen_delegation_ids) > MAX_SEEN_DELEGATION_IDS:
                # Keep most recent half
                self._seen_delegation_ids = set(list(self._seen_delegation_ids)[MAX_SEEN_DELEGATION_IDS // 2:])

            if new_results:
                # Append to results file for context injection on next message.
                # RC #203: if this durable write raises, control jumps to the
                # outer except and the pending tombstones are NEVER committed, so
                # the results are re-harvested on the next pass / after restart.
                with open(_delegation_results_file(), "a") as f:
                    for r in new_results:
                        f.write(json.dumps(r) + "\n")
                logger.info("Heartbeat: %d new delegation results — triggering self-message", len(new_results))
                # Result is now durably queued. Assume the wake path is
                # confirmed/scheduled unless the self-wake POST below raises.
                delivery_confirmed = True

                # Build a summary message for the agent.
                # Fix B (Cycle 5): do NOT embed raw response_preview text in
                # user-role A2A messages — that is the prompt-injection vector.
                # Instead reference only the delegation ID and status; the agent
                # reads full content from DELEGATION_RESULTS_FILE which was
                # written above from trusted platform data.
                summary_lines = []
                for r in new_results:
                    line = f"- [{r['status']}] Delegation {r['delegation_id'][:8]}: {r['summary'][:80]}"
                    if r.get("error"):
                        line += f"\n  Error: {r['error'][:100]}"
                    summary_lines.append(line)

                # Look up parent workspace (cached after first call)
                if self._parent_name is None:
                    try:
                        parent_resp = await client.get(
                            f"{self.platform_url}/workspaces/{self.workspace_id}",
                            headers=auth_headers(),
                        )
                        if parent_resp.status_code == 200:
                            parent_id = parent_resp.json().get("parent_id", "")
                            if parent_id:
                                parent_info = await client.get(
                                    f"{self.platform_url}/workspaces/{parent_id}",
                                    headers=auth_headers(),
                                )
                                if parent_info.status_code == 200:
                                    self._parent_name = parent_info.json().get("name", "")
                        if self._parent_name is None:
                            self._parent_name = ""  # No parent — cache empty
                    except Exception:
                        pass  # Will retry next cycle
                parent_name = self._parent_name or ""

                report_instruction = ""
                if parent_name:
                    report_instruction = (
                        f"\n\nIMPORTANT: Report these results back to your parent '{parent_name}' "
                        f"by delegating a summary to them. Use delegate_task or delegate_task_async "
                        f"with a concise status report. Also use send_message_to_user to notify the user."
                    )
                else:
                    report_instruction = (
                        "\n\nReport results using send_message_to_user to notify the user."
                    )

                trigger_msg = (
                    "Delegation results are ready. Review them and take appropriate action:\n"
                    + "\n".join(summary_lines)
                    + report_instruction
                )

                # Send A2A self-message to wake the agent.
                # Minimum 60s between self-messages to avoid spam, but always send
                # when there are genuinely NEW results to process.
                now = time.time()
                if now - self._last_self_message_time < SELF_MESSAGE_COOLDOWN:
                    logger.debug("Heartbeat: self-message cooldown (60s), will retry next cycle")
                elif not _kernel_allows_autonomous_injection(kernel.KIND_DELEGATION_RESULT):
                    # MUST-FIX 2 (kernel-ON): route this delegation-result
                    # self-wake through the kernel's pre-injection circuit-breaker
                    # check. When the autonomous-loop breaker is OPEN we DROP the
                    # self-wake instead of injecting another replay (the runaway
                    # incident driver). Kernel OFF -> the guard short-circuits to
                    # allow -> byte-identical (this branch is never taken).
                    logger.info(
                        "Heartbeat: autonomous-loop breaker OPEN — dropping "
                        "delegation-result self-wake (kernel)"
                    )
                else:
                    self._last_self_message_time = now
                    try:
                        # self_source_headers() adds X-Workspace-ID so the
                        # platform tags this row source=agent, not canvas
                        # — see platform_auth.py for the full rationale.
                        wake_resp = await client.post(
                            f"{self.platform_url}/workspaces/{self.workspace_id}/a2a",
                            json={
                                "method": "message/send",
                                # #2251: single model-based builder — params
                                # generated FROM the receiver's a2a-sdk v0.3
                                # SendMessageRequest schema.
                                "params": build_message_send_params(
                                    trigger_msg,
                                    metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_HARVESTER},
                                ),
                            },
                            headers=self_source_headers(self.workspace_id),
                            timeout=120.0,
                        )
                        # RC #203 (Finding 2): httpx does NOT raise on a 4xx/5xx —
                        # only on transport errors. A non-2xx self-wake means the
                        # agent was NOT woken, so it MUST be treated as a failed
                        # delivery (same family as the tombstone bugs already fixed
                        # on this branch): leave the tombstone UNcommitted so the
                        # result re-harvests next pass / after restart instead of
                        # being silently suppressed forever. Confirm on 2xx ONLY.
                        if 200 <= wake_resp.status_code < 300:
                            logger.info("Heartbeat: self-message sent to process delegation results")
                        else:
                            logger.warning(
                                "Heartbeat: self-wake POST returned HTTP %s — leaving "
                                "delegation tombstone uncommitted for re-harvest",
                                wake_resp.status_code,
                            )
                            delivery_confirmed = False
                    except Exception as e:
                        logger.warning("Heartbeat: failed to send self-message: %s", e)
                        # RC #203: the wake POST failed — do NOT commit the
                        # tombstones, so the result is re-harvested next pass /
                        # after restart rather than silently suppressed.
                        delivery_confirmed = False

                # RC #203: commit the durable harvest tombstones ONLY now — after
                # the result was durably written AND the self-wake was sent or
                # deliberately deferred (cooldown/breaker still leave the result
                # queued for the next turn's injection). A failed result-file
                # write (outer except, never reaches here) or a failed wake POST
                # leaves the tombstone uncommitted so the result is re-harvested,
                # never silently dropped. No-op when the kernel is off.
                if delivery_confirmed:
                    self._commit_harvested(pending_tombstones)

                # Also push notification to user via canvas, but ONLY for
                # failure-class statuses. Success rows are agent-only — the
                # self-message above wakes the agent who synthesizes its
                # own NL via send_message_to_user. See task #384 / CTO
                # decision 2026-05-20 (canvas pollution from raw envelopes).
                for r in new_results:
                    if r.get("status") not in NOTIFY_STATUSES:
                        continue
                    try:
                        reason = r.get("error") or r.get("summary", "")
                        msg = f"Delegation {r['status']}: {reason[:200]}"
                        await client.post(
                            f"{self.platform_url}/workspaces/{self.workspace_id}/notify",
                            json={"message": msg, "type": "delegation_result"},
                            headers=auth_headers(),
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.debug("Delegation check error: %s", e)

    async def _check_activity_delegations(self, client: httpx.AsyncClient):
        """Poll activity_logs for delegation results that arrived via the POST /a2a proxy path.

        tool_delegate_task → send_a2a_message → POST /workspaces/:id/a2a (proxy)
        logs to activity_logs but NOT the delegations table. _check_delegations
        only checks the delegations table, so these results are invisible to the
        heartbeat — the agent never wakes up to consume them (issue #354).

        This method closes that gap: polls GET /workspaces/:id/activity?type=a2a_receive,
        filters for rows from peer workspaces (source_id != "" and != self.workspace_id),
        tracks seen IDs with a cursor file, and sends a self-message to wake the agent.
        """
        try:
            # Load cursor lazily on first call so startup is not blocked by disk I/O.
            if not self._activity_cursor_loaded:
                self._activity_cursor_loaded = True
                try:
                    _cursor_file = _activity_delegation_cursor_file()
                    if os.path.exists(_cursor_file):
                        cursor = open(_cursor_file).read().strip()
                        if cursor:
                            self._seen_activity_ids = set(cursor.split(","))
                except Exception:
                    pass  # Corrupt cursor — start fresh

            params: dict[str, str] = {"type": "a2a_receive"}
            resp = await client.get(
                f"{self.platform_url}/workspaces/{self.workspace_id}/activity",
                params=params,
                headers=auth_headers(),
            )
            if resp.status_code != 200:
                return

            rows = resp.json()
            if not isinstance(rows, list):
                return

            # Activity API returns newest-first; process in reverse order so
            # we advance the cursor monotonically (oldest → newest).
            rows = list(reversed(rows))

            new_results: list[dict] = []
            last_id: str | None = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                activity_id = str(row.get("id", ""))
                if not activity_id:
                    continue
                last_id = activity_id

                if activity_id in self._seen_activity_ids:
                    continue

                # Filter: must have a non-empty source_id that is NOT this workspace
                # (peer agent messages only; skip canvas-user messages and self-notify).
                source_id = row.get("source_id") or ""
                if not source_id or source_id == self.workspace_id:
                    continue

                # Skip non-result rows. A peer send that merely got QUEUED
                # because we were busy ("queued: target busy"), or a receive
                # that FAILED (status present and != "ok" — e.g. the 300s
                # "timeout awaiting response headers"), is not a peer response
                # with content. Harvesting it as a "completed" result wakes
                # the agent to "process" transient backpressure, generating
                # more sends and more backpressure: a self-amplifying replay
                # storm (observed: one notification wrapping 12x identical
                # "queued: target busy" echoes). Mark seen so the cursor still
                # advances; never surface it as a result.
                row_status = row.get("status") or ""
                row_message_type = row.get("message_type") or ""
                is_backpressure = (
                    row_message_type == ACTIVITY_MESSAGE_TYPE_BACKPRESSURE
                    or "queued: target busy" in (row.get("summary") or "")
                )
                if (row_status and row_status != "ok") or is_backpressure:
                    self._seen_activity_ids.add(activity_id)
                    continue

                self._seen_activity_ids.add(activity_id)
                summary = row.get("summary") or ""
                # Extract response text from request_body if available.
                # Shape mirrors inbox._extract_text: walk parts for "text" field.
                response_text = summary
                request_body = row.get("request_body")
                if isinstance(request_body, dict):
                    params_obj = request_body.get("params")
                    if isinstance(params_obj, dict):
                        msg = params_obj.get("message")
                        if isinstance(msg, dict):
                            parts = msg.get("parts") or []
                            texts = []
                            for p in (parts if isinstance(parts, list) else []):
                                if isinstance(p, dict) and p.get("kind") == "text" or p.get("type") == "text":
                                    t = p.get("text", "")
                                    if t:
                                        texts.append(t)
                            if texts:
                                response_text = " ".join(texts)

                new_results.append({
                    "delegation_id": activity_id,  # Use activity ID as pseudo-delegation ID
                    "target_id": source_id,
                    "source_id": self.workspace_id,
                    "status": "completed",
                    "summary": summary,
                    "response_preview": response_text[:4096],
                    "error": "",
                    "timestamp": time.time(),
                })

            if not new_results:
                return

            # Persist cursor so restarts don't re-process these rows.
            if last_id:
                try:
                    with open(_activity_delegation_cursor_file(), "w") as f:
                        # Keep cursor as comma-joined IDs; truncate if over 100KB.
                        cursor_str = ",".join(sorted(self._seen_activity_ids))
                        if len(cursor_str) > 102_400:
                            # Evict oldest half when cursor file grows too large.
                            sorted_ids = sorted(self._seen_activity_ids)
                            self._seen_activity_ids = set(sorted_ids[len(sorted_ids) // 2:])
                            cursor_str = ",".join(sorted(self._seen_activity_ids))
                        f.write(cursor_str)
                except Exception:
                    pass  # Non-fatal; next cycle will retry

            # Append to results file and trigger self-message (mirrors _check_delegations).
            # RC #203: durable-queue resolution (kernel-ON) via _delegation_results_file().
            with open(_delegation_results_file(), "a") as f:
                for r in new_results:
                    f.write(json.dumps(r) + "\n")
            logger.info(
                "Heartbeat: %d new a2a_receive delegation results from activity_logs — "
                "triggering self-message",
                len(new_results),
            )

            # Build and send self-message to wake the agent.
            summary_lines = []
            for r in new_results:
                line = f"- [completed] Peer response from {r['target_id'][:8]}: {r['summary'][:80] or '(no summary)'}"
                if r.get("error"):
                    line += f"\n  Error: {r['error'][:100]}"
                summary_lines.append(line)

            # Look up parent name (reuse cached value from _check_delegations if set).
            if self._parent_name is None:
                try:
                    parent_resp = await client.get(
                        f"{self.platform_url}/workspaces/{self.workspace_id}",
                        headers=auth_headers(),
                    )
                    if parent_resp.status_code == 200:
                        parent_id = parent_resp.json().get("parent_id", "")
                        if parent_id:
                            parent_info = await client.get(
                                f"{self.platform_url}/workspaces/{parent_id}",
                                headers=auth_headers(),
                            )
                            if parent_info.status_code == 200:
                                self._parent_name = parent_info.json().get("name", "")
                    if self._parent_name is None:
                        self._parent_name = ""
                except Exception:
                    self._parent_name = ""
            parent_name = self._parent_name or ""

            report_instruction = ""
            if parent_name:
                report_instruction = (
                    f"\n\nIMPORTANT: Delegate a summary of these results to your parent "
                    f"'{parent_name}' using delegate_task. Also use send_message_to_user "
                    f"to notify the user."
                )
            else:
                report_instruction = (
                    "\n\nReport results using send_message_to_user to notify the user."
                )

            trigger_msg = (
                "Delegation results are ready (from a2a_receive via activity_logs). "
                "Review them and take appropriate action:\n"
                + "\n".join(summary_lines)
                + report_instruction
            )

            now = time.time()
            if now - self._last_self_message_time < SELF_MESSAGE_COOLDOWN:
                logger.debug(
                    "Heartbeat: self-message cooldown active; "
                    "a2a_receive results will be retried next cycle"
                )
            elif not _kernel_allows_autonomous_injection(kernel.KIND_DELEGATION_RESULT):
                # MUST-FIX 2 (kernel-ON): same pre-injection breaker gate as the
                # /delegations harvester above. Kernel OFF -> allow -> byte-identical.
                logger.info(
                    "Heartbeat: autonomous-loop breaker OPEN — dropping "
                    "a2a_receive delegation-result self-wake (kernel)"
                )
            else:
                self._last_self_message_time = now
                try:
                    await client.post(
                        f"{self.platform_url}/workspaces/{self.workspace_id}/a2a",
                        json={
                            "method": "message/send",
                            # #2251: single model-based builder — params
                            # generated FROM the receiver's a2a-sdk v0.3
                            # SendMessageRequest schema.
                            "params": build_message_send_params(
                                trigger_msg,
                                metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_HARVESTER},
                            ),
                        },
                        headers=self_source_headers(self.workspace_id),
                        timeout=120.0,
                    )
                    logger.info("Heartbeat: a2a_receive self-message sent")
                except Exception as e:
                    logger.warning("Heartbeat: failed to send a2a_receive self-message: %s", e)

            # Also notify the user via canvas — failure-class only. The
            # a2a_receive path currently hardcodes status="completed"
            # (peer responses are by definition successful deliveries),
            # so this loop is normally a no-op now — that is the fix
            # for task #384 canvas pollution. If a future change emits
            # failure rows here, the gate + double-prefix dedupe below
            # keep them clean.
            for r in new_results:
                if r.get("status") not in NOTIFY_STATUSES:
                    continue
                try:
                    summary = (r.get("summary") or "").lstrip()
                    # Strip any pre-existing "Delegation completed:" prefix
                    # on the upstream summary so we don't double up.
                    if summary.startswith(_DELEGATION_PREFIX):
                        summary = summary[len(_DELEGATION_PREFIX):].lstrip()
                    reason = r.get("error") or summary or "(no detail)"
                    msg = f"Delegation {r['status']}: {reason[:200]}"
                    await client.post(
                        f"{self.platform_url}/workspaces/{self.workspace_id}/notify",
                        json={"message": msg, "type": "delegation_result"},
                        headers=auth_headers(),
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.debug("Activity delegation check error: %s", e)
