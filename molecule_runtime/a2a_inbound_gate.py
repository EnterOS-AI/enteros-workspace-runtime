"""Authentication gate for the inbound A2A JSON-RPC surface (``POST /``).

Why this module exists
----------------------
Every other network-reachable surface on this runtime authenticates:

    surface            credential                         helper
    ───────            ──────────                         ──────
    /transcript        /configs/.auth_token               transcript_auth
    /internal/*        /configs/.platform_inbound_secret  platform_inbound_auth
    /  (A2A JSON-RPC)  — nothing —                        (this module)

The A2A route was the single omission: ``boot_routes.build_routes`` mounted
``create_jsonrpc_routes(rpc_url="/")`` with no guard, and the only ASGI
wrapper on the app is ``make_trace_middleware`` (telemetry — it never
inspects Authorization and never short-circuits). uvicorn binds 0.0.0.0
with no TLS. So *any* peer that can reach the port can drive the agent's
executor, unauthenticated.

That was survivable only because the deployed boundary is the network —
the tenant namespace plus a default-deny NetworkPolicy. The moment
workspaces talk over the public internet (the per-workspace tunnel this
platform is moving to) that boundary disappears, so the credential has to
become the boundary instead. This module is that credential check.

The secret is NOT new. The platform already mints a per-workspace
``platform_inbound_secret`` at provision time and re-delivers it on every
register/heartbeat; the runtime already persists it to
``/configs/.platform_inbound_secret`` and already validates it on
``/internal/*``. Nothing new is distributed — the same bearer simply
starts being *checked* on one more route.

Rollout is a one-way door, so it is flagged
--------------------------------------------
Turning enforcement on before every caller sends the header 503s live
agents. So enforcement is OFF by default and gated on
``MOLECULE_A2A_REQUIRE_AUTH``. The intended sequence is:

  1. Platform side ships "always send the bearer" (molecule-core).
  2. This runtime ships, enforcement still OFF — behaviour unchanged.
  3. Operator measures readiness via ``GET /internal/a2a-auth-status``
     on every live workspace (see ``a2a_auth_status_payload``) and only
     then flips the flag.

Step 3 is a *measurement*, not a guess: the status route reports whether
this workspace can actually satisfy the check
(``inbound_secret_present``). A workspace with the new code but no secret
on disk would 401 every caller the instant the flag flipped — that is
precisely the condition the probe exists to catch. The route's mere
existence (200 rather than 404) is also the "is this workspace on the new
runtime" signal, so no version arithmetic is needed.

Deliberately NOT changed here
-----------------------------
``/.well-known/agent-card.json`` stays ungated. It is public discovery
metadata in the A2A protocol, and ``boot_routes`` mounts it
unconditionally on purpose (PR #2756: an operator must be able to
introspect a workspace whose adapter failed to boot). Gating it is a
protocol-visible change with a different blast radius and belongs in its
own change; it exposes description text, not an execution path. Flagged
rather than silently bundled.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from molecule_runtime.platform_inbound_auth import (
    get_inbound_secret,
    inbound_authorized,
)

logger = logging.getLogger(__name__)

# Env key as a named constant so the flag name has exactly one spelling —
# the status payload and the gate must never disagree about which var
# they are reporting on.
A2A_REQUIRE_AUTH_ENV = "MOLECULE_A2A_REQUIRE_AUTH"

# The distribution name as actually published. NOTE: mcp_doctor.py queries
# "molecule-ai-workspace-runtime", which is the *repo* name, not the dist
# name declared in pyproject.toml — that lookup always takes the
# PackageNotFoundError branch. Not fixed here (out of scope for an auth
# change) but recorded so the next reader does not copy the wrong string.
_DIST_NAME = "molecules-workspace-runtime"

# Same truthy vocabulary as smoke_mode.is_smoke_mode() — house style for an
# off-by-default flag in this runtime.
_TRUTHY = ("1", "true", "yes", "on")


def a2a_auth_required() -> bool:
    """True iff inbound A2A requests must present the platform bearer.

    Default OFF. An unset, empty, or unrecognised value means OFF, so a
    typo'd flag fails toward today's behaviour rather than toward 503ing
    a live fleet.
    """
    return os.environ.get(A2A_REQUIRE_AUTH_ENV, "").strip().lower() in _TRUTHY


def runtime_version() -> str:
    """Installed version of this runtime distribution, or "unknown".

    Diagnostic only — the readiness gate keys on ``inbound_secret_present``
    and on this route existing at all, never on parsing this string.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version(_DIST_NAME)
    except PackageNotFoundError:
        return "unknown"
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        logger.debug("a2a_inbound_gate: version lookup failed: %s", exc)
        return "unknown"


def guard_endpoint(endpoint: Any) -> Any:
    """Wrap a Starlette endpoint with the inbound A2A bearer check.

    When the flag is OFF the wrapper is a pure pass-through: it awaits the
    original endpoint before reading a single header, so the response is
    byte-for-byte what the unwrapped route would have produced. That
    property is pinned by a test — it is the whole reason this ships dark.
    """

    async def guarded(request: Any) -> Any:
        # Flag check FIRST and return immediately. Nothing below this line
        # runs in the default configuration.
        if not a2a_auth_required():
            return await endpoint(request)
        if not inbound_authorized(
            get_inbound_secret(), request.headers.get("Authorization", "")
        ):
            # Deliberately terse and identical for every failure mode
            # (absent header, wrong secret, unreadable secret file) so the
            # response cannot be used to distinguish "no secret on disk"
            # from "wrong secret presented". Mirrors the /internal/* body.
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await endpoint(request)

    # Preserve introspectability; some a2a-sdk versions log the handler name.
    guarded.__name__ = getattr(endpoint, "__name__", "guarded_a2a_endpoint")
    return guarded


def guard_routes(routes: list) -> list:
    """Return ``routes`` with every Starlette ``Route`` endpoint guarded.

    Anything that is not a plain ``Route`` (a ``Mount``, a WebSocket route,
    whatever a future a2a-sdk returns) is passed through UNTOUCHED rather
    than guessed at. A surface this module does not understand must not be
    silently half-wrapped — that would look enforced while not being.
    Such a case is logged so it is visible rather than assumed absent.
    """
    guarded: list = []
    for route in routes:
        if isinstance(route, Route):
            guarded.append(
                Route(
                    route.path,
                    guard_endpoint(route.endpoint),
                    methods=list(route.methods or ["POST"]),
                    name=route.name,
                )
            )
        else:
            logger.warning(
                "a2a_inbound_gate: leaving non-Route %s UNGUARDED on the A2A "
                "surface; it needs an explicit decision before enforcement",
                type(route).__name__,
            )
            guarded.append(route)
    return guarded


def a2a_auth_status_payload() -> dict:
    """Readiness facts for the pre-flip measurement.

    Read the HTTP STATUS, not just this body. Because the route is itself
    gated by the inbound secret, the status code carries the readiness
    verdict end-to-end:

        404 → workspace is on an older runtime; do not flip.
        401 → new runtime, but the caller's secret does not match what
              this workspace has on disk (or it has none). Flipping the
              flag would 401 this workspace's real A2A traffic too.
        200 → the tenant's copy of the secret and the workspace's copy
              agree, which is exactly what enforcement will require.

    So ``ready_to_enforce`` is True whenever this body is reachable at
    all — it is a tautology by construction, kept because it makes the
    contract self-describing to whoever automates the sweep. The load
    bearing signal is the status code. ``require_auth`` is the useful
    field after the flip: it confirms the flag actually landed on this
    process, which an env-var change only takes effect on restart.
    """
    secret_present = get_inbound_secret() is not None
    return {
        "require_auth": a2a_auth_required(),
        "inbound_secret_present": secret_present,
        "ready_to_enforce": secret_present,
        "runtime_version": runtime_version(),
    }


async def a2a_auth_status_handler(request: Any) -> Any:
    """GET /internal/a2a-auth-status.

    Gated by the SAME inbound secret as the rest of ``/internal/*`` — the
    tenant doing the measuring already holds it, so the probe adds no
    unauthenticated surface. It reports only booleans and a version
    string; it never returns the secret.
    """
    if not inbound_authorized(
        get_inbound_secret(), request.headers.get("Authorization", "")
    ):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(a2a_auth_status_payload())
