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

FAIL-SOFT IS NOT THE SAME AS SILENT
-----------------------------------

Fail-soft was originally implemented as *fail-quiet*: every drop arm logged at
``debug``, so a workspace that never reported looked exactly like a workspace that
reported fine. That turned this module — whose entire purpose is to answer "did
boot-install work here?" — into something that could itself fail invisibly, which
is the same blind spot one level up.

So the drop arms are now graded by whether an operator can ACT on them:

* ``warning`` — **resolution failure** (no platform URL or no workspace id: the box
  is misconfigured), **401/403** (the receiver rejected our credentials), **400**
  (we sent a malformed payload — see ``report_payload``), any **other non-2xx**,
  and any **transport exception**. Each names the arm, the reason, the resolved
  URL, and the status code where there is one.
* ``debug`` — **404**. A platform that predates core's handler answers 404 by
  construction; it is expected during a rollout, not a misconfiguration, and it is
  not actionable from inside the box.

What did NOT change: the function still returns a bool, still raises nothing, and
``main.py`` still does not branch on it. Raising a log level cannot alter control
flow. There is at most ONE such line per boot — this runs once, after
``install_declared_plugins()``, not once per boot phase.

The resolved URL is safe to log: it is the platform base plus the workspace id
(already in every other log line). Credentials live in headers and are never
logged.
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


# Sentinel for "the report object has no such attribute at all", which is a
# different fact from "the attribute is there and is None". Only the former is
# drift; the latter is a legitimate value the coercions below normalise.
_ABSENT = object()


def report_payload(report: "InstallReport") -> dict[str, Any]:
    """Project an InstallReport onto the contract's wire shape.

    Driven by the contract's ``fields`` map rather than a hand-written dict, so a
    contract rename lands here by construction instead of silently sending a field
    core ignores. A dataclass attribute the contract does not name is NOT sent —
    the contract is the wire, not the dataclass.

    ABSENT IS NOT FALSE
    -------------------

    An attribute the report object does not have is **omitted from the payload**,
    never coerced. This is the whole reason core declares ``declared`` and
    ``swapped`` as ``*bool`` rather than ``bool``
    (``workspace-server/internal/handlers/plugin_install_report.go``)::

        if body.Declared == nil || body.Swapped == nil {
            // "absent is not the same as false"
            400
        }

    Coercing a missing attribute with ``bool()`` — which is what this function did
    until runtime#379 — hands core a definitive ``declared:false``, which core
    accepts, persists, and renders to an operator as *"core never asked for a
    plugin"*: the wrong repo, and precisely the mis-diagnosis the field exists to
    prevent. Omitting the key instead makes core answer 400 with the reason in the
    body, and core's request log is readable where this box's stdout is not (see
    the module docstring) — a 400 an operator can see beats a persisted lie.

    So the drift is loud on the receiver, and it costs nothing here: the producer
    is fail-soft by contract, so a 400 cannot break a boot. A local ``warning`` is
    emitted too, but it is the belt, not the braces — stdout invisibility is the
    premise of this whole module.

    The three list fields are still normalised to lists when PRESENT (never
    ``None``): the receiver must not have to tell "reported no failures" from
    "reported nothing about failures", and core stores ``[]`` rather than ``null``
    for the same reason.
    """
    payload: dict[str, Any] = {}
    missing: list[str] = []
    for attr, wire in field_names().items():
        value = getattr(report, attr, _ABSENT)
        if value is _ABSENT:
            # Do NOT invent a value. See the docstring: core distinguishes
            # absent from false on purpose, and this is the producer half of it.
            missing.append(attr)
            continue
        if attr in ("installed", "skipped", "failed"):
            value = list(value or [])
        elif attr == "plugins_dir":
            value = "" if value is None else str(value)
        elif attr == "installed_refs":
            # A MAPPING, not a flag. The else-branch below coerces with bool(),
            # which would send `true` for a populated dict and `false` for an
            # empty one — a plausible-looking payload carrying no commits at
            # all, which is worse than omitting the field. Normalised to
            # str->str so the wire shape cannot depend on what the resolver
            # happened to return.
            value = {str(k): str(v) for k, v in dict(value or {}).items()}
        else:
            value = bool(value)
        payload[wire] = value
    if missing:
        logger.warning(
            "plugin-install-report: report object is missing %s — omitting from the "
            "payload rather than sending false; the platform will refuse it (400) so "
            "the drift is visible instead of persisted as a definitive answer",
            ", ".join(sorted(missing)),
        )
    return payload


def report_install_outcome(
    report: "InstallReport",
    *,
    platform_url: str | None = None,
    workspace_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """POST the boot-install outcome. Returns True iff the platform accepted it.

    Never raises. The return value is for tests and for a log line — the boot path
    deliberately ignores it.

    Every drop is logged. Actionable drops (resolution failure, 401/403, 400, any
    other non-2xx, transport error) log at ``warning`` and name the arm, the
    reason, the resolved URL and the status code; an expected 404 from a platform
    older than core's handler stays at ``debug``. See the module docstring.
    """
    # Tracked outside the try so the transport-exception arm can name the URL it
    # was talking to. "" until resolution succeeds — an exception raised during
    # resolution genuinely has no URL to report, and saying so is more honest than
    # printing a half-built one.
    url = ""
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
            # Actionable: the box cannot address the platform at all. In practice
            # this is the workspace id — _resolve_platform_url() falls back to a
            # Docker/localhost default and so effectively never returns "" —
            # meaning WORKSPACE_ID is unset, or set to something
            # validate_workspace_id rejects. Either is a provisioning bug an
            # operator fixes, and at debug it was indistinguishable from success.
            unresolved = ", ".join(
                name
                for name, ok in (("platform url", bool(base)), ("workspace id", bool(wsid)))
                if not ok
            )
            logger.warning(
                "plugin-install-report: DROPPED before sending — could not resolve %s "
                "(platform url=%r, workspace id=%r). Nothing was recorded for this "
                "workspace; check PLATFORM_URL / WORKSPACE_ID in the container "
                "environment (a WORKSPACE_ID that fails validation resolves to empty)",
                unresolved,
                base,
                wsid,
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
        if resp.status_code == 400:
            # 400 means WE sent a malformed report — in practice, a required
            # field omitted because InstallReport drifted (see report_payload).
            # That is a runtime bug; the authoritative copy is core's own request
            # log, which is readable where this box's stdout is not.
            logger.warning(
                "plugin-install-report: DROPPED — platform REFUSED the report (400) "
                "at POST %s. The payload is malformed, most likely an InstallReport "
                "field that no longer exists (see the omission warning above); "
                "nothing was recorded for this workspace",
                url,
            )
            return False
        if resp.status_code == 404:
            # The expected shape against a platform that predates core's handler.
            # Debug, not warning: during a rollout this is correct behaviour, not
            # a misconfiguration, and it is not actionable from inside the box.
            logger.debug(
                "plugin-install-report: platform has no handler at %s (404) — "
                "dropped; expected on a platform older than core's "
                "plugin-install-report handler",
                url,
            )
            return False
        if resp.status_code in (401, 403):
            # Actionable and NOT self-healing: the box is talking to the right
            # place and being refused. Distinct from 404 — a rollout does not fix
            # this, an operator does.
            logger.warning(
                "plugin-install-report: DROPPED — platform REJECTED our credentials "
                "(%s) at POST %s. This is a workspace-token/auth misconfiguration, "
                "not a platform-version skew; nothing was recorded for this "
                "workspace",
                resp.status_code,
                url,
            )
            return False
        logger.warning(
            "plugin-install-report: DROPPED — platform returned %s at POST %s; "
            "nothing was recorded for this workspace",
            resp.status_code,
            url,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — reporting must never break boot
        # Connection refused, DNS failure, timeout, TLS error — or a failure in
        # resolution/import before the URL existed. All actionable: the report
        # never reached the platform and no row will appear for this workspace.
        logger.warning(
            "plugin-install-report: DROPPED — could not reach the platform at %s: "
            "%s: %s. Nothing was recorded for this workspace",
            url or "<url unresolved>",
            type(exc).__name__,
            exc,
        )
        return False
