"""Shared input validation helpers for builtin tools.

Defence-in-depth: validate environment-derived values before they are used
in URLs, headers, or other security-sensitive positions (CWE-20 / CWE-88).
"""

from __future__ import annotations

import os
import re
from typing import assert_never

# Pattern: alphanumeric + hyphen + underscore + dot; no path-traversal chars.
# This deliberately rejects `/`, `\`, `..`, `#`, `?`, `&` which could
# manipulate URL path segments or query strings.
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,256}$")

# Error message prefix used by callers so callers can surface context.
_WORKSPACE_ID_INVALID_MSG = (
    "WORKSPACE_ID has an invalid format. "
    "Expected an alphanumeric identifier (hyphens, underscores, dots allowed); "
    "got: {value!r}  "
    "(path-traversal characters such as / \\ .. or fragment chars such as # ? & are not permitted)"
)


class WorkspaceIdValidationError(ValueError):
    """Raised when WORKSPACE_ID fails format validation.

    This is intentionally a ValueError subclass so callers that currently
    swallow generic Exceptions still get a clear signal.
    """

    pass


def _validate_workspace_id(workspace_id: str, *, caller: str = "unknown") -> None:
    """Validate WORKSPACE_ID and raise WorkspaceIdValidationError if unsafe.

    Args:
        workspace_id: The raw WORKSPACE_ID value to check.
        caller: Human-readable name of the calling module/function (for the error).

    Raises:
        WorkspaceIdValidationError: If workspace_id is empty or contains unsafe chars.
    """
    if not workspace_id:
        raise WorkspaceIdValidationError(
            f"[{caller}] WORKSPACE_ID is empty — cannot construct platform URLs. "
            "Set the WORKSPACE_ID environment variable."
        )
    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise WorkspaceIdValidationError(
            f"[{caller}] " + _WORKSPACE_ID_INVALID_MSG.format(value=workspace_id)
        )


# ---------------------------------------------------------------------------
# Lazy validation — call once at module initialisation, then cache the result.
# ---------------------------------------------------------------------------

_cached_workspace_id: str | None = None
_cached_validated: bool = False


def get_validated_workspace_id(*, caller: str = "builtin_tools") -> str:
    """Return the validated WORKSPACE_ID, raising on the first bad call.

    Result is cached so repeated calls are cheap.
    """
    global _cached_workspace_id, _cached_validated
    if _cached_validated:
        assert _cached_workspace_id is not None
        return _cached_workspace_id

    ws_id = os.environ.get("WORKSPACE_ID", "")
    _validate_workspace_id(ws_id, caller=caller)
    _cached_workspace_id = ws_id
    _cached_validated = True
    return ws_id
