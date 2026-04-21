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
from pathlib import Path

logger = logging.getLogger(__name__)

# Valid workspace ID: lowercase alphanumeric + hyphens (UUIDs and org-generated IDs).
# Rejects /, \, .., #, ?, &, newlines — all chars that could break URL paths
# or HTTP header values. This is the single validation gate for WORKSPACE_ID.
_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,127}$")

# Cached result — validated once per process startup, not on every call.
_validated_workspace_id: str | None = None


def validate_workspace_id(workspace_id: str) -> str:
    """Validate *workspace_id* and return it.

    Raises ValueError if the ID is empty, contains unsafe characters, or
    does not match the expected format. This function is the single validation
    gate — call it once at startup and reuse the result.

    Fixes issue #14 (CWE-20): prevents URL/header injection when WORKSPACE_ID
    is used in platform API URLs and ``X-Workspace-ID`` headers.
    """
    global _validated_workspace_id
    if _validated_workspace_id is not None:
        return _validated_workspace_id  # pragma: no cover — cached fast path

    if not workspace_id:
        raise ValueError("WORKSPACE_ID is empty — set the WORKSPACE_ID env var")

    # Strip and check again after strip
    workspace_id = workspace_id.strip()

    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValueError(
            f"WORKSPACE_ID contains invalid characters: {workspace_id!r}. "
            "Only lowercase letters, digits, and hyphens are allowed. "
            "Ensure WORKSPACE_ID is a valid UUID or alphanumeric ID."
        )

    _validated_workspace_id = workspace_id
    return workspace_id

# In-process cache so we don't hit disk on every heartbeat. The heartbeat
# loop fires on a short interval and reading a tiny file 10x per minute
# is wasteful. The file is the durable copy; this var is the hot path.
_cached_token: str | None = None

# Validated WORKSPACE_ID — read once at import time so every caller gets the
# same validated value without re-checking. Raises on bad input.
WORKSPACE_ID: str = validate_workspace_id(os.environ.get("WORKSPACE_ID", ""))


def get_workspace_id() -> str:
    """Return the validated workspace ID.

    Cached result from module-level WORKSPACE_ID constant. Call this instead
    of reading WORKSPACE_ID directly — it guarantees the ID passed validation.
    """
    return WORKSPACE_ID


def _token_file() -> Path:
    """Path to the on-disk token file. Respects CONFIGS_DIR, falls back
    to /configs for the default container layout."""
    return Path(os.environ.get("CONFIGS_DIR", "/configs")) / ".auth_token"


def get_token() -> str | None:
    """Return the cached token, reading it from disk on first call."""
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    path = _token_file()
    if not path.exists():
        return None
    try:
        tok = path.read_text().strip()
    except OSError as exc:
        logger.warning("platform_auth: failed to read %s: %s", path, exc)
        return None
    if not tok:
        return None
    _cached_token = tok
    return tok


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


def auth_headers() -> dict[str, str]:
    """Return a header dict to merge into every outbound platform call.

    Two headers, both optional:

    - ``Authorization: Bearer <token>`` — the workspace-scoped auth
      token issued on first /registry/register. Empty if not yet
      issued; the platform grandfathers pre-token workspaces through.

    - ``X-Molecule-Org-Id: <uuid>`` — the SaaS cross-org routing tag
      the tenant platform's TenantGuard requires on every non-
      allowlisted route. Read from the ``MOLECULE_ORG_ID`` env var
      that the control plane exports into workspace user-data.
      Unset on self-hosted / dev deployments where TenantGuard is a
      no-op, so omitting the header keeps those paths working.
    """
    headers: dict[str, str] = {}
    tok = get_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    org_id = os.environ.get("MOLECULE_ORG_ID", "").strip()
    if org_id:
        headers["X-Molecule-Org-Id"] = org_id
    return headers


def clear_cache() -> None:
    """Reset the in-memory cache. Used by tests that write fresh token
    files between cases."""
    global _cached_token
    _cached_token = None
