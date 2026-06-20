"""SSOT re-export for the Molecule target-resolution contract (issue #38).

Adapters (a2a_cli, the standalone ``molecule-mcp`` console script,
plugin pipelines) MUST import the env-driven workspace resolution
contract from this module rather than re-parsing ``WORKSPACE_ID``,
``MOLECULE_WORKSPACE_TOKEN``, or ``MOLECULE_WORKSPACES`` themselves.
The lower-level
``mcp_workspace_resolver`` is the implementation; this module is the
**stable public surface** that drift-tests pin adapters against.

Public surface
--------------

* ``resolve_workspaces()`` → ``(workspaces, errors)`` — the canonical
  multi-workspace + legacy single-workspace resolution. ``workspaces``
  is a list of ``(workspace_id, token, platform_url)`` triples;
  ``errors`` is the operator-help list.
* ``read_token_file()`` → token text or ``""`` — the canonical helper
  for the ``${CONFIGS_DIR}/.auth_token`` fallback path.
* ``print_missing_env_help(missing, have_token_file)`` — the canonical
  operator-help printer; the standalone ``molecule-mcp`` entry point
  uses this so every consumer surfaces the same first-3-lines-of-help
  on a misconfigured box.
* ``resolve_target_for_adapter()`` → single-target dict — convenience
  wrapper that returns the first resolved target as a dict
  (``{"id": ..., "token": ..., "platform_url": ...}``) or ``None`` if
  no workspace is resolvable. Adapters that operate on a single
  workspace (e.g. legacy single-tenant operators) call this instead
  of reaching into the multi-workspace list.

Why the re-export layer
-----------------------

Without this module, an adapter that imports
``mcp_workspace_resolver`` directly bypasses the SSOT public
surface — and any future refactor of the resolver (e.g. consolidating
single-workspace + multi-workspace into a single resolution function)
would silently break the adapter. This module is the contract surface
that makes the refactor loud at adapter test time.

See also: ``mcp_schemas`` for the SSOT tool schema contract.
"""
from __future__ import annotations

from typing import Any

# Re-exports — the entire SSOT public surface adapters should use.
from molecule_runtime.mcp_workspace_resolver import (  # noqa: F401
    resolve_workspaces,
    read_token_file,
    print_missing_env_help,
)


def resolve_target_for_adapter() -> dict[str, Any] | None:
    """Convenience wrapper: return the first resolved target as a dict.

    Adapters that operate on a single workspace (legacy single-tenant
    operators, plugin pipelines) call this instead of indexing into
    the multi-workspace list themselves. Returns ``None`` if no
    workspace is resolvable (the caller decides whether to fail-closed
    or fall through to a default).
    """
    workspaces, errors = resolve_workspaces()
    if errors and not workspaces:
        # Caller will see errors via the (workspaces, errors) tuple
        # when they call resolve_workspaces directly; this convenience
        # path returns None to signal "no usable target".
        return None
    if not workspaces:
        return None
    wid, tok, platform = workspaces[0]
    return {"id": wid, "token": tok, "platform_url": platform}


__all__ = [
    "resolve_workspaces",
    "read_token_file",
    "print_missing_env_help",
    "resolve_target_for_adapter",
]
