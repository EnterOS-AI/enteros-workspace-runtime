"""Workspace auth-token store (Phase 30.1).

Single source of truth for this workspace's authentication token. The
token is issued by the platform on the first successful
``POST /registry/register`` call and travels with every subsequent
heartbeat / update-card / (later) secrets-pull / A2A request.

The token is persisted to ``<configs>/.auth_token`` so it survives
restarts — we only expect to receive it once from the platform, since
``/registry/register`` no-ops token issuance for workspaces that already
have one on file.

Storage:
    ${CONFIGS_DIR}/.auth_token        # 0600, one line, no trailing newline

Callers interact with three functions:
    :func:`get_token`   — returns the cached token or None
    :func:`save_token`  — persists a freshly-issued token
    :func:`auth_headers`— builds the Authorization header dict for httpx
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import molecule_runtime.configs_dir as configs_dir

logger = logging.getLogger(__name__)

# Valid workspace ID: lowercase alphanumeric + hyphens (UUIDs and
# org-generated IDs). Rejects /, \, .., #, ?, &, newlines — all
# characters that could break URL paths or HTTP header values when
# WORKSPACE_ID is interpolated into platform API URLs or the
# ``X-Workspace-ID`` header. This is the single validation gate for the
# legacy single-workspace WORKSPACE_ID env var AND for each entry in the
# multi-workspace ``MOLECULE_WORKSPACES`` registry (see
# :func:`register_workspace_token`).
#
# Fixes issue #14 (CWE-20, Improper Input Validation). Originally landed
# as standalone PR #29 in 0.1.x, dropped accidentally in the
# standalone-SSOT 0.2.0 restructure, restored here so 0.2.0 doesn't ship
# with a known security regression.
_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,127}$")

# In-process cache for the validated single-workspace ID. Validated once
# on first call to :func:`get_workspace_id` and reused thereafter — the
# regex check is cheap but pointless to repeat on every heartbeat.
_validated_workspace_id: str | None = None


def validate_workspace_id(workspace_id: str) -> str:
    """Validate *workspace_id* and return the canonical (stripped) form.

    Raises :class:`ValueError` if the ID is empty, contains unsafe
    characters, or does not match the expected lowercase-alphanumeric-
    plus-hyphen format. Callers that hand-construct a workspace ID for
    a platform request should pass it through here first; callers that
    only need the env-var workspace ID should use :func:`get_workspace_id`,
    which caches the result.

    Fixes issue #14 (CWE-20): prevents URL/header injection when the
    workspace ID is interpolated into platform API URLs or the
    ``X-Workspace-ID`` header. The regex deliberately matches the
    smallest set of characters platform-generated IDs are known to use,
    so unexpected encodings (uppercase, underscores, dots, slashes,
    fragment/query/space chars, control chars) fail closed.
    """
    if not workspace_id:
        raise ValueError(
            "WORKSPACE_ID is empty — set the WORKSPACE_ID env var "
            "(single-workspace) or MOLECULE_WORKSPACES "
            "(multi-workspace external runtime)"
        )

    # Strip whitespace before regex match. Whitespace-only IDs fail the
    # regex (which requires a leading [a-z0-9]) with the "invalid
    # characters" message — same behaviour as PR #29's original
    # validator.
    workspace_id = workspace_id.strip()

    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValueError(
            f"WORKSPACE_ID contains invalid characters: {workspace_id!r}. "
            "Only lowercase letters, digits, and hyphens are allowed "
            "(starting with an alphanumeric, max 128 chars). "
            "Ensure the ID is a valid UUID or platform-generated alphanumeric."
        )

    return workspace_id


def get_workspace_id() -> str:
    """Return the validated single-workspace WORKSPACE_ID.

    Reads ``os.environ['WORKSPACE_ID']`` on first call, validates it via
    :func:`validate_workspace_id`, caches the result, and returns it.
    Subsequent calls return the cached value without re-reading the
    environment — workspace IDs are immutable per container lifetime, so
    refreshing on every call would just burn CPU.

    Use this in any module that needs to interpolate the workspace ID
    into a platform URL or ``X-Workspace-ID`` header. Multi-workspace
    callers (mcp_cli external-runtime path) should look up per-workspace
    IDs from the ``MOLECULE_WORKSPACES`` JSON via
    :func:`get_workspace_token`, not from this function.

    Raises :class:`ValueError` if ``WORKSPACE_ID`` is unset or malformed.
    Callers that want to gracefully handle the missing-env case
    (multi-workspace mode, doctor tools, tests) should catch the
    ValueError; the alternative — returning empty/None — re-introduces
    the CWE-20 surface this validator exists to close.
    """
    global _validated_workspace_id
    if _validated_workspace_id is not None:
        return _validated_workspace_id

    raw = os.environ.get("WORKSPACE_ID", "")
    _validated_workspace_id = validate_workspace_id(raw)
    return _validated_workspace_id


def _reset_workspace_id_cache() -> None:
    """Clear the cached single-workspace ID. Test-only — production
    workspaces never change WORKSPACE_ID after boot."""
    global _validated_workspace_id
    _validated_workspace_id = None


# In-process cache so we don't hit disk on every heartbeat. The heartbeat
# loop fires on a short interval and reading a tiny file 10x per minute
# is wasteful. The file is the durable copy; this var is the hot path.
_cached_token: str | None = None

# Per-workspace token registry — populated by mcp_cli when the operator
# runs a multi-workspace external agent (MOLECULE_WORKSPACES env var).
# Keyed by workspace_id, value is the bearer token issued by that
# workspace's tenant. Distinct from `_cached_token` (which is the
# single-workspace path's token); the two coexist so single-workspace
# back-compat is preserved exactly.
#
# Lock guards mutations from the registration phase (one writer per
# workspace, but the writers run in main(), not in heartbeat threads).
# Reads are lock-free for the hot path; the dict is finalized before
# any heartbeat / poller thread starts.
_WORKSPACE_TOKENS: dict[str, str] = {}
_WORKSPACE_TOKENS_LOCK = threading.Lock()

# Per-workspace platform_url registry — populated by mcp_cli when the
# operator's MOLECULE_WORKSPACES JSON carries a per-entry ``platform_url``
# field (RFC#601, task #296 Phase B0). Mirrors ``_WORKSPACE_TOKENS`` so
# the wiring stays uniform: each registered workspace optionally owns
# BOTH a bearer token AND a platform URL.
#
# Distinct from the module-level ``PLATFORM_URL`` env var (which still
# wins for entries without a per-workspace override AND for the entire
# single-workspace path). Reads happen on every outbound HTTP construct
# in the A2A client + messaging tool layer, so the lookup must stay
# lock-free in the hot path — the dict is populated synchronously from
# main() before any thread starts, then read-only thereafter.
_WORKSPACE_PLATFORM_URLS: dict[str, str] = {}
_WORKSPACE_PLATFORM_URLS_LOCK = threading.Lock()


def _token_file() -> Path:
    """Path to the on-disk token file. Resolved via configs_dir so
    in-container (/configs) and external-runtime (~/.molecule-workspace)
    operators land on a writable location automatically. Explicit
    CONFIGS_DIR env var still wins."""
    return configs_dir.resolve() / ".auth_token"


def get_token() -> str | None:
    """Return the cached token, reading it from disk / env on first call.

    Resolution order depends on WHERE the token file lives:

    In-container (the token file is ``/configs/.auth_token``):
        1. In-process cache
        2. ``/configs/.auth_token`` file — PROVISIONER-OWNED and ROTATED on
           restart, so it is the fresh SSOT; an env var captured at container
           start may be stale → file wins.
        3. ``MOLECULE_WORKSPACE_TOKEN`` env (only if the file is missing/empty)

    External-runtime (the token file is ``~/.molecule-workspace/.auth_token``
    or under an explicit non-``/configs`` ``CONFIGS_DIR``):
        1. In-process cache
        2. ``MOLECULE_WORKSPACE_TOKEN`` env — the operator passes the CURRENT
           token here; any on-disk ``.auth_token`` may be a LEFTOVER from a
           prior session and must NOT shadow it. Before this fix a stale file
           silently 401'd the inbox poller while the heartbeat used env (a
           split-brain), and it disagreed with ``mcp_target_resolution`` (which
           is already env-first). Off ``/configs``, env wins.
        3. ``.auth_token`` file (only if env is unset)

    In-container behavior is unchanged (file-first on ``/configs``); only the
    external path changes — env now beats a stale file.
    """
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    env_tok = os.environ.get("MOLECULE_WORKSPACE_TOKEN", "").strip()
    path = _token_file()
    # /configs is the platform-managed, restart-rotated volume → file is the
    # fresh SSOT there. Everywhere else, an explicitly-provided env token wins
    # over a possibly-stale on-disk file. The boundary is EXACT parent
    # equality with /configs (the token file is always /configs/.auth_token
    # in-container, and containers never set CONFIGS_DIR) — deliberately not
    # a prefix test, so /configsfoo can't masquerade as the platform volume.
    on_platform_configs = path.parent == Path("/configs")
    if env_tok and not on_platform_configs:
        _cached_token = env_tok
        return env_tok
    if path.exists():
        try:
            tok = path.read_text().strip()
        except OSError as exc:
            logger.warning("platform_auth: failed to read %s: %s", path, exc)
            tok = ""
        if tok:
            _cached_token = tok
            return tok
    # File missing/empty (or in-container with no file) — fall back to env.
    if env_tok:
        _cached_token = env_tok
        return env_tok
    return None


def save_token(token: str) -> None:
    """Persist a newly-issued token. Creates the file with 0600 mode atomically.

    Uses ``os.open(O_CREAT, 0o600)`` so the file is never world-readable,
    even transiently. The previous ``write_text()`` + ``chmod()`` approach
    had a TOCTOU window where a concurrent reader could access the token
    between the two syscalls (M4 — flagged in security audit cycle 10).

    Idempotent — if an identical token is already on disk we skip the
    write so we don't churn the file's mtime or trigger spurious
    filesystem watchers."""
    global _cached_token
    token = token.strip()
    if not token:
        raise ValueError("platform_auth: refusing to save empty token")
    if get_token() == token:
        return
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT | O_WRONLY | O_TRUNC with mode=0o600 atomically creates (or
    # truncates) the file with restricted permissions in a single syscall,
    # eliminating the TOCTOU window.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    _cached_token = token


def register_workspace_token(workspace_id: str, token: str) -> None:
    """Register a per-workspace bearer token in the multi-workspace registry.

    Called by ``mcp_cli`` once per entry in the ``MOLECULE_WORKSPACES``
    env var so per-workspace heartbeat / poller threads can resolve their
    own auth via ``auth_headers(workspace_id=...)`` without each thread
    closing over a token literal.

    Idempotent: re-registering the same workspace_id with the same token
    is a no-op; with a different token it overwrites and logs at INFO
    (the legitimate case is operator token rotation between restarts).
    """
    workspace_id = (workspace_id or "").strip()
    token = (token or "").strip()
    if not workspace_id or not token:
        return
    # Validate the workspace_id format before it lands in the registry —
    # otherwise a crafted MOLECULE_WORKSPACES JSON entry could store an
    # injection-bearing ID that get_workspace_token(...) returns
    # unchanged, re-introducing the CWE-20 surface. Single validation
    # gate here means every legitimate caller (mcp_cli) gets uniform
    # protection without each call-site duplicating the regex.
    try:
        workspace_id = validate_workspace_id(workspace_id)
    except ValueError as exc:
        logger.warning(
            "platform_auth: refusing to register invalid workspace_id: %s", exc,
        )
        return
    with _WORKSPACE_TOKENS_LOCK:
        prior = _WORKSPACE_TOKENS.get(workspace_id)
        if prior == token:
            return
        if prior is not None:
            logger.info(
                "platform_auth: workspace_id %s token rotated", workspace_id,
            )
        _WORKSPACE_TOKENS[workspace_id] = token


def get_workspace_token(workspace_id: str) -> str | None:
    """Return the per-workspace token from the registry, or None.

    Lookup is lock-free: writes happen in main() before threads start,
    reads are stable thereafter.
    """
    return _WORKSPACE_TOKENS.get((workspace_id or "").strip())


def register_workspace_platform_url(workspace_id: str, platform_url: str) -> None:
    """Register a per-workspace platform URL in the multi-tenant registry.

    Called by ``mcp_cli`` once per MOLECULE_WORKSPACES entry whose JSON
    carried a ``platform_url`` field (RFC#601, task #296 Phase B0). Lets
    a single external-agent process register into workspaces that live
    on different platform tenants — each workspace's outbound HTTP
    target / Origin header is derived from its own URL rather than the
    process-wide ``PLATFORM_URL`` env var.

    Pre-validates ``workspace_id`` through ``validate_workspace_id`` for
    symmetry with the token-registry path — an invalid id stored here
    could later flow into a URL/header injection through
    ``get_workspace_platform_url``. Empty / blank ``platform_url`` is
    treated as "operator declined the per-entry override" — we silently
    drop the registration so the caller falls through to the
    module-level env var (back-compat default).

    Idempotent: re-registering the same workspace_id with the same URL
    is a no-op; with a different URL it overwrites and logs at INFO so
    operator-side tenant moves leave a trail in the runtime log.
    """
    workspace_id = (workspace_id or "").strip()
    platform_url = (platform_url or "").strip().rstrip("/")
    if not workspace_id or not platform_url:
        return
    try:
        workspace_id = validate_workspace_id(workspace_id)
    except ValueError as exc:
        logger.warning(
            "platform_auth: refusing to register platform_url for invalid "
            "workspace_id: %s", exc,
        )
        return
    with _WORKSPACE_PLATFORM_URLS_LOCK:
        prior = _WORKSPACE_PLATFORM_URLS.get(workspace_id)
        if prior == platform_url:
            return
        if prior is not None:
            logger.info(
                "platform_auth: workspace_id %s platform_url changed "
                "(%s → %s)",
                workspace_id, prior, platform_url,
            )
        _WORKSPACE_PLATFORM_URLS[workspace_id] = platform_url


def get_workspace_platform_url(workspace_id: str | None) -> str | None:
    """Return the per-workspace platform URL from the registry, or None.

    None means "no per-workspace override registered" — callers should
    fall through to the module-level ``PLATFORM_URL`` env var (the
    pre-RFC#601 behavior, unchanged for single-tenant operators).

    Lookup is lock-free: writes happen in main() before any thread
    starts, reads are stable thereafter — same hot-path contract as
    ``get_workspace_token``.
    """
    if not workspace_id:
        return None
    return _WORKSPACE_PLATFORM_URLS.get(workspace_id.strip())


def list_registered_workspaces() -> list[str]:
    """Return the workspace IDs currently in the per-workspace registry.

    Empty list when no multi-workspace registration has happened (i.e.
    single-workspace operators using the legacy WORKSPACE_ID env path —
    those callers should fall back to the module-level WORKSPACE_ID).

    Used by ``a2a_tools.tool_list_peers`` to aggregate peers across all
    workspaces an external agent has registered against, so a
    multi-workspace operator can see the full peer surface in one call
    instead of having to query each workspace separately.
    """
    with _WORKSPACE_TOKENS_LOCK:
        return list(_WORKSPACE_TOKENS.keys())


# --------------------------------------------------------------------------
# Tenant routing (issue #373)
# --------------------------------------------------------------------------
# The SaaS platform's TenantGuard middleware
# (molecule-core/workspace-server/internal/middleware/tenant_guard.go) is
# registered on the ROOT gin engine, so it runs BEFORE authentication. Any
# request that carries neither ``X-Molecule-Org-Id`` nor ``Fly-Replay-Src``
# is rejected with:
#
#     400 {"code":"TENANT_ORG_HEADER_REQUIRED",
#          "error":"missing tenant routing header",
#          "required_header":"X-Molecule-Org-Id"}
#
# A valid bearer token cannot save the call — measured on a live tenant, a
# bogus token and a real token return byte-identical 400s. ``/workspaces/:id/a2a``
# is in no exemption list, so workspace→workspace A2A over PLATFORM_URL is
# 100% dead on any tenant whose platform process has MOLECULE_ORG_ID set.
#
# The guard is a total passthrough when the platform's own MOLECULE_ORG_ID is
# empty (self-host), which is why this was latent for so long rather than
# constantly on fire.
_ORG_ID_HEADER = "X-Molecule-Org-Id"

# Source env keys, in precedence order. Mirrors
# ``privileged_mcp_env._SELF_ORG_ID_SOURCE_KEYS`` — the strict key plus the
# one legacy alias the mcp-server surface also accepts. The contract
# (contracts/credentials.contract.json, id "org-id") makes MOLECULE_ORG_ID
# canonical.
_ORG_ID_ENV_KEYS: tuple[str, ...] = ("MOLECULE_ORG_ID", "MOLECULE_ORGANIZATION_ID")

# An org id is interpolated straight into an HTTP header value, so it gets
# the same fail-closed treatment WORKSPACE_ID gets before it reaches
# ``X-Workspace-ID`` (CWE-20 / header injection): printable ASCII only, no
# spaces, no control characters, no CR/LF.
_ORG_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")


def get_org_id() -> str | None:
    """Return this workspace's tenant org id, or ``None`` when unset.

    Resolution is env-only and deliberately dumb: whichever of
    ``MOLECULE_ORG_ID`` / ``MOLECULE_ORGANIZATION_ID`` is non-empty first.
    There is no default and no derivation — see :func:`tenant_headers` for
    why forging a value is worse than omitting it.

    Returns ``None`` (with one warning log) if the value present in the
    environment cannot be safely placed in a header value.
    """
    for key in _ORG_ID_ENV_KEYS:
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        if not _ORG_ID_RE.match(raw):
            logger.warning(
                "%s is set but is not a valid HTTP header value — omitting the "
                "%s tenant-routing header. Platform calls will be rejected with "
                "400 TENANT_ORG_HEADER_REQUIRED on a SaaS tenant.",
                key,
                _ORG_ID_HEADER,
            )
            return None
        return raw
    return None


def tenant_headers() -> dict[str, str]:
    """Return the platform's tenant-routing headers — ``{}`` when unset.

    ``{"X-Molecule-Org-Id": <org id>}`` when ``MOLECULE_ORG_ID`` resolves,
    otherwise an empty dict.

    OMIT rather than forge (issue #373): the org id is an authorization-
    adjacent routing claim the runtime has no standing to invent. A
    fabricated value would route the request at some *other* tenant or be
    rejected further in with a much worse error; the platform's own
    ``400 TENANT_ORG_HEADER_REQUIRED`` is the better, more diagnosable
    failure. On self-host (no org id anywhere) the platform's guard is a
    passthrough, so an absent header is also the correct wire shape.
    """
    org_id = get_org_id()
    return {_ORG_ID_HEADER: org_id} if org_id else {}


def platform_headers(
    workspace_id: str | None = None,
    *,
    source: bool = False,
) -> dict[str, str]:
    """THE header builder for every request this runtime sends to the
    platform. If you are about to hand-roll a dict for a ``PLATFORM_URL``
    call, use this instead.

    Returns, in one dict:

    * ``X-Molecule-Org-Id`` — tenant routing (:func:`tenant_headers`),
      omitted when unresolvable.
    * ``Origin``           — the tenant edge WAF requires same-origin.
    * ``Authorization``    — ``Bearer <workspace token>`` when one exists.
    * ``X-Workspace-ID``   — only when ``source=True``; identifies this
      workspace as the SOURCE of the request (see
      :func:`self_source_headers`).

    Merge extra headers at the call site — ``{**platform_headers(ws),
    "Content-Type": "application/json"}`` — rather than growing kwargs
    here.

    Why a single builder, enforced structurally: this repo has now been
    bitten twice by two call sites that had to agree about a header and
    didn't. ``tests/test_platform_headers_ssot.py`` walks the AST of the
    whole package and fails if ANY platform-bound request builds its
    headers by another route, and separately asserts that every builder on
    its allowlist emits the tenant header — so an author cannot silence the
    structural gate by adding a new hand-rolled builder to the allowlist.
    """
    headers: dict[str, str] = {}
    ws_url: str | None = None
    if workspace_id:
        ws_url = get_workspace_platform_url(workspace_id)
    if ws_url:
        headers["Origin"] = ws_url
        # A per-workspace platform_url override means the multi-workspace
        # external bridge (MOLECULE_WORKSPACES): this one local process talks
        # to SEVERAL tenants, and the tenant is selected by platform_url —
        # see README "Multiple External Workspaces", which states outright
        # that org_id is not part of that config. This process's env
        # MOLECULE_ORG_ID describes at most ONE of those tenants, so stamping
        # it on a request bound for another one would be exactly the forging
        # this fix refuses to do. Omit; the entry's own token and URL carry
        # the routing.
    else:
        platform_url = os.environ.get("PLATFORM_URL", "").strip()
        if platform_url:
            headers["Origin"] = platform_url
        headers.update(tenant_headers())
    tok: str | None = None
    if workspace_id:
        tok = get_workspace_token(workspace_id)
    if tok is None:
        tok = get_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if source:
        if not workspace_id:
            raise ValueError("platform_headers(source=True) requires workspace_id")
        headers["X-Workspace-ID"] = workspace_id
    return headers


def auth_headers(workspace_id: str | None = None) -> dict[str, str]:
    """Return a header dict to merge into httpx calls. Empty if no token
    is available yet — callers send the request as-is and the platform's
    heartbeat handler grandfathers pre-token workspaces through until
    their next /registry/register issues one.

    Thin alias for :func:`platform_headers` — the name most of the runtime
    already imports. New code should prefer ``platform_headers``.

    Always sets ``Origin`` to ``PLATFORM_URL`` when that env var is set.
    On hosted SaaS deployments the tenant's edge WAF requires a same-
    origin header — without it ``/workspaces/*`` and ``/registry/*/peers``
    requests get silently rewritten to the canvas Next.js app, which has
    no such routes and returns an empty 404. Inside-container calls are
    unaffected (Docker-internal PLATFORM_URLs aren't behind the WAF).
    Discovered while smoke-testing the molecule-mcp external-runtime
    path against a live tenant — every tool call returned "not found"
    because the WAF was eating them.

    Token resolution order:
        1. ``workspace_id`` arg → per-workspace registry
           (multi-workspace external agent — set by mcp_cli)
        2. Single-workspace cache + .auth_token file + env var
           (pre-existing path; back-compat unchanged)

    Single-workspace operators see no behavior change: ``auth_headers()``
    with no arg routes through the legacy resolution path exactly as
    before. Multi-workspace operators pass ``workspace_id`` so each
    thread (heartbeat, poller, send_message_to_user) authenticates
    against the correct workspace.

    Origin-header resolution order (RFC#601, task #296 Phase B0):
        1. Per-workspace platform_url registry — set by mcp_cli when
           the MOLECULE_WORKSPACES JSON carried a per-entry
           ``platform_url`` field. Used only when ``workspace_id`` was
           passed AND that workspace has a URL registered.
        2. Module-level ``PLATFORM_URL`` env var — the pre-RFC#601
           default, still wins for entries without a per-workspace
           override AND for the entire single-workspace path.

    Tenant routing (#373): also carries ``X-Molecule-Org-Id`` when
    ``MOLECULE_ORG_ID`` resolves. Every caller of this function was firing
    at a platform endpoint sitting behind TenantGuard, so the header
    belongs here rather than at ~50 individual call sites.
    """
    return platform_headers(workspace_id)


def self_source_headers(workspace_id: str) -> dict[str, str]:
    """Return auth headers PLUS X-Workspace-ID identifying this workspace
    as the source of the request.

    Use this for any POST the workspace's own runtime fires against the
    platform's A2A endpoints — heartbeat self-messages, initial_prompt,
    idle-loop fires, peer-to-peer A2A from runtime tools. Without the
    X-Workspace-ID header the platform's a2a_receive logger writes
    source_id=NULL, which the canvas's My Chat tab interprets as a
    user-typed message and renders the internal prompt to the user.
    See workspace-server/internal/handlers/a2a_proxy.go:184 for the
    server-side classification rule.

    Centralised here so adding a new system header (e.g. a per-fire
    correlation ID) only touches one place — and so that any
    workspace→A2A POST that doesn't use this helper stands out in
    review as a probable bug."""
    # Pass workspace_id through to platform_headers so the bearer token
    # comes from the per-workspace registry when set — otherwise a
    # multi-workspace operator's source-tagged POST authenticates with
    # the legacy single token (or none) and the platform rejects with
    # 401, or worse silently logs the wrong source.
    return platform_headers(workspace_id, source=True)


def clear_cache() -> None:
    """Reset the in-memory cache. Used by tests that write fresh token
    files between cases."""
    global _cached_token
    _cached_token = None
    with _WORKSPACE_TOKENS_LOCK:
        _WORKSPACE_TOKENS.clear()
    with _WORKSPACE_PLATFORM_URLS_LOCK:
        _WORKSPACE_PLATFORM_URLS.clear()


def refresh_cache() -> str | None:
    """Re-resolve the token, discarding the in-process cache.

    Use this when a 401 response suggests the cached token is stale —
    e.g. after the platform rotates tokens during a restart (issue #1877).
    Resolution follows get_token(): in-container (/configs) this re-reads
    the rotated file; on external-runtime hosts an explicitly-set
    MOLECULE_WORKSPACE_TOKEN wins instead, so the env var is the operator's
    responsibility to keep current there (file-based 401 recovery does not
    apply off /configs). Returns the token value or None if not found."""
    global _cached_token
    _cached_token = None
    return get_token()


# Back-compat alias for callers + tests that imported the original
# ``refresh_from_disk`` symbol introduced alongside the CWE-20 fix
# (standalone PR #1877). The standalone-SSOT restructure renamed it to
# ``refresh_cache`` — both names point at the same implementation so
# pre-0.2.0 callers keep working without behaviour change.
refresh_from_disk = refresh_cache
