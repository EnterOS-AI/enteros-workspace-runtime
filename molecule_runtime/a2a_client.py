"""A2A protocol client — peer discovery, messaging, and workspace info.

Shared constants (WORKSPACE_ID, PLATFORM_URL) live here so that
a2a_tools and a2a_mcp_server can import them from a single place.
"""

import asyncio
import logging
import os
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import httpx

import molecule_runtime.a2a_response as a2a_response
from molecule_runtime.platform_auth import (
    auth_headers,
    get_workspace_platform_url,
    self_source_headers,
    validate_workspace_id as _validate_workspace_id,
)

if TYPE_CHECKING:
    # SSOT typed payload (molecule-contracts / RFC molecule-core#3285), published
    # as `molecule-ai-contracts` on the gitea PyPI registry. Imported under
    # TYPE_CHECKING ONLY — no hard runtime dependency (see the `[contracts]` extra
    # in pyproject.toml). Used to type the hand-built outbound A2A `message/send`
    # `params` against the SSOT A2A-envelope shape (which pins the load-bearing
    # v0.3 `kind` part discriminator). `raw` is a function-local annotation, which
    # CPython never evaluates at runtime, so this stays type-checker-only even
    # without `from __future__ import annotations`. Mirrors molecule-ai-sdk #36.
    from molecule_ai_contracts.workspace_comms_gen import A2aEnvelopeRequestParams

logger = logging.getLogger(__name__)

# Validate WORKSPACE_ID (CWE-20, issue #14) — flows into X-Workspace-ID
# headers on every outbound A2A call below.
#
# Lazy validation via PEP 562 module __getattr__ + a module-level
# helper: previously this block ran at MODULE IMPORT time and raised
# RuntimeError if WORKSPACE_ID was unset. That broke the publish-runtime
# wheel-build job (which imports the runtime for static analysis but
# has no WORKSPACE_ID), painting main red. We now defer validation to
# first access — module import never raises. Callers that don't
# actually use WORKSPACE_ID (e.g., the publish job's `python -m build`)
# can import freely; callers that do get the same RuntimeError on
# first access, with a clear message.
#
# IMPORTANT: PEP 562 `__getattr__` only fires for EXTERNAL attribute
# access (e.g. `a2a_client.WORKSPACE_ID` from another module). It
# does NOT resolve bare global references from inside this same
# module — so any internal code path that does `WORKSPACE_ID` directly
# would raise NameError, not the intended RuntimeError. To avoid that
# regression, every internal call site goes through the helper
# `_resolve_workspace_id()` (defined below) instead of a bare
# reference, and `__getattr__` for external callers also routes
# through the same helper. Both paths share the cache, so validation
# runs at most once per process.
_WORKSPACE_ID_cache: str | None = None


def _resolve_workspace_id() -> str:
    """Lazy WORKSPACE_ID resolver. Validates on first call, caches thereafter.

    Used by both the PEP 562 `__getattr__` path (external access) and
    all internal call sites in this module (which would otherwise
    NameError on a bare `WORKSPACE_ID` reference). Returns the
    validated ID; raises RuntimeError on unset or invalid.
    """
    global _WORKSPACE_ID_cache
    # Always check for test override first (monkeypatch compat). pytest
    # monkeypatch.setattr(obj, 'WORKSPACE_ID', ...) does a getattr before
    # setattr, which triggers __getattr__ -> _resolve_workspace_id() and
    # populates the cache with the env value BEFORE the monkeypatch lands.
    # By checking __dict__ directly on every call we respect overrides
    # without needing to clear the cache after every setattr.
    _override = globals().get("WORKSPACE_ID")
    if _override is not None and isinstance(_override, str):
        return _override
    if _WORKSPACE_ID_cache is None:
        _raw = os.environ.get("WORKSPACE_ID")
        if not _raw:
            raise RuntimeError(
                "WORKSPACE_ID environment variable is required but not set"
            )
        try:
            _WORKSPACE_ID_cache = _validate_workspace_id(_raw)
        except ValueError as _exc:
            raise RuntimeError(
                f"WORKSPACE_ID failed validation: {_exc}"
            ) from _exc
    return _WORKSPACE_ID_cache


def __getattr__(name: str):  # PEP 562
    if name == "WORKSPACE_ID":
        return _resolve_workspace_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Platform URL: always host.docker.internal inside containers. The platform API
# is only reachable via the Docker network mesh from inside a workspace
# container regardless of the runtime environment (Docker/host).
PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://host.docker.internal:8080")


def _resolve_platform_url(src: str | None) -> str:
    """Return the platform URL to use for an outbound call from ``src``.

    Single resolution gate for the multi-tenant platform_url support
    introduced in RFC#601 / task #296 Phase B0. Every outbound URL
    construction below routes through here so the policy lives in one
    place:

        1. ``src`` set AND that workspace has a per-entry platform_url
           registered → use it. Lets a multi-tenant external agent
           dispatch each workspace's outbound HTTP to the correct
           platform host without each tool re-deriving the lookup.
        2. Otherwise → fall through to the module-level ``PLATFORM_URL``
           constant. This is the only path single-workspace callers
           ever see; their behavior is unchanged.

    Returning ``str`` (not ``str | None``) keeps every call-site's f-string
    interpolation safe — the helper always picks SOME url and the empty
    string is reserved for "no value available" which the legacy path
    never produced. Single-workspace + same-tenant multi-workspace
    operators get identical URLs to the pre-RFC#601 code.
    """
    if src:
        ws_url = get_workspace_platform_url(src)
        if ws_url:
            return ws_url
    return PLATFORM_URL

# Cache workspace ID → name mappings (populated by list_peers calls)
_peer_names: dict[str, str] = {}

# Cache: peer workspace_id → the source workspace_id whose registry
# returned that peer. Populated by ``a2a_tools.tool_list_peers`` whenever
# it queries a specific workspace's peers — so a later
# ``tool_delegate_task(target)`` can auto-route through the correct
# source workspace without the agent having to specify
# ``source_workspace_id`` explicitly.
#
# Single-workspace mode: dict stays empty, all delegations fall through
# to the module-level WORKSPACE_ID (existing behavior).
#
# Multi-workspace mode: as the agent calls list_peers, this map is
# populated with each peer's source. Subsequent delegate_task calls
# auto-route. If a peer is registered under multiple sources (rare —
# e.g. an org-wide capability) the LAST observed source wins; the agent
# can override by passing ``source_workspace_id`` explicitly.
_peer_to_source: dict[str, str] = {}

# Cache workspace ID → full peer record (id, name, role, status, url, ...).
# Populated by tool_list_peers and by the lazy registry lookup in
# enrich_peer_metadata. The notification-callback path (channel envelope
# enrichment) reads this cache on every inbound peer_agent push, so the
# read shape stays a dict-like ``__getitem__`` lookup; entries carry
# their fetched-at timestamp so TTL eviction is in-line with the
# lookup. ``None`` as the record is the negative-cache sentinel:
# registry failure is cached for one TTL window so we don't re-fire
# the 2s-bounded GET on every push from a flaky peer.
#
# OrderedDict + maxsize bound (#2482): pre-fix this was an unbounded
# ``dict``, so a workspace receiving from N distinct peers across its
# lifetime accumulated ~100 bytes/entry × N indefinitely. At 10K peers
# that's ~1 MB; at 100K (a chatty platform-wide router) ~10 MB; not
# crash-class but unbounded. The LRU bound caps memory + the TTL caps
# per-entry staleness — both gates are needed because a runaway poller
# touching N new peer_ids per push could grow within a single TTL
# window.
#
# All reads / writes go through ``_peer_metadata_get`` /
# ``_peer_metadata_set`` so the LRU move-to-end + size-trim invariants
# stay co-located. Direct mutation is allowed only in test fixtures
# (clearing for isolation); production code path uses the helpers.
_PEER_METADATA_MAXSIZE = 1024
_peer_metadata: "OrderedDict[str, tuple[float, dict | None]]" = OrderedDict()
_peer_metadata_lock = threading.Lock()

# How long an entry in ``_peer_metadata`` is treated as fresh. 5 minutes
# is the same window we use for delegation routing — long enough that a
# busy agent receiving repeated pushes from one peer doesn't hit the
# registry on every push, short enough that role/name renames propagate
# within a single agent session.
_PEER_METADATA_TTL_SECONDS = 300.0


def _peer_metadata_get(canon: str) -> tuple[float, dict | None] | None:
    """Read with LRU touch — moves the entry to the most-recently-used
    position so steady-state pushes from a busy peer don't get evicted
    by a cold-start burst from new peers. Returns the raw tuple shape
    callers expect; TTL eviction stays at the call site.
    """
    with _peer_metadata_lock:
        entry = _peer_metadata.get(canon)
        if entry is not None:
            _peer_metadata.move_to_end(canon)
        return entry


def _peer_metadata_set(canon: str, value: tuple[float, dict | None]) -> None:
    """Write + evict-if-over-maxsize. The eviction is in-process and
    cheap (popitem(last=False) on an OrderedDict is O(1)). Holding the
    lock across the trim keeps the size invariant stable under concurrent
    writes from background enrichment workers.
    """
    with _peer_metadata_lock:
        _peer_metadata[canon] = value
        _peer_metadata.move_to_end(canon)
        # Trim the oldest entries until at-or-below maxsize. The bound
        # is a soft cap — a single overrun (set called when at maxsize)
        # evicts the LRU entry before returning, never letting size
        # exceed maxsize.
        while len(_peer_metadata) > _PEER_METADATA_MAXSIZE:
            _peer_metadata.popitem(last=False)


# Background-fetch executor for enrich_peer_metadata_nonblocking (#2484).
# A small pool — peers are highly TTL-cached, so the steady-state load
# is "one fetch per peer per 5 minutes." Two workers handle the cold-
# start burst when an agent starts receiving pushes from a new peer for
# the first time without backing up the inbox poller. Daemon threads:
# the executor must NOT block process exit if the inbox shuts down.
_enrich_executor: ThreadPoolExecutor | None = None
_enrich_executor_lock = threading.Lock()

# In-flight peer IDs — guards against a single peer's repeated pushes
# scheduling N concurrent registry fetches before the first one fills
# the cache. Set membership is "a worker is currently fetching this
# peer; subsequent calls should NOT schedule another."
_enrich_in_flight: set[str] = set()
_enrich_in_flight_lock = threading.Lock()


def _get_enrich_executor() -> ThreadPoolExecutor:
    """Lazy-init the enrichment worker pool. Lazy because most test
    fixtures and short-lived CLI invocations don't need it; only the
    long-running molecule-mcp / inbox-poller path actually schedules
    background fetches.
    """
    global _enrich_executor
    if _enrich_executor is not None:
        return _enrich_executor
    with _enrich_executor_lock:
        if _enrich_executor is None:
            _enrich_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="enrich-peer",
            )
    return _enrich_executor


def enrich_peer_metadata_nonblocking(
    peer_id: str,
    source_workspace_id: str | None = None,
) -> dict | None:
    """Cache-first variant of ``enrich_peer_metadata`` — returns
    immediately without blocking on a registry GET.

    Behavior:
      - Cache hit (fresh): return the cached record.
      - Cache miss or TTL expired: schedule a background fetch via the
        worker pool, return ``None`` (caller renders bare peer_id).
        The next push for this peer hits the warm cache and gets the
        full record.

    Why this exists (#2484): the inbox poller's notification callback
    in molecule-mcp called the synchronous ``enrich_peer_metadata`` on
    every push, blocking the poller for up to 2s × N uncached peers
    per batch. Push-delivery latency was gated on registry latency —
    the exact thing the negative-cache patch in PR #2471 was supposed
    to avoid amplifying. Moving the fetch off the poller thread means
    push delivery is bounded by the inbox poll interval, never by
    registry RTT.

    Trade-off: the FIRST push from a new peer arrives metadata-light
    (no name/role). The MCP host renders the bare peer_id. Subsequent
    pushes (within the 5-min TTL) hit the warm cache and get the full
    record. Acceptable because:
      - Channel-envelope enrichment is a UX nicety, not a correctness
        invariant.
      - The cold-cache window per peer is bounded to one push.
      - The TTL is long enough that an active conversation never
        re-enters the cold state.
    """
    canon = _validate_peer_id(peer_id)
    if canon is None:
        return None
    # Cache hit (fresh): return without blocking on a registry GET.
    # This is the hot path for active peer conversations — avoids
    # spawning a background thread for every push from a known peer.
    current = time.monotonic()
    cached = _peer_metadata_get(canon)
    if cached is not None:
        fetched_at, record = cached
        if current - fetched_at < _PEER_METADATA_TTL_SECONDS:
            return record
    # Cache miss or TTL expired: schedule background fetch unless one is
    # already in flight for this peer. The in-flight set keeps a flurry
    # of pushes from one peer (e.g., a chatty agent) from spawning N
    # parallel GETs.
    with _enrich_in_flight_lock:
        if canon in _enrich_in_flight:
            return None
        _enrich_in_flight.add(canon)
    try:
        _get_enrich_executor().submit(
            _enrich_peer_metadata_worker, canon, source_workspace_id
        )
    except RuntimeError:
        # Executor was shut down (process exit path) — drop the request,
        # let the caller render bare peer_id.
        with _enrich_in_flight_lock:
            _enrich_in_flight.discard(canon)
    return None


def _enrich_peer_metadata_worker(
    canon: str, source_workspace_id: str | None
) -> None:
    """Background-thread body for ``enrich_peer_metadata_nonblocking``.
    Runs the same fetch logic as the synchronous helper but discards
    the return value — the cache write is the only output anyone
    needs. Always clears the in-flight marker so a future cache miss
    can retry.
    """
    try:
        enrich_peer_metadata(canon, source_workspace_id)
    except Exception as exc:  # noqa: BLE001
        # Background workers must not crash the executor — log and
        # move on. The negative-cache path inside enrich_peer_metadata
        # already records failures, so a re-attempt is rate-limited
        # by TTL.
        logger.debug("_enrich_peer_metadata_worker: %s failed: %s", canon, exc)
    finally:
        with _enrich_in_flight_lock:
            _enrich_in_flight.discard(canon)


def _wait_for_enrichment_inflight_for_testing(timeout: float = 2.0) -> None:
    """Block until all in-flight enrichment workers have completed.

    Test-only helper. Production code never has a reason to wait — the
    point of the nonblocking path is that callers don't care when the
    cache fills. Tests that want to assert "after the worker runs, the
    cache has the record" use this to synchronise without sleeping.

    Polls ``_enrich_in_flight`` rather than holding a Condition because
    the worker pool is already serializing through ``_enrich_in_flight_lock``;
    poll keeps the production hot path lock-free.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _enrich_in_flight_lock:
            if not _enrich_in_flight:
                return
        time.sleep(0.01)


def _peer_in_flight_clear_for_testing() -> None:
    """Clear the in-flight enrichment set. Test-only helper."""
    with _enrich_in_flight_lock:
        _enrich_in_flight.clear()


def enrich_peer_metadata(
    peer_id: str,
    source_workspace_id: str | None = None,
    *,
    now: float | None = None,
) -> dict | None:
    """Return cached or freshly-fetched metadata for ``peer_id``.

    Sync helper — safe to call from the inbox poller's notification
    callback thread (which is not async). Hits the in-process cache
    first; on miss or TTL expiry, GETs ``/registry/discover/<peer_id>``
    synchronously with a tight timeout. Returns None on validation
    failure, network failure, or non-200 response so callers can
    degrade gracefully (the channel envelope falls back to the raw
    ``peer_id`` instead of crashing the push path).

    Negative caching: failure outcomes (4xx/5xx/non-JSON/network
    exception) are stored as ``(now, None)`` and treated as
    fresh-but-empty for the TTL window. Without this, a peer with a
    flaky/missing registry record would re-fire the 2s-bounded GET on
    EVERY push — turning the cache into a no-op for the exact failure
    scenarios it most needs to defend against.

    The fetched dict is stored as-is, so callers can read whatever
    fields the platform exposes (currently: ``id``, ``name``, ``role``,
    ``status``, ``url``). New fields surface automatically without a
    code change here.
    """
    canon = _validate_peer_id(peer_id)
    if canon is None:
        return None

    current = now if now is not None else time.monotonic()
    cached = _peer_metadata_get(canon)
    if cached is not None:
        fetched_at, record = cached
        if current - fetched_at < _PEER_METADATA_TTL_SECONDS:
            # Fresh entry — return whatever's there. ``None`` is the
            # negative-cache sentinel: caller treats absence of fields
            # the same as a registry miss, which is the desired UX.
            return record

    src = (source_workspace_id or "").strip() or _resolve_workspace_id()
    url = f"{_resolve_platform_url(src)}/registry/discover/{canon}"
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url, headers={"X-Workspace-ID": src, **auth_headers(src)})
    except Exception as exc:  # noqa: BLE001
        logger.debug("enrich_peer_metadata: GET %s failed: %s", url, exc)
        _peer_metadata_set(canon, (current, None))
        return None

    if resp.status_code != 200:
        logger.debug(
            "enrich_peer_metadata: %s returned HTTP %d", url, resp.status_code
        )
        _peer_metadata_set(canon, (current, None))
        return None

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        _peer_metadata_set(canon, (current, None))
        return None
    if not isinstance(data, dict):
        _peer_metadata_set(canon, (current, None))
        return None

    _peer_metadata_set(canon, (current, data))
    if name := data.get("name"):
        _peer_names[canon] = name
    return data


def _agent_card_url_for(peer_id: str) -> str:
    """Construct the platform-side agent-card URL for ``peer_id``.

    Returns the empty string when ``peer_id`` is not a UUID — same
    trust-boundary rationale as ``discover_peer``: never interpolate
    path-traversal characters into a URL. An invalid id reflected back
    to the receiving agent as ``…/registry/discover/../../foo`` is a
    foothold we close at construction time.

    Uses the registry's discovery path so the agent receiving a push
    can hit a single endpoint to enumerate the sender's capabilities
    + role + URL. Same shape every workspace exposes regardless of
    runtime — claude-code, hermes, langchain wrappers all register
    through ``/registry/register`` and surface through ``/registry/discover``.
    """
    safe_id = _validate_peer_id(peer_id)
    if safe_id is None:
        return ""
    return f"{PLATFORM_URL}/registry/discover/{safe_id}"

# Sentinel prefix for errors originating from send_a2a_message / child agents.
# Used by delegate_task to distinguish real errors from normal response text.
_A2A_ERROR_PREFIX = "[A2A_ERROR] "

# Sentinel prefix for queued-for-poll-mode-peer outcomes (#2967).
# When the target workspace is registered as delivery_mode=poll (no
# public URL — typical for external molecule-mcp standalone runtimes),
# the platform's a2a_proxy.go:402 short-circuit returns a synthetic
# {"status":"queued","delivery_mode":"poll","method":"..."} envelope
# instead of dispatching over HTTP. The message IS delivered (written
# to the platform's inbox queue); there's just no synchronous reply
# to relay. Pre-#2967 the client treated this as "unexpected response
# shape" → caller saw DELEGATION FAILED → retried → recipient saw
# duplicates. The Queued prefix lets callers branch on this outcome
# explicitly: "delivered async, no synchronous reply expected" is
# different from both success-with-text and failure.
_A2A_QUEUED_PREFIX = "[A2A_QUEUED] "

# A2A `message/send` envelope normalization — the DEFENSIVE BACKSTOP for
# params we did NOT construct (inbound / proxy paths). Outbound
# construction is owned by ``build_message_send_params`` below, which
# generates the body FROM the a2a-sdk model (#2251 SSOT upgrade).
#
# Root cause (#2251): the receiver runs a2a-sdk 1.x with
# `enable_v0_3_compat=True` (see boot_routes.py). That compat layer
# validates the inbound body against a Pydantic `SendMessageRequest`
# whose `Message.role` and `Message.parts` are BOTH required. An
# outbound envelope that omits `role` is rejected at parse time with
#   "1 validation error for SendMessageRequest
#    params.message.role  Field required"
# and the JSON-RPC call fails with -32600 Invalid Request — silently
# breaking the whole delegation (this is the agents-team transport-retry
# storm root). The part discriminator (`type` vs `kind`) is NOT
# rejected — the v0.3 TextPart defaults `kind="text"` and tolerates an
# extra `type` — but we normalize it to `kind` anyway so the wire shape
# matches the schema's canonical field and never drifts.
#
# This normalizer is still the implementation the builder's no-SDK
# fallback delegates to, and remains available for any path that
# receives a hand-shaped params dict from elsewhere. New OUTBOUND sends
# must call ``build_message_send_params``, not this.
_A2A_VALID_ROLES = ("user", "agent")
_A2A_DEFAULT_ROLE = "user"


def _normalize_a2a_part(part: Any) -> dict[str, Any]:
    """Normalize a single A2A message part to the v0.3 ``kind`` shape.

    Accepts the legacy ``{"type": "text", ...}`` shape (emitted by some
    older builders) and rewrites the discriminator to ``kind`` — the
    field name the a2a-sdk v0.3 schema canonicalizes on. Non-dict parts
    are returned unchanged so the schema validator (not this helper) is
    the one that rejects genuinely malformed parts with a clear error.
    """
    if not isinstance(part, dict):
        return part
    out = dict(part)
    # Promote legacy `type` discriminator to `kind` when `kind` is absent.
    # If both are present, `kind` wins (it's the schema field); drop the
    # redundant `type` so the wire shape is unambiguous.
    if "kind" not in out and "type" in out:
        out["kind"] = out["type"]
    out.pop("type", None)
    return out


def normalize_a2a_message_send_params(
    params: dict[str, Any], *, default_text: str | None = None
) -> dict[str, Any]:
    """Return a copy of ``params`` with a schema-valid ``message`` (#2251).

    Guarantees the outbound ``message/send`` ``params.message`` carries:

      * ``role`` — defaulted to ``"user"`` when absent or not one of the
        v0.3-valid roles (``user`` / ``agent``). Missing role is the
        exact field that triggers the receiver's
        ``SendMessageRequest params.message.role Field required``
        rejection — defaulting it here makes the failure impossible.
      * ``messageId`` — defaulted to a fresh UUID when absent.
      * ``parts`` — each part normalized to the ``kind`` discriminator
        (``type`` → ``kind``). When ``parts`` is absent and
        ``default_text`` is supplied, a single text part is synthesized.

    Pure + side-effect-free (operates on a shallow copy) so it is safe to
    call from any builder. The receiver's Pydantic schema remains the
    authority on full validity — this helper only closes the
    deterministic gaps (role / messageId / part discriminator) that our
    own builders are responsible for.
    """
    out = dict(params)
    message = dict(out.get("message") or {})

    role = message.get("role")
    if role not in _A2A_VALID_ROLES:
        message["role"] = _A2A_DEFAULT_ROLE

    if not message.get("messageId"):
        message["messageId"] = str(uuid.uuid4())

    parts = message.get("parts")
    if isinstance(parts, list):
        message["parts"] = [_normalize_a2a_part(p) for p in parts]
    elif parts is None and default_text is not None:
        message["parts"] = [{"kind": "text", "text": default_text}]

    out["message"] = message
    return out


# Canonical OUTBOUND `message/send` params builder (#2251 — SSOT upgrade).
#
# This is the single source of truth for constructing an outbound
# `message/send` ``params`` body. Every outbound send in the runtime
# funnels through here so the wire shape is GENERATED FROM the a2a-sdk
# v0.3 ``MessageSendParams`` Pydantic schema rather than hand-rolled and
# normalized after the fact. Because the model is the same one the
# receiver validates against (``a2a.compat.v0_3.types`` — see
# boot_routes.py ``enable_v0_3_compat=True`` and
# tests/test_a2a_message_send_contract.py), the envelope cannot drift
# out of schema: a field the schema requires (``role`` / ``messageId`` /
# ``parts`` discriminator) is produced by the model's own serializer,
# not by a literal a future edit might break.
#
# ``normalize_a2a_message_send_params`` above is retained ONLY as a
# defensive backstop for inbound / proxy params we did not construct.
# All OUTBOUND construction goes through ``build_message_send_params``.
_A2A_COMPAT_TYPES_MODULE = "a2a.compat.v0_3.types"

# --- Self-turn session convergence -----------------------------------------
# Every turn the runtime sends to ITSELF (idle / harvester / delegation-result /
# cron / scheduler / goal-nudge / lifecycle boot + reprovision wake) carries a
# ``source_type`` in ``params.metadata`` whose value starts with this prefix.
# a2a_executor.py owns the canonical A2A_SOURCE_SELF_* constants + the routine
# drop set; here we only need the shared PREFIX (import-free to avoid an
# a2a_executor<->a2a_client cycle) to recognise "this is a self-turn".
_A2A_SOURCE_TYPE_KEY = "source_type"
_SELF_SOURCE_TYPE_PREFIX = "self-"

# Env the platform MAY set to hand the runtime the authoritative default-session
# id; absent it we derive the same value the platform's canvasSessionContextID
# produces (see below).
_DEFAULT_SESSION_CONTEXT_ID_ENV = "MOLECULE_DEFAULT_SESSION_CONTEXT_ID"


def default_self_turn_context_id() -> str:
    """The stable per-workspace session id a self-originated turn lands in.

    CORE owns this id. The platform's provisioner injects it into every
    workspace container as ``MOLECULE_DEFAULT_SESSION_CONTEXT_ID`` (from
    workspace-server ``internal/sessionid.DefaultContextID`` — the SAME authority
    the a2a proxy belt + platform restart / first-boot self-turns stamp), and we
    read it at HIGHEST precedence so the runtime consumes core's value rather than
    re-deriving the convention. Absent the env (unit / CLI / an older platform
    that predates the injection) we fall back to deriving ``canvas-<WORKSPACE_ID>``
    — currently identical to what core produces.

    Without a stamped id, a self-turn is POSTed to the local executor with NO
    ``contextId``, the a2a-sdk mints a FRESH ``context_id`` per turn, and
    ``TracingExecutor`` (which reads ``session_id`` from ``context.context_id``)
    files every idle / harvester / delegation-result / boot self-wake under its
    own throwaway Langfuse session — the "session keeps changing after a restart /
    plugin install" fragmentation.

    Returns ``""`` when neither env nor WORKSPACE_ID is available — the caller
    then leaves ``contextId`` unset, preserving legacy behaviour.
    """
    override = os.environ.get(_DEFAULT_SESSION_CONTEXT_ID_ENV, "").strip()
    if override:
        return override
    ws = os.environ.get("WORKSPACE_ID", "").strip()
    return f"canvas-{ws}" if ws else ""


def _metadata_is_self_source(metadata: dict[str, Any] | None) -> bool:
    """True when ``metadata`` marks the send as a runtime self-turn.

    Keyed on the ``self-`` ``source_type`` prefix so EVERY current and future
    self-* kind converges automatically (no per-kind allowlist to keep in sync).
    Peer delegations (``parent_task_id`` / ``source_workspace_id`` metadata) and
    user/canvas sends carry no ``self-`` source_type, so they are untouched and
    keep minting their own per-conversation context.
    """
    if not isinstance(metadata, dict):
        return False
    st = metadata.get(_A2A_SOURCE_TYPE_KEY)
    return isinstance(st, str) and st.startswith(_SELF_SOURCE_TYPE_PREFIX)


def _build_message_send_params_from_model(
    text: str,
    *,
    role: str,
    message_id: str,
    metadata: dict[str, Any] | None,
    attachments: list[dict[str, Any]] | None,
    context_id: str | None = None,
):
    """Construct the real a2a-sdk ``MessageSendParams`` and dump it.

    Returns ``None`` when a2a-sdk is not importable (the stubbed unit
    test environment — ``conftest.py`` stubs ``a2a.types`` but not the
    ``a2a.compat.v0_3`` compat layer), letting the caller fall back to
    the hand-built canonical dict. CI installs the real wheel, so the
    model path is what the contract test exercises.
    """
    try:
        import importlib

        t = importlib.import_module(_A2A_COMPAT_TYPES_MODULE)
    except ImportError:
        return None

    parts = [t.Part(root=t.TextPart(text=text))]
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        # Accept either {"uri": ...} or {"bytes": ...} file descriptors;
        # let the schema decide which FileWithUri/FileWithBytes variant
        # applies and reject anything malformed with a clear error.
        if "bytes" in att:
            file_obj = t.FileWithBytes(**att)
        else:
            file_obj = t.FileWithUri(**att)
        parts.append(t.Part(root=t.FilePart(file=file_obj)))

    message = t.Message(role=role, messageId=message_id, parts=parts)
    params = t.MessageSendParams(message=message, metadata=metadata)
    # by_alias → camelCase wire field names (messageId, etc.);
    # exclude_none → omit unset optionals so the body matches what the
    # hand-rolled fallback produces byte-for-byte where they overlap.
    dumped = params.model_dump(by_alias=True, exclude_none=True)
    # Stamp contextId post-dump (not via the Message constructor) so we do not
    # depend on the compat model's field-name-vs-alias population rules — the
    # wire key is always the camelCase ``contextId`` the receiver reads.
    if context_id:
        dumped.setdefault("message", {})["contextId"] = context_id
    return dumped


def build_message_send_params(
    text: str,
    *,
    role: str = "user",
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Build a schema-valid outbound ``message/send`` ``params`` body.

    THE single canonical builder for outbound A2A sends (#2251). The
    returned dict is suitable as the ``params`` value of a JSON-RPC
    ``message/send`` envelope and is guaranteed to satisfy the
    receiver's a2a-sdk v0.3 ``SendMessageRequest`` schema:

      * ``role`` — required; defaults to ``"user"``. An invalid role is
        coerced to the default rather than shipping a body the receiver
        rejects with ``params.message.role``.
      * ``messageId`` — defaults to a fresh UUID when not supplied.
      * ``parts`` — the ``text`` becomes a ``TextPart(kind="text")``;
        each ``attachments`` entry becomes a ``FilePart``.
      * ``metadata`` — passed through as the sibling ``params.metadata``
        when supplied (e.g. delegation's ``parent_task_id`` chain).
      * ``contextId`` — session id stamped on ``params.message``. An explicit
        ``context_id`` always wins; otherwise, when ``metadata`` marks this as a
        runtime self-turn (``source_type`` starts with ``self-``), it defaults
        to :func:`default_self_turn_context_id` so idle / harvester /
        delegation-result / boot / reprovision self-wakes converge on the
        workspace's stable session instead of each minting a throwaway one
        (Langfuse session fragmentation across restart / plugin install). Peer
        and user sends carry no self-``source_type`` and are left unstamped, so
        they keep their own per-conversation context.

    When a2a-sdk is installed (CI + the workspace image) the body is
    GENERATED FROM the real Pydantic model so it cannot drift from the
    schema. In the stubbed unit-test env (no compat layer) it falls
    back to ``normalize_a2a_message_send_params`` over the same inputs,
    which produces the identical canonical shape.
    """
    if role not in _A2A_VALID_ROLES:
        role = _A2A_DEFAULT_ROLE
    if not message_id:
        message_id = str(uuid.uuid4())

    # An explicit contextId wins; otherwise a self-turn inherits the stable
    # default session so its trace groups with the canvas conversation.
    effective_context_id = context_id
    if not effective_context_id and _metadata_is_self_source(metadata):
        effective_context_id = default_self_turn_context_id()

    from_model = _build_message_send_params_from_model(
        text,
        role=role,
        message_id=message_id,
        metadata=metadata,
        attachments=attachments,
        context_id=effective_context_id,
    )
    if from_model is not None:
        return from_model

    # Fallback (a2a-sdk absent): build the same canonical dict by hand
    # and run it through the normalizer so the shape is identical to the
    # model-dumped path on the fields they share.
    parts: list[dict[str, Any]] = [{"kind": "text", "text": text}]
    for att in attachments or []:
        if isinstance(att, dict):
            parts.append({"kind": "file", "file": dict(att)})
    # Typed against the SSOT contract (molecule-contracts A2aEnvelopeRequestParams)
    # for drift-prevention; the wire shape is unchanged and still flows through
    # normalize_a2a_message_send_params below.
    message: dict[str, Any] = {"role": role, "messageId": message_id, "parts": parts}
    if effective_context_id:
        message["contextId"] = effective_context_id
    raw: A2aEnvelopeRequestParams = {"message": message}
    if metadata is not None:
        raw["metadata"] = metadata
    return normalize_a2a_message_send_params(raw)


# Workspace IDs are UUIDs everywhere we generate them (platform's
# workspaces.id column, /registry/discover/:id route param, etc.) but
# the agent-facing tool surface receives them as free-form strings via
# tool args. ``_validate_peer_id`` enforces UUID-shape at the
# trust boundary so we never interpolate `..` or `/` into a URL path,
# never silently coerce malformed input into a 404, and surface a
# clear error to the agent rather than letting an HTTP 4xx bubble up
# from the platform with a generic error message.
#
# Lenient on case + whitespace because real-world peer-id strings
# come from list_peers/discover_peer responses (canonical lowercase)
# or hand-typed agent input (mixed-case acceptable). Strict on
# everything else.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_peer_id(peer_id: str) -> str | None:
    """Return the canonicalised peer_id if valid, else None.

    Returning None instead of raising so callers in tool surfaces can
    convert to a friendly agent-facing string ("workspace_id is not a
    valid UUID") rather than crashing with a stack trace.
    """
    if not isinstance(peer_id, str):
        return None
    pid = peer_id.strip()
    if not _UUID_RE.match(pid):
        return None
    return pid.lower()


async def discover_peer(target_id: str, source_workspace_id: str | None = None) -> dict | None:
    """Discover a peer workspace's URL via the platform registry.

    Validates ``target_id`` is a UUID before constructing the URL — a
    malformed id can't reach the platform handler now, which both
    short-circuits an avoidable round-trip AND ensures we never
    interpolate path-traversal characters into the URL.

    ``source_workspace_id`` selects which registered workspace asks the
    question — both the X-Workspace-ID header AND the Authorization
    bearer token must come from the same workspace, otherwise the
    platform's TenantGuard rejects the request. Defaults to the
    module-level WORKSPACE_ID for back-compat with single-workspace
    callers.
    """
    safe_id = _validate_peer_id(target_id)
    if safe_id is None:
        return None
    src = (source_workspace_id or "").strip() or _resolve_workspace_id()
    base = _resolve_platform_url(src)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{base}/registry/discover/{safe_id}",
                headers={"X-Workspace-ID": src, **auth_headers(src)},
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            logger.error(f"Discovery failed for {target_id}: {e}")
            return None


# httpx exception classes that indicate a transient transport-layer
# failure worth retrying — the request never produced an application
# response, so a fresh attempt has a real chance of succeeding. Any
# error not in this tuple is treated as deterministic (HTTP-status,
# JSON parse, runtime-returned JSON-RPC error, etc.) and surfaced to
# the caller on the first try.
#
# Why each one belongs here:
#   - ConnectError / ConnectTimeout: peer's listening socket wasn't
#     ready (mid-restart, not yet bound). Fast failure, fast recovery.
#   - RemoteProtocolError: peer closed the TCP connection without
#     writing a response — observed on 2026-04-27 when a peer's prior
#     in-flight Claude SDK session aborted and the new request's
#     connection was reset mid-handler.
#   - ReadError / WriteError: TCP read/write socket error mid-flight,
#     typically a network blip on the Docker bridge or a peer worker
#     crash.
#   - ReadTimeout: peer didn't write ANY response bytes within the
#     300s read budget. Distinct from "peer is slow but progressing"
#     (which httpx surfaces as a successful read with chunked bytes).
#     Retry budget caps the worst case — see _DELEGATE_TOTAL_BUDGET_S.
_TRANSIENT_HTTP_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
)

# Retry budget. Up to 5 attempts (1 initial + 4 retries) with
# exponential backoff (1, 2, 4, 8 seconds), each backoff jittered ±25%
# to prevent synchronized retry storms across siblings if a peer flaps.
# _DELEGATE_TOTAL_BUDGET_S caps cumulative wall-clock so a string of
# ReadTimeouts can't make the caller wait 25 minutes — once the
# deadline elapses we stop retrying even if attempts remain. 600s = 10
# minutes is the agreed worst case the caller can tolerate before
# falling back to "peer unavailable" handling in tool_delegate_task.
_DELEGATE_MAX_ATTEMPTS = 5
_DELEGATE_BACKOFF_BASE_S = 1.0
_DELEGATE_BACKOFF_CAP_S = 16.0
_DELEGATE_TOTAL_BUDGET_S = 600.0


def _delegate_backoff_seconds(attempt_zero_indexed: int) -> float:
    """Return the (jittered) backoff delay before retrying after the
    given attempt index (0 = backoff before retry #1).

    Pure function so the schedule is unit-testable without monkey-
    patching asyncio.sleep. Jitter is symmetric ±25% on top of the
    capped exponential — enough to break sync across simultaneous
    callers without making the schedule unpredictable.
    """
    base = min(_DELEGATE_BACKOFF_BASE_S * (2 ** attempt_zero_indexed), _DELEGATE_BACKOFF_CAP_S)
    jitter = base * (0.5 * random.random() - 0.25)
    return max(0.0, base + jitter)


def _format_a2a_error(exc: BaseException, target_url: str) -> str:
    """Format an httpx exception as an [A2A_ERROR] string.

    Some httpx exceptions stringify to empty (RemoteProtocolError,
    ConnectionReset variants) — the canvas would then render
    "[A2A_ERROR] " with no detail and the operator has no signal to
    act on. Always include the exception class name and the target
    URL so the activity log + Agent Comms panel have actionable
    information without a trip through container logs.
    """
    msg = str(exc).strip()
    type_name = type(exc).__name__
    if not msg:
        detail = f"{type_name} (no message — likely connection reset or silent timeout)"
    elif msg.startswith(f"{type_name}:") or msg.startswith(f"{type_name} "):
        # Already prefixed with the type — don't double-prefix.
        # Prefix-anchored check (not substring) so a message that
        # happens to mention some OTHER class name mid-string
        # (e.g. "got OSError on read") doesn't suppress our own
        # type prefix and lose the diagnostic signal.
        detail = msg
    else:
        detail = f"{type_name}: {msg}"
    return f"{_A2A_ERROR_PREFIX}{detail} [target={target_url}]"


async def send_a2a_message(peer_id: str, message: str, source_workspace_id: str | None = None) -> str:
    """Send an A2A ``message/send`` to a peer workspace via the platform proxy.

    The target URL is constructed internally as
    ``${PLATFORM_URL}/workspaces/{peer_id}/a2a``. Going through the
    platform's A2A proxy is the only path that works for both
    in-container and external runtimes — see
    a2a_tools.tool_delegate_task for the rationale.

    ``source_workspace_id`` is the SENDING workspace — drives both the
    X-Workspace-ID source-tagging header and the bearer token. Defaults
    to the module-level WORKSPACE_ID for back-compat. Multi-workspace
    operators pass it explicitly so each registered workspace's peers
    are reached via their own auth chain.

    Auto-retries up to _DELEGATE_MAX_ATTEMPTS times on transient
    transport-layer errors (RemoteProtocolError, ConnectError,
    ReadTimeout, etc.) with exponential-backoff + jitter, capped by
    _DELEGATE_TOTAL_BUDGET_S. Application-level failures (HTTP 4xx,
    JSON-RPC error response, malformed JSON) are NOT retried — they
    indicate a deterministic problem retry won't fix.
    """
    safe_id = _validate_peer_id(peer_id)
    if safe_id is None:
        return f"{_A2A_ERROR_PREFIX}invalid peer_id (expected UUID): {peer_id!r}"
    src = (source_workspace_id or "").strip() or _resolve_workspace_id()
    target_url = f"{_resolve_platform_url(src)}/workspaces/{safe_id}/a2a"

    # Fix F (Cycle 5 / H2 — flagged 5 consecutive audits): timeout=None allowed
    # a hung upstream to block the agent indefinitely. Use a generous but bounded
    # timeout: 30s connect + 300s read (long enough for slow LLM responses).
    timeout_cfg = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
    deadline = time.monotonic() + _DELEGATE_TOTAL_BUDGET_S
    last_exc: BaseException | None = None

    for attempt in range(_DELEGATE_MAX_ATTEMPTS):
        async with httpx.AsyncClient(timeout=timeout_cfg) as client:
            try:
                # self_source_headers() includes X-Workspace-ID so the
                # platform's a2a_receive logger records source_id =
                # WORKSPACE_ID. Otherwise peer-A2A messages — including
                # the case where target_url resolves to this workspace's
                # own /a2a — get logged with source_id=NULL and surface
                # in the recipient's My Chat tab as user-typed input.
                resp = await client.post(
                    target_url,
                    headers=self_source_headers(src),
                    json={
                        "jsonrpc": "2.0",
                        "id": str(uuid.uuid4()),
                        "method": "message/send",
                        # #2251: build params from the canonical a2a-sdk
                        # v0.3 model so role/messageId/parts are generated
                        # FROM the receiver's SendMessageRequest schema and
                        # cannot drift. Single builder for every outbound send.
                        "params": build_message_send_params(message),
                    },
                )
                # core#143: guard resp.json() — proxy/gateway 5xx responses can
                # return HTML/text bodies, and a partial body can also be
                # non-JSON. Swallowing these as JSONDecodeError produced the
                # opaque "proxy a2a error" that masked the real signal.
                # Surface the HTTP status + a body snippet instead, and do not
                # retry (a non-2xx from the proxy is not a transport-layer
                # transient; a 5xx may even have delivered the message).
                if resp.status_code < 200 or resp.status_code >= 300:
                    body = resp.text[:200]
                    return (
                        f"{_A2A_ERROR_PREFIX}A2A proxy returned HTTP "
                        f"{resp.status_code} (message may have been delivered); "
                        f"body={body!r} [target={target_url}]"
                    )
                content_type = resp.headers.get("content-type", "")
                if "application/json" not in content_type:
                    body = resp.text[:200]
                    return (
                        f"{_A2A_ERROR_PREFIX}A2A proxy returned non-JSON response "
                        f"(content-type={content_type!r}); body={body!r} "
                        f"[target={target_url}]"
                    )
                try:
                    data = resp.json()
                except Exception as e:
                    body = resp.text[:200]
                    return (
                        f"{_A2A_ERROR_PREFIX}A2A proxy response was not valid JSON "
                        f"({e}); body={body!r} [target={target_url}]"
                    )
                # Dispatch via the SSOT response model (a2a_response.py).
                # All shape detection lives in one place — the parser
                # never raises and routes unknown shapes to Malformed
                # so a future server-side change is loud, not silent.
                variant = a2a_response.parse(data)
                if isinstance(variant, a2a_response.Result):
                    # Match legacy semantics:
                    #   parts non-empty + first part has no text → ""
                    #   parts empty                              → "(no response)"
                    # Differentiation matters for callers that assert
                    # on the empty-string case (test_a2a_client).
                    if variant.parts:
                        text = variant.text
                    else:
                        text = "(no response)"
                    # Tag child-reported errors so the caller can
                    # detect them reliably — agent-side bug surfaces
                    # text like "Agent error: <traceback>" inside a
                    # JSON-RPC success envelope.
                    if text.startswith("Agent error:"):
                        return f"{_A2A_ERROR_PREFIX}{text}"
                    return text
                if isinstance(variant, a2a_response.Queued):
                    # Poll-mode peer — message accepted into the inbox
                    # queue, target agent will fetch via poll. NOT a
                    # failure. Return the queued sentinel so callers
                    # (delegate_task etc.) can render the outcome
                    # accurately instead of treating it as an error.
                    logger.info(
                        "send_a2a_message: queued for poll-mode peer (target=%s method=%s)",
                        target_url,
                        variant.method,
                    )
                    return f"{_A2A_QUEUED_PREFIX}target={safe_id} method={variant.method}"
                if isinstance(variant, a2a_response.Error):
                    msg = variant.message
                    code = variant.code
                    if msg and code is not None:
                        detail = f"{msg} (code={code})"
                    elif msg:
                        detail = msg
                    elif code is not None:
                        detail = f"JSON-RPC error with no message (code={code})"
                    else:
                        detail = "JSON-RPC error with no message"
                    if variant.restarting:
                        # Surface platform-restart-in-progress
                        # explicitly — caller (UI / delegating agent)
                        # can render a softer "agent is restarting"
                        # message rather than a generic failure.
                        retry = (
                            f", retry_after={variant.retry_after}s"
                            if variant.retry_after is not None
                            else ""
                        )
                        detail = f"{detail} (restarting{retry})"
                    return f"{_A2A_ERROR_PREFIX}{detail} [target={target_url}]"
                # Malformed — log loud + surface as error so the
                # operator notices a server change. SSOT refactor
                # subsumes the inline "queued" check that landed in
                # the #2972 hotfix; that branch is now the typed
                # Queued variant above.
                logger.warning(
                    "send_a2a_message: malformed response (target=%s body=%.200s)",
                    target_url,
                    str(variant.raw),
                )
                return (
                    f"{_A2A_ERROR_PREFIX}unexpected response shape "
                    f"(no result, error, or queued envelope): "
                    f"{str(variant.raw)[:200]} [target={target_url}]"
                )
            except _TRANSIENT_HTTP_ERRORS as e:
                last_exc = e
                attempts_remaining = _DELEGATE_MAX_ATTEMPTS - (attempt + 1)
                if attempts_remaining <= 0 or time.monotonic() >= deadline:
                    # Out of attempts OR out of total budget — surface
                    # the last error to the caller.
                    break
                delay = _delegate_backoff_seconds(attempt)
                # Don't sleep past the deadline — clamp.
                remaining = deadline - time.monotonic()
                if delay > remaining:
                    delay = max(0.0, remaining)
                logger.warning(
                    "send_a2a_message: transient %s on attempt %d/%d, retrying in %.1fs (target=%s)",
                    type(e).__name__,
                    attempt + 1,
                    _DELEGATE_MAX_ATTEMPTS,
                    delay,
                    target_url,
                )
                await asyncio.sleep(delay)
                continue
            except Exception as e:
                # Non-transient (HTTP-status, JSON parse, etc.) — don't retry.
                return _format_a2a_error(e, target_url)
    # Retries exhausted (or budget elapsed). last_exc must be set
    # because we only break out of the loop after assigning it.
    assert last_exc is not None  # noqa: S101
    return _format_a2a_error(last_exc, target_url)


async def get_peers_with_diagnostic(source_workspace_id: str | None = None) -> tuple[list[dict], str | None]:
    """Get this workspace's peers, returning (peers, diagnostic).

    diagnostic is None when the call succeeded (status 200, even if the list
    is empty). When peers is [] for a non-trivial reason (auth failure,
    workspace-id missing from registry, platform error, network error),
    diagnostic is a short human-readable string explaining what went wrong
    so callers can surface it instead of "may be isolated" — see #2397.

    ``source_workspace_id`` selects which registered workspace's peers to
    enumerate; defaults to the module-level WORKSPACE_ID for
    single-workspace back-compat. Multi-workspace operators iterate over
    each registered workspace separately so each set of peers is fetched
    with the correct auth.

    The legacy get_peers() shim below preserves the bare-list contract for
    non-tool callers.
    """
    src = (source_workspace_id or "").strip() or _resolve_workspace_id()
    base = _resolve_platform_url(src)
    url = f"{base}/registry/{src}/peers"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                url,
                headers={"X-Workspace-ID": src, **auth_headers(src)},
            )
        except Exception as e:
            return [], f"Cannot reach platform at {base}: {e}"

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as e:
                return [], f"Platform returned 200 but body was not JSON: {e}"
            if not isinstance(data, list):
                return [], f"Platform returned 200 but body was not a list: {type(data).__name__}"
            return data, None

        if resp.status_code in (401, 403):
            return [], (
                f"Authentication to platform failed (HTTP {resp.status_code}). "
                "The workspace bearer token may be invalid — restarting the workspace usually re-mints it."
            )
        if resp.status_code == 404:
            return [], (
                f"Workspace ID {src} is not registered with the platform (HTTP 404). "
                "Re-registration via the platform's /registry/register endpoint is needed."
            )
        if 500 <= resp.status_code < 600:
            return [], f"Platform error: HTTP {resp.status_code}."
        return [], f"Unexpected platform response: HTTP {resp.status_code}."


async def get_peers() -> list[dict]:
    """Get this workspace's peers from the platform registry.

    Bare-list shim over get_peers_with_diagnostic() — discards the diagnostic
    so callers that don't care about the failure reason (e.g. system-prompt
    bootstrap formatters) get the same shape they always had.
    """
    peers, _ = await get_peers_with_diagnostic()
    return peers


async def get_workspace_info(source_workspace_id: str | None = None) -> dict:
    """Get this workspace's info from the platform.

    ``source_workspace_id`` selects which registered workspace to
    introspect when the agent is registered into multiple workspaces
    (multi-workspace mode). Unset → defaults to the module-level
    WORKSPACE_ID — single-workspace operators see no behaviour change.

    Distinguishes three failure shapes so callers can handle them
    distinctly (#2429):
      - 410 Gone        → workspace was deleted; re-onboard required
      - 404 / other     → workspace never existed (or transient)
      - exception       → network / auth failure
    """
    src = source_workspace_id or _resolve_workspace_id()
    base = _resolve_platform_url(src)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{base}/workspaces/{src}",
                headers=auth_headers(src),
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 410:
                # #2429: platform returns 410 when status='removed'.
                # Surface "removed" + the actionable hint so callers
                # can prompt re-onboard instead of falling through to
                # "not found" — which made the 2026-04-30 incident
                # impossible to diagnose ("workspace not found" with
                # a workspace_id we KNEW we'd just registered).
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                return {
                    "error": "removed",
                    "id": body.get("id", src),
                    "removed_at": body.get("removed_at"),
                    "hint": body.get(
                        "hint",
                        "Workspace was deleted on the platform. "
                        "Re-create the workspace, then create a token in Canvas "
                        "Settings → Workspace Tokens.",
                    ),
                }
            return {"error": "not found"}
        except Exception as e:
            return {"error": str(e)}
