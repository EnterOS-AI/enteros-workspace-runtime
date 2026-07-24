"""Enter-OS boot-step emitter (task #51 — runtime-emit half).

The runtime walks its cold-boot checklist while a workspace is
``provisioning``. This module POSTs one ``BOOT_STEP`` presentation event per
boot-phase transition to the platform's

    POST {PLATFORM_URL}/workspaces/{WORKSPACE_ID}/boot-event

endpoint (molecule-core ``workspace-server`` ``BootEventHandler``, task
core#3739). The server validates the payload and BroadcastOnly's it to the
canvas over the existing WS hub, where ``BootSequenceScreen`` renders a
per-step keycap animation. There is NO DB row — this is presentation only.

Wire shape the server expects (``bootStepBody``)::

    {
      "step":    3,          # 1-based index of this step (>= 1)
      "total":   8,          # total steps in the boot plan (>= step)
      "key":     "MCP",      # short keycap legend, <= 8 chars
      "label":   "Management MCP",       # human step name
      "status":  "running",  # one of: running | ok | failed
      "message": "…"         # optional log line; on failed = red reason
    }

Design contract (why this module is deliberately paranoid):

* **Fire-and-forget + best-effort.** A short timeout, and EVERY exception is
  swallowed — connection error, timeout, non-2xx, and the 404 that happens
  when core#3739 isn't merged onto the platform yet. Emitting a boot step must
  NEVER block boot, slow boot, or raise into the boot sequence. This PR is safe
  to merge before core#3739: until the endpoint exists the POST 404s and is
  silently dropped, and boot proceeds exactly as it does today.

* **Concierge-gated.** Only emit when this box is the platform concierge
  (``kind=platform``) AND both ``WORKSPACE_ID`` and ``PLATFORM_URL`` are
  resolvable. This mirrors the guard the ``loaded_mcp_tools`` init-capture uses
  (``loaded_mcp_tools_probe._is_platform_agent`` =
  ``on_platform_agent_image() or mcp_server_present()``) so an ordinary,
  non-concierge tenant boot doesn't spam the endpoint.

* **Auth.** The route is mounted under core's ``wsAuth`` group (WorkspaceAuth
  middleware — the same bearer trust boundary as ``POST /activity`` /
  ``POST /notify``), so the POST carries this workspace's own scoped token.
  We reuse ``platform_auth.auth_headers()`` — the single source of truth for
  the ``Authorization: Bearer <token>`` + ``Origin`` header set every other
  runtime→platform POST uses. WorkspaceAuth binds the token to ``:id`` so a
  workspace can only post boot steps for itself.

* **Logs at debug only.** Container stdout may be invisible on locked-down
  prod boxes (platform_agent_identity module note), so a failed emit is not
  something an operator can act on from the logs — we keep it to ``debug`` and
  never print. The boot screen itself degrades gracefully (indeterminate
  fallback) when steps don't arrive.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Server-enforced cap on the keycap legend (core maxBootKeyLen). We enforce it
# client-side too so a runaway key can't even leave the box — a truncated key
# is strictly better than a rejected (400) or layout-breaking one.
_MAX_KEY_LEN = 8

# Short, fixed timeout. This is best-effort presentation telemetry — we would
# rather drop a boot step than add latency to a cold boot. httpx's connect +
# read timeout both derive from this single value.
_TIMEOUT_SECONDS = 2.0

# The closed set of statuses the server accepts (core bootStepStatuses). A
# caller passing anything else is a runtime bug; we drop the emit rather than
# eat a guaranteed 400.
_VALID_STATUSES = ("running", "ok", "failed")


def _resolve_platform_url() -> str:
    """Resolve the platform base URL the same way the rest of the runtime does.

    Prefers the explicit ``PLATFORM_URL`` env var. Falls back to the
    Docker-internal default in-container and localhost otherwise, matching
    ``main.py``'s resolution so a standalone caller (or a call site that
    doesn't pass ``platform_url``) lands on the same base as register/heartbeat.
    Returns "" only when nothing is resolvable, which the caller treats as
    "don't emit".
    """
    url = os.environ.get("PLATFORM_URL", "").strip()
    if url:
        return url.rstrip("/")
    # Mirror main.py's Docker-aware default so an in-container caller that never
    # set PLATFORM_URL still reaches the platform mesh.
    if os.path.exists("/.dockerenv") or os.environ.get("DOCKER_VERSION"):
        return "http://host.docker.internal:8080"
    return "http://localhost:8080"


def _resolve_workspace_id() -> str:
    """Return the validated workspace id, or "" when unset/invalid.

    Routes through ``platform_auth.validate_workspace_id`` (the CWE-20 gate)
    since the id is interpolated into the boot-event URL path. A missing or
    malformed id yields "" so the caller no-ops rather than firing a
    path-injection-shaped request.
    """
    raw = os.environ.get("WORKSPACE_ID", "").strip()
    if not raw:
        return ""
    try:
        from molecule_runtime.platform_auth import validate_workspace_id

        return validate_workspace_id(raw)
    except Exception:  # noqa: BLE001 — bad id => don't emit, never raise
        return ""


def _is_concierge() -> bool:
    """True on the platform concierge (kind=platform), mirroring the guard the
    loaded_mcp_tools init-capture uses (``_is_platform_agent``).

    "Platform-ness" is a composition: the baked platform-agent image marker OR
    the management MCP being wired into the active runtime's config. Ordinary
    tenants match neither, so they never emit boot steps. Defaults to False
    (skip) if the signal can't be read — same fail-closed default as the probe.
    """
    try:
        from molecule_runtime.platform_agent_identity import (
            mcp_server_present,
            on_platform_agent_image,
        )

        return on_platform_agent_image() or mcp_server_present()
    except Exception:  # noqa: BLE001 — never let the gate raise into boot
        return False


def emit_boot_step(
    key: str,
    label: str,
    status: str,
    *,
    step: int,
    total: int,
    message: str | None = None,
) -> None:
    """Fire-and-forget one BOOT_STEP to the platform's boot-event endpoint.

    Best-effort presentation telemetry for the "Enter OS" boot screen. Emits
    ONLY on the platform concierge and ONLY when a workspace id + platform URL
    resolve — otherwise a silent no-op. Swallows EVERY error (connection
    refused, timeout, 404 before core#3739, any non-2xx). Never blocks or
    raises into the boot sequence.

    Args:
        key: short keycap legend, truncated to 8 chars (server cap).
        label: human-readable step name.
        status: one of ``running`` | ``ok`` | ``failed``.
        step: 1-based index of this step (>= 1).
        total: total steps in the boot plan (>= step).
        message: optional log line; on ``status="failed"`` this is the red
            failure reason rendered on the halted keycap.
    """
    try:
        # Cheap guards first — no HTTP unless everything lines up.
        if status not in _VALID_STATUSES:
            logger.debug("emit_boot_step: dropping invalid status %r", status)
            return
        # Prepare mode (review wf_3a7b849d #4): molecule-runtime-prepare runs
        # main() through steps 1-4 BEFORE the real serve does, so without this
        # guard every prepared boot double-emits those steps — the canvas
        # progress bar visibly resets to 1/8 mid-boot and operator tooling
        # counting boot-step events misreads a healthy boot as a restart.
        # prepare_sync sets this env at the source, so the real serve (a
        # separate process without it) still emits normally.
        if os.environ.get("MOLECULE_RUNTIME_PREPARE_ONLY") == "1":
            return
        if not _is_concierge():
            # Ordinary tenant boot — don't spam the endpoint.
            return

        workspace_id = _resolve_workspace_id()
        platform_url = _resolve_platform_url()
        if not workspace_id or not platform_url:
            return

        # Enforce the server's key cap client-side. A truncated key still
        # renders a sensible keycap; an over-length one would 400.
        key = (key or "")[:_MAX_KEY_LEN]

        body = {
            "step": step,
            "total": total,
            "key": key,
            "label": label,
            "status": status,
        }
        if message:
            body["message"] = message

        # Reuse the SSOT auth headers (Bearer token + Origin) every other
        # runtime->platform POST uses. WorkspaceAuth binds the token to :id.
        try:
            from molecule_runtime.platform_auth import auth_headers

            headers = auth_headers()
        except Exception:  # noqa: BLE001 — send open rather than fail
            headers = {}

        url = f"{platform_url}/workspaces/{workspace_id}/boot-event"
        # Synchronous, short-timeout POST. This is called from an async main()
        # but we deliberately don't await an async client: the phase call sites
        # include a branch that raises immediately after (the mgmt-MCP
        # fail-closed abort), and a sync best-effort call keeps the emitter
        # usable from any context without depending on a running loop. The
        # 2s timeout bounds the worst case.
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            client.post(url, json=body, headers=headers)
        logger.debug(
            "emit_boot_step: sent step %s/%s %s=%s", step, total, key, status
        )
    except Exception as exc:  # noqa: BLE001 — MUST never break boot
        # Connection refused / timeout / 404 (core#3739 not merged) / any other
        # failure: swallow. stdout may be invisible on prod boxes, so debug-only.
        logger.debug("emit_boot_step: swallowed %s: %s", type(exc).__name__, exc)
