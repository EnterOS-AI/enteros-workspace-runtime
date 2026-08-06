"""Process-local record of WHERE this workspace's role identity came from.

The gap this closes (established live on the prod box, 2026-07-30): when a
workspace's persona file does not arrive, the runtime already says so twice — a
preflight ``[WARN] Prompt file: Missing prompt file: …`` line and (previously) a
bare ``print`` in ``prompt.build_system_prompt`` — but BOTH are container stdout
only. Nothing reaches the control plane. An operator reading the CP cannot tell

    "this workspace FAILED to get its identity"        (degraded, act on it)

apart from

    "this workspace never needed a role prompt file"   (fine, ignore it)

which is exactly the distinction that matters. This module is the one place that
answers it, and :func:`heartbeat_payload` routes the answer to the CP on the
heartbeat every workspace already sends — the same runtime-only-diagnostic
pattern ``platform_mcp_diag`` uses (``platform_agent_identity.identity_gate_payload``).

Design rules:

* **Nothing here may raise.** It is written from the boot hot path and read from
  the heartbeat thread; a diagnostic that can break either is worse than no
  diagnostic. Every public function is total.
* **Healthy = silent.** :func:`heartbeat_payload` returns ``{}`` unless the
  identity is degraded, so the normal heartbeat wire shape is unchanged and this
  can never make a healthy workspace look interesting.
* **Last write wins.** ``build_system_prompt`` may run more than once in a
  process (adapter re-setup); the newest build is the truth.
"""

from __future__ import annotations

import threading

# Where the role identity in the assembled system prompt came from.
SOURCE_ROLE_PROMPT_FILES = "role_prompt_files"  # the workspace's own persona loaded
SOURCE_BRANDED_DEFAULT = "branded_default"  # persona did NOT arrive → platform default

_LOCK = threading.Lock()
_STATE: dict = {
    "role_identity_source": None,
    "missing_prompt_files": [],
}


def record_role_identity(source: str, missing_prompt_files: list[str] | None = None) -> None:
    """Record the outcome of one system-prompt build. Never raises."""
    try:
        missing = [str(p) for p in (missing_prompt_files or [])]
        with _LOCK:
            _STATE["role_identity_source"] = source
            _STATE["missing_prompt_files"] = missing
    except Exception:  # noqa: BLE001 — a diagnostic must never break the caller
        pass


def snapshot() -> dict:
    """Current record as a plain dict. ``role_identity_source`` is None before
    the first ``build_system_prompt`` of the process."""
    try:
        with _LOCK:
            return {
                "role_identity_source": _STATE["role_identity_source"],
                "missing_prompt_files": list(_STATE["missing_prompt_files"]),
            }
    except Exception:  # noqa: BLE001
        return {"role_identity_source": None, "missing_prompt_files": []}


def is_degraded() -> bool:
    """True iff the last build had to fall back to the branded platform default."""
    return snapshot()["role_identity_source"] == SOURCE_BRANDED_DEFAULT


def heartbeat_payload() -> dict:
    """``{"role_identity_diag": {...}}`` when degraded, otherwise ``{}``.

    Merged into the heartbeat body so the CP can see — for every workspace, not
    just the concierge, and on every beat rather than only in a boot log nobody
    keeps — that this workspace is serving the platform default because its
    persona never arrived. Absent field = healthy, so the wire is unchanged for
    the overwhelmingly common case.
    """
    try:
        state = snapshot()
        if state["role_identity_source"] != SOURCE_BRANDED_DEFAULT:
            return {}
        return {
            "role_identity_diag": {
                "source": SOURCE_BRANDED_DEFAULT,
                "degraded": True,
                # Cap the list: this is a diagnostic, not a manifest.
                "missing_prompt_files": state["missing_prompt_files"][:10],
                "detail": (
                    "no role prompt file reached this workspace; it is serving the "
                    "branded platform default identity instead of its provisioned role"
                ),
            }
        }
    except Exception:  # noqa: BLE001 — never break a heartbeat
        return {}


def reset_for_test() -> None:
    """Clear the record (tests only — the record is process-lifetime otherwise)."""
    with _LOCK:
        _STATE["role_identity_source"] = None
        _STATE["missing_prompt_files"] = []
