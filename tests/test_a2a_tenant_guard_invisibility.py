"""The A2A relay against a platform that enforces TenantGuard (issue #373).

The fake platform in here is not a generic mock — it reimplements the two
properties of ``molecule-core/workspace-server/internal/middleware/
tenant_guard.go`` that were MEASURED on a live tenant:

  1. the tenant check runs on the ROOT engine, i.e. BEFORE authentication —
     a bogus bearer and a valid bearer produce byte-identical 400s;
  2. ``/workspaces/{id}/a2a`` is in no exemption list.

and one property of the runtime that made the resulting outage undiagnosable:

  3. ``send_a2a_message`` converts a non-2xx into a RETURNED string. It does
     not raise and does not touch any health signal — so a caller with a
     per-message ``except`` (every channel daemon we ship) drops the message
     into a hole while five health signals read green. That is the two-day-
     dark incident (#360), and it is what
     ``test_the_400_is_invisible_to_a_per_message_except`` pins.

core#5029 amends property 3 in ONE respect: a non-2xx now emits a WARNING
carrying the discriminating cause (rate-limit vs upstream timeout vs
generic upstream error). The silent-to-the-caller hole is unchanged and
still pinned below — but the log-based-alert leg of the invisibility is
closed, so this file now asserts the WARNING is present rather than
absent.
"""
from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest

import molecule_runtime.a2a_client as a2a_client
import molecule_runtime.platform_auth as platform_auth

PEER_ID = str(uuid.uuid4())
ORG_ID = "org-7f3a1c22-9d4e-4b18-8c7a-2f5e6d8b1a90"

TENANT_ORG_HEADER = "X-Molecule-Org-Id"
# The platform's verbatim rejection body.
TENANT_REJECTION = {
    "code": "TENANT_ORG_HEADER_REQUIRED",
    "error": "missing tenant routing header",
    "required_header": TENANT_ORG_HEADER,
}


class FakeTenantPlatform:
    """A platform process whose MOLECULE_ORG_ID is set.

    ``requests`` records every inbound request so a test can assert on the
    wire shape rather than on the runtime's internal state.
    """

    def __init__(self, enforce: bool = True):
        self.enforce = enforce
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # (1) root-engine middleware: this runs before ANY auth handling, so
        # it never looks at Authorization.
        if self.enforce and not request.headers.get(TENANT_ORG_HEADER):
            if not request.headers.get("Fly-Replay-Src"):
                return httpx.Response(400, json=TENANT_REJECTION)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"parts": [{"kind": "text", "text": "peer answered"}]},
            },
        )


@pytest.fixture
def platform(monkeypatch):
    """Wire a real httpx client over a MockTransport into the relay path."""
    fake = FakeTenantPlatform()
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("timeout", None)
        return real_async_client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", _factory, raising=True)
    monkeypatch.setenv("WORKSPACE_ID", "ws-sender")
    monkeypatch.setenv("PLATFORM_URL", "http://platform:8080")
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "tok-valid")
    platform_auth.clear_cache()
    return fake


@pytest.mark.asyncio
async def test_relay_reaches_an_org_id_configured_tenant(platform, monkeypatch):
    """The whole point: a workspace→workspace A2A now lands."""
    monkeypatch.setenv("MOLECULE_ORG_ID", ORG_ID)
    platform_auth.clear_cache()

    result = await a2a_client.send_a2a_message(PEER_ID, "hello peer", "ws-sender")

    assert result == "peer answered"
    sent = platform.requests[-1]
    assert sent.headers[TENANT_ORG_HEADER] == ORG_ID
    # the pre-existing contract is untouched
    assert sent.headers["X-Workspace-ID"] == "ws-sender"
    assert sent.headers["Authorization"] == "Bearer tok-valid"
    assert sent.url.path == f"/workspaces/{PEER_ID}/a2a"


@pytest.mark.asyncio
async def test_the_400_is_invisible_to_a_per_message_except(platform, monkeypatch, caplog):
    """NEGATIVE CONTROL + the reason nobody noticed for two days.

    With the tenant header suppressed at the builder — exactly the pre-fix
    state — the relay:

      * does NOT raise, so ``try/except Exception`` around a per-message
        relay catches nothing;
      * RETURNS a string that a caller must remember to inspect.

    A channel daemon that treats "no exception" as "delivered" therefore
    advances its commit frontier over a message the platform never accepted.

    core#5029: the third leg of the original finding — "does NOT log at
    WARNING or above, so no log-based alert fires" — is now FIXED, and
    this test asserts the fix instead of the defect. The WARNING must
    carry the status AND a ``cause=`` discriminator; a log line that
    just says "it failed" would restore the undiagnosable state this
    file exists to prevent.
    """
    monkeypatch.setenv("MOLECULE_ORG_ID", ORG_ID)
    # revert the fix at its single seam
    monkeypatch.setattr(platform_auth, "tenant_headers", dict, raising=True)
    platform_auth.clear_cache()

    caplog.set_level(logging.WARNING)
    raised: Exception | None = None
    try:
        result = await a2a_client.send_a2a_message(PEER_ID, "hello peer", "ws-sender")
    except Exception as exc:  # pragma: no cover - the point is that it doesn't
        raised = exc
        result = ""

    sent = platform.requests[-1]
    assert TENANT_ORG_HEADER not in sent.headers, "fix was not actually reverted"

    assert raised is None, "a raise would have made this outage visible"
    # core#5029: a WARNING+ log now fires, and it names the cause.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "core#5029: a non-2xx A2A response must emit a WARNING"
    logged = "\n".join(r.getMessage() for r in warnings)
    assert "400" in logged, "the WARNING must carry the upstream status"
    assert "cause=" in logged, "the WARNING must carry a discriminating cause"
    assert result.startswith("[A2A_ERROR]")
    assert "400" in result
    # the platform's diagnosis is IN the returned string, and was discarded
    assert "TENANT_ORG_HEADER_REQUIRED" in result

    # And this is the caller shape that lost the message: a per-message loop
    # that guards against exceptions and nothing else commits the message as
    # delivered, because nothing was ever raised.
    committed: list[str] = []
    try:
        _ = result  # the send above; it did not raise
        committed.append("msg-1")
    except Exception:  # pragma: no cover - unreachable, which IS the finding
        pass

    assert committed == ["msg-1"], (
        "a caller that only guards against exceptions advances its commit "
        "frontier over a message the platform rejected — that is the shape "
        "that kept the Gmail channel dark for two days"
    )


@pytest.mark.asyncio
async def test_rejection_precedes_auth_bogus_and_valid_token_are_identical(platform, monkeypatch):
    """Pins the property that proved the ordering on the live box.

    If someone 'fixes' a future 400 by refreshing the token, this test is
    the standing answer: the token was never consulted.
    """
    monkeypatch.setattr(platform_auth, "tenant_headers", dict, raising=True)

    async def _attempt(token: str) -> httpx.Response:
        monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", token)
        platform_auth.clear_cache()
        await a2a_client.send_a2a_message(PEER_ID, "hi", "ws-sender")
        return platform.requests[-1]

    bogus = await _attempt("tok-bogus")
    valid = await _attempt("tok-valid")

    assert bogus.headers["Authorization"] != valid.headers["Authorization"]
    replies = [platform.handler(r) for r in (bogus, valid)]
    assert {r.status_code for r in replies} == {400}
    assert json.loads(replies[0].content) == json.loads(replies[1].content) == TENANT_REJECTION


@pytest.mark.asyncio
async def test_self_host_sends_no_tenant_header_and_still_works(platform, monkeypatch):
    """Self-host: no org id anywhere. The header must be ABSENT, not empty
    and not invented — and the platform (guard in passthrough) still answers.
    """
    monkeypatch.delenv("MOLECULE_ORG_ID", raising=False)
    monkeypatch.delenv("MOLECULE_ORGANIZATION_ID", raising=False)
    platform.enforce = False  # a self-host platform's guard is a passthrough
    platform_auth.clear_cache()

    result = await a2a_client.send_a2a_message(PEER_ID, "hello peer", "ws-sender")

    assert result == "peer answered"
    assert TENANT_ORG_HEADER not in platform.requests[-1].headers
