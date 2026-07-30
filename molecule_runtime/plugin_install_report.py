"""Report the declared-plugin boot-install outcome to the platform.

SDK contract: ``contracts/plugin-install-report/`` (vendored here as
``molecule_runtime/contracts/plugin-install-report.contract.json``, byte-identical
per ``contracts/PROVENANCE.md``, drift-gated by
``scripts/check-schemas-in-sync.sh``). Same shape as ``plugin_state`` — a vendored
contract consumed as *executable constants* rather than as a validation schema.

WHY THIS MODULE EXISTS
----------------------

``install_declared_plugins()`` already computes the whole answer on every boot::

    InstallReport(declared, plugins_dir, installed, skipped, failed, swapped)

and ``main.py`` then prints ``report.summary()``. That was the end of it, and two
facts made it a hole rather than a nit:

1. **stdout is not readable on a locked-down prod box.** ``boot_step_emit`` says so
   itself: *"Container stdout may be invisible on locked-down prod boxes … a failed
   emit is not something an operator can act on from the logs."*
2. **The BOOT_STEP that would have carried it is concierge-gated.**
   ``boot_step_emit`` fires only when ``kind=platform``, *"so an ordinary,
   non-concierge tenant boot doesn't spam the endpoint"* — and core's
   ``POST /boot-event`` is BroadcastOnly, *"presentation event, no
   structure_events row"*.

So the one class of box whose plugin install could silently produce nothing was
also the one class the platform could not see. Measured on both prod tenants
2026-07-30 (molecule-core#4953): every ``kind=workspace`` workspace had zero
plugins loaded and an empty schedule grid, and nothing anywhere could say why.

THE TWO DELIBERATE DIFFERENCES FROM ``boot_step_emit``
------------------------------------------------------

* **NOT concierge-gated.** Every workspace reports. This is the entire point, and
  it is a contract constant (``CONCIERGE_GATED is False``) rather than a local
  choice, asserted in tests so a future "reduce the noise" change has to argue
  with the contract instead of quietly re-creating the blind spot. One POST per
  boot, not one per boot-phase, so the volume argument that justifies gating an
  8-step animation feed does not transfer.
* **The platform PERSISTS it** (``DURABLE is True``), so the answer survives for
  an operator asking later rather than only reaching a canvas that happens to be
  attached.

FAIL-SOFT, WITHOUT EXCEPTION
----------------------------

Short timeout; every exception swallowed — connection error, timeout, non-2xx, and
the 404 that occurs on a platform older than core's handler. Observability must
never become a boot dependency: a workspace that cannot report its plugin state
must still boot with the plugins it has. This module therefore returns a bool and
raises nothing, and the call site does not branch on it.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from typing import TYPE_CHECKING, Any, Mapping

import httpx

if TYPE_CHECKING:  # pragma: no cover — typing only
    from molecule_runtime.plugin_sources import InstallReport

logger = logging.getLogger(__name__)

_CONTRACT_RESOURCE = "contracts/plugin-install-report.contract.json"

# Short and fixed. This is best-effort telemetry on the boot path — we would
# rather drop the report than add latency to a cold boot. Mirrors
# boot_step_emit's budget.
_TIMEOUT_SECONDS = 2.0

_contract: dict[str, Any] | None = None


def _load_contract() -> dict[str, Any]:
    """Load the vendored contract once per process."""
    global _contract
    if _contract is None:
        _contract = json.loads(
            resources.files("molecule_runtime")
            .joinpath(_CONTRACT_RESOURCE)
            .read_text(encoding="utf-8")
        )
    return _contract


def path_template() -> str:
    """The endpoint path template, from the contract."""
    return str(_load_contract()["report"]["path_template"])


def http_method() -> str:
    return str(_load_contract()["report"]["method"])


def success_status() -> int:
    return int(_load_contract()["report"]["success_status"])


def concierge_gated() -> bool:
    """False by contract — every workspace reports. See the module docstring."""
    return bool(_load_contract()["concierge_gated"])


def durable() -> bool:
    return bool(_load_contract()["durable"])


def field_names() -> dict[str, str]:
    """InstallReport attribute name -> wire field name."""
    return dict(_load_contract()["fields"])


def report_payload(report: "InstallReport") -> dict[str, Any]:
    """Project an InstallReport onto the contract's wire shape.

    Driven by the contract's ``fields`` map rather than a hand-written dict, so a
    contract rename lands here by construction instead of silently sending a field
    core ignores. A dataclass attribute the contract does not name is NOT sent —
    the contract is the wire, not the dataclass.

    The three list fields are normalised to lists (never ``None``): the receiver
    must not have to tell "reported no failures" from "reported nothing about
    failures", and core stores ``[]`` rather than ``null`` for the same reason.
    """
    payload: dict[str, Any] = {}
    for attr, wire in field_names().items():
        value = getattr(report, attr, None)
        if attr in ("installed", "skipped", "failed"):
            value = list(value or [])
        elif attr == "plugins_dir":
            value = "" if value is None else str(value)
        else:
            value = bool(value)
        payload[wire] = value
    return payload


def report_install_outcome(
    report: "InstallReport",
    *,
    platform_url: str | None = None,
    workspace_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """POST the boot-install outcome. Returns True iff the platform accepted it.

    Never raises. The return value is for tests and for a debug log line — the
    boot path deliberately ignores it.
    """
    try:
        # Reuse boot_step_emit's resolvers rather than re-deriving them: the
        # Docker-aware PLATFORM_URL fallback and the CWE-20 workspace-id
        # validation are exactly the kind of logic that drifts when copied, and
        # the id is interpolated into a URL path.
        from molecule_runtime.boot_step_emit import (
            _resolve_platform_url,
            _resolve_workspace_id,
        )

        base = (platform_url or _resolve_platform_url() or "").rstrip("/")
        wsid = workspace_id or _resolve_workspace_id()
        if not base or not wsid:
            logger.debug(
                "plugin-install-report: no platform url or workspace id — not reporting"
            )
            return False

        from molecule_runtime.platform_auth import auth_headers

        url = base + path_template().replace("{workspace_id}", wsid)
        headers = dict(auth_headers(wsid))
        headers.setdefault("Content-Type", "application/json")

        resp = httpx.request(
            http_method(),
            url,
            json=report_payload(report),
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code == success_status() or 200 <= resp.status_code < 300:
            logger.debug("plugin-install-report: accepted (%s)", resp.status_code)
            return True
        # A 404 is the expected shape against a platform that predates core's
        # handler. Debug, not warning: it is not actionable from inside the box,
        # and boot_step_emit's note about invisible stdout applies here too.
        logger.debug(
            "plugin-install-report: platform returned %s — dropped", resp.status_code
        )
        return False
    except Exception as exc:  # noqa: BLE001 — reporting must never break boot
        logger.debug("plugin-install-report: not reported (%s)", exc)
        return False
