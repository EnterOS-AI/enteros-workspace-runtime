"""Inbound A2A authentication gate — behaviour and negative controls.

The point of this file is NOT to prove that the gate can refuse. Code that
refuses unconditionally also passes a refusal-only suite, and that exact
mutation has slipped through here before. Every refusal assertion below is
therefore paired with an acceptance assertion on the same code path, and
the flag-OFF path is pinned byte-for-byte against the pre-change response
so "ships dark" is a tested property rather than a claim.

Coverage map:

    flag OFF, no credential      → 200, body identical to unguarded route
    flag OFF, wrong credential   → 200 (flag genuinely dominates)
    flag ON,  no credential      → 401
    flag ON,  wrong credential   → 401
    flag ON,  correct credential → 200, body identical to unguarded route
    flag ON,  no secret on disk  → 401 (fail-closed, not fail-open)
    agent card, flag ON          → 200 (deliberately not gated)
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from molecule_runtime import a2a_inbound_gate
from molecule_runtime.a2a_inbound_gate import (
    A2A_REQUIRE_AUTH_ENV,
    a2a_auth_required,
    a2a_auth_status_payload,
    guard_routes,
)

SECRET = "inbound-secret-under-test"
GOOD = {"Authorization": f"Bearer {SECRET}"}
BAD = {"Authorization": "Bearer wrong-secret"}

# The exact body the unguarded route produces. Flag-OFF responses are
# compared against this so a regression that changes the passthrough
# payload is caught, not just one that changes the status code.
UNGUARDED_BODY = {"jsonrpc": "2.0", "result": "ok"}


# ---------- fixtures ----------

@pytest.fixture
def with_secret(monkeypatch: pytest.MonkeyPatch):
    """Workspace has a readable inbound secret."""
    monkeypatch.setattr(a2a_inbound_gate, "get_inbound_secret", lambda: SECRET)


@pytest.fixture
def without_secret(monkeypatch: pytest.MonkeyPatch):
    """Workspace has no secret on disk — get_inbound_secret returns None."""
    monkeypatch.setattr(a2a_inbound_gate, "get_inbound_secret", lambda: None)


@pytest.fixture
def flag_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(A2A_REQUIRE_AUTH_ENV, raising=False)


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(A2A_REQUIRE_AUTH_ENV, "1")


def _rpc_routes() -> list:
    """One JSON-RPC-shaped route, standing in for create_jsonrpc_routes."""

    async def endpoint(_request):
        return JSONResponse(UNGUARDED_BODY)

    return [Route("/", endpoint, methods=["POST"])]


def _client(routes: list) -> TestClient:
    return TestClient(Starlette(routes=routes))


def _post(client: TestClient, headers: dict | None = None):
    return client.post(
        "/",
        json={"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}},
        headers=headers or {},
    )


# ---------- the pre-flip contract: OFF must change nothing ----------

def test_flag_off_unauthenticated_is_allowed_and_body_unchanged(flag_off, with_secret):
    """Today's behaviour, preserved. This is the reno-stars safety property:
    five agents are serving right now with no caller sending a bearer."""
    baseline = _post(_client(_rpc_routes()))
    guarded = _post(_client(guard_routes(_rpc_routes())))

    assert baseline.status_code == 200
    assert guarded.status_code == baseline.status_code
    assert guarded.json() == baseline.json() == UNGUARDED_BODY


def test_flag_off_wrong_credential_still_allowed(flag_off, with_secret):
    """The flag must dominate. If a bad credential were rejected while the
    flag is off, the gate would already be live and the rollout ordering
    would be a fiction."""
    resp = _post(_client(guard_routes(_rpc_routes())), BAD)
    assert resp.status_code == 200
    assert resp.json() == UNGUARDED_BODY


def test_flag_off_is_the_default(monkeypatch: pytest.MonkeyPatch):
    """Unset means OFF — the default is the thing that ships."""
    monkeypatch.delenv(A2A_REQUIRE_AUTH_ENV, raising=False)
    assert a2a_auth_required() is False


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "maybe", "  ", "2"])
def test_unrecognised_flag_values_fail_toward_off(monkeypatch: pytest.MonkeyPatch, raw):
    """A typo must not enforce. Failing toward ON would 503 the fleet on a
    fat-fingered env var."""
    monkeypatch.setenv(A2A_REQUIRE_AUTH_ENV, raw)
    assert a2a_auth_required() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_truthy_flag_values_enable(monkeypatch: pytest.MonkeyPatch, raw):
    """Paired with the test above: the flag must actually be settable.
    Without this, a predicate hardcoded to False would pass the negative
    test and silently never enforce."""
    monkeypatch.setenv(A2A_REQUIRE_AUTH_ENV, raw)
    assert a2a_auth_required() is True


# ---------- the post-flip contract: ON must refuse AND accept ----------

def test_flag_on_unauthenticated_is_refused(flag_on, with_secret):
    """The hole, closed. Unauthenticated POST to the A2A / endpoint."""
    resp = _post(_client(guard_routes(_rpc_routes())))
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_flag_on_wrong_credential_is_refused(flag_on, with_secret):
    resp = _post(_client(guard_routes(_rpc_routes())), BAD)
    assert resp.status_code == 401


def test_flag_on_correct_credential_is_accepted(flag_on, with_secret):
    """The other direction. A gate that refuses everything passes every
    test above this line and fails this one."""
    resp = _post(_client(guard_routes(_rpc_routes())), GOOD)
    assert resp.status_code == 200
    assert resp.json() == UNGUARDED_BODY


def test_flag_on_reaches_the_wrapped_endpoint_exactly_once(flag_on, with_secret):
    """Acceptance must actually invoke the underlying handler — not return
    a synthesized 200. Pins that the guard forwards rather than fakes."""
    calls = []

    async def endpoint(_request):
        calls.append(1)
        return JSONResponse(UNGUARDED_BODY)

    routes = guard_routes([Route("/", endpoint, methods=["POST"])])
    resp = _post(_client(routes), GOOD)

    assert resp.status_code == 200
    assert calls == [1], f"endpoint invoked {len(calls)} times, want exactly 1"


def test_flag_on_endpoint_not_invoked_when_refused(flag_on, with_secret):
    """The mirror: a refusal must short-circuit BEFORE the executor runs.
    A gate that 401s after already driving the agent is not a gate."""
    calls = []

    async def endpoint(_request):
        calls.append(1)
        return JSONResponse(UNGUARDED_BODY)

    routes = guard_routes([Route("/", endpoint, methods=["POST"])])
    resp = _post(_client(routes))

    assert resp.status_code == 401
    assert calls == [], "endpoint ran despite an unauthorized request"


def test_flag_on_missing_secret_file_fails_closed(flag_on, without_secret):
    """No secret on disk must refuse, never bypass. This is the
    'skip auth when unconfigured' mutation, pinned."""
    assert _post(_client(guard_routes(_rpc_routes())), GOOD).status_code == 401
    assert _post(_client(guard_routes(_rpc_routes()))).status_code == 401


def test_case_sensitive_bearer_prefix(flag_on, with_secret):
    """Matches the platform's wsauth.BearerTokenFromHeader contract."""
    resp = _post(_client(guard_routes(_rpc_routes())), {"Authorization": f"bearer {SECRET}"})
    assert resp.status_code == 401


def test_bare_secret_without_bearer_prefix_refused(flag_on, with_secret):
    resp = _post(_client(guard_routes(_rpc_routes())), {"Authorization": SECRET})
    assert resp.status_code == 401


# ---------- route-shape handling ----------

def test_non_route_entries_pass_through_untouched(flag_on, with_secret):
    """Anything that is not a plain Route is returned as-is rather than
    half-wrapped. Pins the documented behaviour so a future a2a-sdk shape
    change surfaces as an obvious ungated route, not a silent one."""
    from starlette.routing import Mount

    mount = Mount("/sub", routes=[])
    out = guard_routes([mount])
    assert out[0] is mount


def test_guard_preserves_path_methods_and_name(flag_off, with_secret):
    async def endpoint(_request):
        return JSONResponse(UNGUARDED_BODY)

    original = Route("/", endpoint, methods=["POST"], name="jsonrpc")
    guarded = guard_routes([original])[0]

    assert guarded.path == original.path
    assert guarded.name == original.name
    assert "POST" in guarded.methods


# ---------- readiness probe ----------

def test_status_payload_reports_flag_and_secret(flag_on, with_secret):
    payload = a2a_auth_status_payload()
    assert payload["require_auth"] is True
    assert payload["inbound_secret_present"] is True
    assert payload["ready_to_enforce"] is True
    assert "runtime_version" in payload


def test_status_payload_reports_not_ready_without_secret(flag_off, without_secret):
    """The condition the pre-flip sweep exists to catch."""
    payload = a2a_auth_status_payload()
    assert payload["require_auth"] is False
    assert payload["inbound_secret_present"] is False
    assert payload["ready_to_enforce"] is False


def test_status_payload_never_contains_the_secret(flag_on, with_secret):
    """A readiness probe that leaks the credential is worse than no probe."""
    assert SECRET not in repr(a2a_auth_status_payload())


def test_status_route_requires_the_inbound_secret(flag_off, with_secret):
    """The probe adds no unauthenticated surface — and is reachable with
    the credential the tenant already holds. Both directions asserted."""
    routes = [Route(
        "/internal/a2a-auth-status",
        a2a_inbound_gate.a2a_auth_status_handler,
        methods=["GET"],
    )]
    client = _client(routes)

    assert client.get("/internal/a2a-auth-status").status_code == 401
    assert client.get("/internal/a2a-auth-status", headers=BAD).status_code == 401

    ok = client.get("/internal/a2a-auth-status", headers=GOOD)
    assert ok.status_code == 200
    assert ok.json()["ready_to_enforce"] is True


# ---------- integration through build_routes ----------

def _install_a2a_stubs(monkeypatch: pytest.MonkeyPatch):
    """Minimal a2a-sdk stubs, mirroring tests/test_boot_routes.py."""

    def _make_card_routes(_agent_card):
        async def card_endpoint(_request):
            return JSONResponse({"name": "stubbed-card"})

        return [Route("/.well-known/agent-card.json", card_endpoint, methods=["GET"])]

    def _make_jsonrpc_routes(**_kwargs):
        async def jsonrpc_endpoint(_request):
            return JSONResponse(UNGUARDED_BODY)

        return [Route("/", jsonrpc_endpoint, methods=["POST"])]

    routes_mod = types.ModuleType("a2a.server.routes")
    routes_mod.create_agent_card_routes = _make_card_routes
    routes_mod.create_jsonrpc_routes = _make_jsonrpc_routes
    monkeypatch.setitem(sys.modules, "a2a.server.routes", routes_mod)

    rh_mod = types.ModuleType("a2a.server.request_handlers")
    rh_mod.DefaultRequestHandler = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "a2a.server.request_handlers", rh_mod)

    tasks_mod = types.ModuleType("a2a.server.tasks")
    tasks_mod.InMemoryTaskStore = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "a2a.server.tasks", tasks_mod)


def test_build_routes_gates_jsonrpc_when_enforcing(
    monkeypatch: pytest.MonkeyPatch, flag_on, with_secret
):
    """End-to-end through the real build_routes, both directions."""
    _install_a2a_stubs(monkeypatch)
    from molecule_runtime.boot_routes import build_routes

    client = _client(build_routes(MagicMock(), MagicMock(), None))

    assert _post(client).status_code == 401
    assert _post(client, GOOD).status_code == 200


def test_build_routes_leaves_agent_card_ungated(
    monkeypatch: pytest.MonkeyPatch, flag_on, with_secret
):
    """Deliberate carve-out: public discovery metadata stays reachable
    even with enforcement on (PR #2756 operator introspection)."""
    _install_a2a_stubs(monkeypatch)
    from molecule_runtime.boot_routes import build_routes

    client = _client(build_routes(MagicMock(), MagicMock(), None))
    assert client.get("/.well-known/agent-card.json").status_code == 200


def test_build_routes_gates_the_not_configured_branch(
    monkeypatch: pytest.MonkeyPatch, flag_on, with_secret
):
    """A workspace whose adapter failed to boot must not become an
    auth-bypass path."""
    _install_a2a_stubs(monkeypatch)
    from molecule_runtime.boot_routes import build_routes

    client = _client(build_routes(MagicMock(), None, "adapter exploded"))
    assert _post(client).status_code == 401

    # ...and still serves its -32603 diagnostic to an authorized caller.
    # 503 (not 200) is this branch's documented contract — the assertion
    # that matters is that an authorized caller gets the DIAGNOSTIC rather
    # than the gate's 401, i.e. the guard forwards instead of swallowing.
    authorized = _post(client, GOOD)
    assert authorized.status_code == 503
    assert authorized.json()["error"]["code"] == -32603


def test_build_routes_unchanged_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, flag_off, with_secret
):
    """The shipping default, through the real build_routes."""
    _install_a2a_stubs(monkeypatch)
    from molecule_runtime.boot_routes import build_routes

    routes = build_routes(MagicMock(), MagicMock(), None)
    assert len(routes) == 2, f"route count changed: {routes}"

    client = _client(routes)
    resp = _post(client)
    assert resp.status_code == 200
    assert resp.json() == UNGUARDED_BODY
