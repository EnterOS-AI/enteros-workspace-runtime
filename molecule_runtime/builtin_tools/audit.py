"""Immutable append-only audit log for EU AI Act compliance.

Fulfils Article 12 (record-keeping), Article 13 (transparency), and
Article 17 (quality-management system) requirements for high-risk AI systems.

Log format: JSON Lines (one UTF-8 JSON object per line), suitable for direct
ingestion by any SIEM (Splunk, Elastic, Datadog, etc.).

Required event fields
---------------------
timestamp       ISO 8601 UTC datetime with timezone offset
event_type      Coarse category: "delegation", "approval", "memory", "rbac"
workspace_id    Workspace that generated this event
actor           Entity that triggered the action; defaults to workspace_id for
                automated events, or the human identity for approval decisions
action          Verb describing what was attempted:
                  delegate | approve | memory.read | memory.write | rbac.deny
resource        Object of the action: target workspace ID, memory scope,
                approval action string, etc.
outcome         One of: allowed | denied | success | failure | timeout |
                requested | granted
trace_id        UUID v4 correlating related events across workspaces

The log file is opened in append mode ("a") on every write — it is NEVER
truncated, rewritten, or deleted by this module.  Rotate externally using
logrotate (with ``copytruncate`` disabled) or ship to a SIEM before rotating.

Configuration
-------------
AUDIT_LOG_PATH  env var — full path to the JSONL file
                default: /var/log/molecule/audit.jsonl
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from molecule_runtime.rbac_policy import ROLE_PERMISSIONS  # noqa: F401
from molecule_runtime.rbac_policy import check_permission  # noqa: F401
from molecule_runtime.platform_auth import get_workspace_id as _get_workspace_id

if TYPE_CHECKING:
    pass  # avoid circular import at runtime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUDIT_LOG_PATH: str = os.environ.get(
    "AUDIT_LOG_PATH", "/var/log/molecule/audit.jsonl"
)
# CWE-20 (issue #14): WORKSPACE_ID lands in audit-log records — even
# though audit.jsonl is local-only (no URL/header surface), validating
# at module load keeps an injection-bearing env from being persisted
# into long-term audit storage.
try:
    WORKSPACE_ID: str = _get_workspace_id()
except ValueError:
    WORKSPACE_ID = ""

# Protects the open() + write() sequence; prevents interleaved JSON lines
# when multiple async tasks run in the same event-loop thread.
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config loader (lazy, cached per process)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_workspace_config():
    """Return the WorkspaceConfig or None if it cannot be loaded."""
    try:
        from molecule_runtime.config import load_config  # local import avoids circular deps
        return load_config()
    except Exception as exc:
        logger.error(
            "audit: could not load workspace config for RBAC; "
            "fail-securing to read-only. Set WORKSPACE_CONFIG_PATH to a "
            "directory containing config.yaml, or unset it to use the "
            "built-in configs_dir fallback. Error: %s",
            exc,
        )
        return None


def get_workspace_roles() -> tuple[list[str], dict[str, list[str]]]:
    """Return ``(roles, custom_permissions)`` from the workspace config.

    Falls back to ``["read-only"]`` / ``{}`` when the config is unavailable so
    that agents retain only memory-read access in degraded environments,
    denying by default rather than granting elevated permissions (fail-secure).

    Fix originally landed in standalone c72fbfc (closes #11, CWE-285);
    re-applied during standalone-as-SSOT migration. Pinned by
    tests/test_audit.py::TestGetWorkspaceRolesFailSecure.
    """
    cfg = _load_workspace_config()
    if cfg is None:
        return ["read-only"], {}
    return list(cfg.rbac.roles), dict(cfg.rbac.allowed_actions)


# ---------------------------------------------------------------------------
# Public audit API
# ---------------------------------------------------------------------------

def log_event(
    event_type: str,
    action: str,
    resource: str,
    outcome: str,
    actor: str | None = None,
    trace_id: str | None = None,
    **extra: Any,
) -> str:
    """Append one audit event to the immutable JSON Lines log.

    Args:
        event_type: Coarse category — ``"delegation"``, ``"approval"``,
                    ``"memory"``, or ``"rbac"``.
        action:     Verb — ``"delegate"``, ``"approve"``, ``"memory.write"``,
                    ``"memory.read"``, ``"rbac.deny"``.
        resource:   Object of the action — target workspace ID, memory scope,
                    approval action string, etc.
        outcome:    Terminal state — one of ``"allowed"``, ``"denied"``,
                    ``"success"``, ``"failure"``, ``"timeout"``,
                    ``"requested"``, ``"granted"``.
        actor:      Identity that triggered the event.  Defaults to
                    ``WORKSPACE_ID`` (the running workspace) for automated
                    events.  Pass ``decided_by`` for human approval decisions.
        trace_id:   Caller-supplied UUID v4 for cross-event correlation.
                    A fresh UUID is generated when omitted.
        **extra:    Additional key-value pairs appended verbatim to the JSON
                    object (e.g. ``target_workspace_id``, ``memory_scope``,
                    ``attempt``).  Built-in keys cannot be overridden.

    Returns:
        The ``trace_id`` used for this event, enabling callers to chain
        related events under a single correlation identifier.

    Example::

        trace = log_event(
            event_type="delegation",
            action="delegate",
            resource="billing-agent",
            outcome="success",
            target_workspace_id="billing-agent",
            attempt=1,
        )
    """
    if trace_id is None:
        trace_id = str(uuid.uuid4())

    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "workspace_id": WORKSPACE_ID,
        "actor": actor if actor is not None else WORKSPACE_ID,
        "action": action,
        "resource": resource,
        "outcome": outcome,
        "trace_id": trace_id,
    }

    # Merge extra fields — built-in keys are not overridable
    for key, value in extra.items():
        if key not in event:
            event[key] = value

    _write_event(event)
    return trace_id


# ---------------------------------------------------------------------------
# Internal writer
# ---------------------------------------------------------------------------

def _ensure_log_dir(path: str) -> None:
    """Create the parent directory for *path* if it does not already exist."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _write_event(event: dict[str, Any]) -> None:
    """Serialise *event* as a JSON line and fsync-append it to the log file.

    The write is atomic with respect to other threads in this process: the
    lock ensures that no two JSON objects are interleaved on the same line.

    Failures are emitted to the standard Python logger at WARNING level but
    are **never** re-raised — the application must not crash because audit
    logging is temporarily unavailable (e.g. disk full, permission error).
    In production, consider wiring an alert on WARNING messages from this
    module so that missing audit records are detected quickly.
    """
    try:
        log_path = AUDIT_LOG_PATH
        _ensure_log_dir(log_path)
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        with _write_lock:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Audit log write failed — event NOT persisted "
            "(trace_id=%s, action=%s): %s",
            event.get("trace_id", "?"),
            event.get("action", "?"),
            exc,
        )
