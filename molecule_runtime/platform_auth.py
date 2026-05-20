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


def _token_file() -> Path:
    """Path to the on-disk token file. Resolved via configs_dir so
    in-container (/configs) and external-runtime (~/.molecule-workspace)
    operators land on a writable location automatically. Explicit
    CONFIGS_DIR env var still wins."""
    return configs_dir.resolve() / ".auth_token"


def get_token() -> str | None:
    """Return the cached token, reading it from disk on first call.

    Resolution order:
        1. In-process cache (hot path)
        2. ``${CONFIGS_DIR}/.auth_token`` file (in-container default —
           the platform writes this on provision and rotates it on
           restart)
        3. ``MOLECULE_WORKSPACE_TOKEN`` env var (external-runtime path —
           operators running the universal MCP server outside a
           container have no /configs volume to populate, so they pass
           the token via env)

    File-first preserves in-container behavior unchanged: containers
    always have /configs/.auth_token on disk, env-var fallback only
    fires when there's no file. This is additive — no existing caller
    sees a behavior change.
    """
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    path = _token_file()
    if path.exists():
        try:
            tok = path.read_text().strip()
        except OSError as exc:
            logger.warning("platform_auth: failed to read %s: %s", path, exc)
            tok = ""
        if tok:
            _cached_token = tok
            return tok
    # File missing or empty — fall back to env (external-runtime path).
    env_tok = os.environ.get("MOLECULE_WORKSPACE_TOKEN", "").strip()
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


def auth_headers(workspace_id: str | None = None) -> dict[str, str]:
    """Return a header dict to merge into httpx calls. Empty if no token
    is available yet — callers send the request as-is and the platform's
    heartbeat handler grandfathers pre-token workspaces through until
    their next /registry/register issues one.

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
    """
    headers: dict[str, str] = {}
    platform_url = os.environ.get("PLATFORM_URL", "").strip()
    if platform_url:
        headers["Origin"] = platform_url
    tok: str | None = None
    if workspace_id:
        tok = get_workspace_token(workspace_id)
    if tok is None:
        tok = get_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


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
    # Pass workspace_id through to auth_headers so the bearer token
    # comes from the per-workspace registry when set — otherwise a
    # multi-workspace operator's source-tagged POST authenticates with
    # the legacy single token (or none) and the platform rejects with
    # 401, or worse silently logs the wrong source.
    return {**auth_headers(workspace_id), "X-Workspace-ID": workspace_id}


def clear_cache() -> None:
    """Reset the in-memory cache. Used by tests that write fresh token
    files between cases."""
    global _cached_token
    _cached_token = None
    with _WORKSPACE_TOKENS_LOCK:
        _WORKSPACE_TOKENS.clear()


def refresh_cache() -> str | None:
    """Force re-read of the token from disk, discarding the in-process cache.

    Use this when a 401 response suggests the cached token is stale —
    e.g. after the platform rotates tokens during a restart (issue #1877).
    Returns the (new) token value or None if not found/error."""
    global _cached_token
    _cached_token = None
    return get_token()


# Back-compat alias for callers + tests that imported the original
# ``refresh_from_disk`` symbol introduced alongside the CWE-20 fix
# (standalone PR #1877). The standalone-SSOT restructure renamed it to
# ``refresh_cache`` — both names point at the same implementation so
# pre-0.2.0 callers keep working without behaviour change.
refresh_from_disk = refresh_cache
