"""Boot-register retry-with-backoff (internal#688).

Incident (agents-team 2026-05-26 ~19:00 GMT): three workspace EC2s
(PM, CR2, Researcher) launched ~16s AFTER the tenant orchestrator
(workspace-server) was stopped by a workspace-recreate sweep. Their
ONE-SHOT boot-register POST to ``/registry/register`` landed against a
dead workspace-server (Cloudflare 530 / tunnel error 1033). The old code
caught the failure, printed a warning, and proceeded — so ``workspaces.url``
stayed empty for 18+ minutes even though the workspaces were ``online``
and heartbeating. PM's ``pm-autonomous-tick`` schedule then threw
``workspace has no URL`` and never fired. Recovery required a manual
``docker restart molecule-workspace`` on each EC2.

Two failure shapes have to be covered, because both occurred:
  1. Transport exception — workspace-server unreachable (connection
     refused / DNS / timeout). httpx raises.
  2. Non-2xx HTTP response — Cloudflare returns 530 while the origin is
     down. httpx returns a response object with ``status_code=530``; the
     old code only special-cased ``== 200`` and treated everything else
     as "registered" (it printed the status and moved on).

The fix: ``register_with_platform`` retries with bounded exponential
backoff until it gets a 2xx, then returns True. These tests pin:
  - a transient transport error is retried and eventually succeeds,
  - a transient 530 is retried and eventually succeeds,
  - the auth token from the successful response is still captured,
  - retries are bounded (gives up after max attempts, returns False, does
    NOT raise — boot must continue so the workspace can still serve
    traffic / be recovered by the heartbeat backfill),
  - a first-try 200 does NOT sleep/retry (no regression in the hot path),
  - a 4xx client error (e.g. 401 — bad/missing auth, a misconfiguration)
    is NOT retried: it fails fast (review 7658), since retrying just masks
    the real cause behind ~91s of backoff. The Cloudflare 520-530 band is
    the exception and is still retried (it is a transient edge error).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import main as runtime_main  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _ScriptedClient:
    """A stand-in for httpx.AsyncClient whose .post() replays a scripted
    list of outcomes. Each outcome is either an Exception (raised) or a
    _FakeResponse (returned). Records call count."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        if not self._outcomes:
            raise AssertionError("post() called more times than scripted")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Don't actually sleep during backoff — keep tests fast. Patch the
    async sleep the function uses so the backoff schedule is exercised
    without wall-clock cost."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(runtime_main.asyncio, "sleep", _instant)


@pytest.mark.asyncio
async def test_retries_then_succeeds_after_transport_error():
    import httpx

    client = _ScriptedClient([
        httpx.ConnectError("connection refused"),  # orchestrator dead
        httpx.ConnectError("connection refused"),  # still settling
        _FakeResponse(200, {"auth_token": "tok-xyz"}),  # back up
    ])

    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "pm"},
        headers={},
    )

    assert ok is True
    assert client.calls == 3, "must keep retrying until a 2xx, not give up on first error"


@pytest.mark.asyncio
async def test_retries_on_cloudflare_530_then_succeeds():
    # The exact incident shape: Cloudflare 530 (tunnel 1033) while the
    # workspace-server origin is down. Old code treated this as success.
    client = _ScriptedClient([
        _FakeResponse(530),
        _FakeResponse(530),
        _FakeResponse(200, {}),
    ])

    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "cr2"},
        headers={},
    )

    assert ok is True
    assert client.calls == 3, "a 530 is NOT a successful registration and must be retried"


@pytest.mark.asyncio
async def test_captures_auth_token_on_eventual_success(monkeypatch):
    saved = {}
    import molecule_runtime.platform_auth as platform_auth
    monkeypatch.setattr(platform_auth, "save_token", lambda t: saved.__setitem__("token", t))

    client = _ScriptedClient([
        _FakeResponse(530),
        _FakeResponse(200, {"auth_token": "tok-after-retry"}),
    ])

    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "researcher"},
        headers={},
    )

    assert ok is True
    assert saved.get("token") == "tok-after-retry", "token issued on the successful retry must still be saved"


@pytest.mark.asyncio
async def test_bounded_gives_up_and_returns_false_without_raising():
    import httpx

    # Every attempt fails — must give up after the bound, return False,
    # and NOT raise (boot continues; heartbeat/backfill is the safety net).
    client = _ScriptedClient([httpx.ConnectError("down")] * 50)

    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "pm"},
        headers={},
        max_attempts=5,
    )

    assert ok is False
    assert client.calls == 5, "must stop at max_attempts, not retry forever"


@pytest.mark.asyncio
async def test_first_try_success_does_not_retry():
    client = _ScriptedClient([_FakeResponse(200, {})])

    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "pm"},
        headers={},
    )

    assert ok is True
    assert client.calls == 1, "the happy path must not pay any retry/backoff cost"


@pytest.mark.asyncio
async def test_client_error_401_is_not_retried_and_fails_fast():
    # review 7658: a 401 (bad/missing auth — a misconfiguration) is a CLIENT
    # error, not a transient outage. Retrying it would just burn ~91s of
    # backoff and mask the real cause. It must fail fast: exactly one POST,
    # no retry, returns False (boot still continues), and must NOT raise.
    client = _ScriptedClient([
        _FakeResponse(401),
        # Any further outcome would mean we wrongly retried — assert on calls.
        _FakeResponse(200, {"auth_token": "should-never-reach"}),
    ])

    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "pm"},
        headers={},
    )

    assert ok is False, "a 401 is a misconfiguration, not a successful registration"
    assert client.calls == 1, "a 4xx must fail fast — exactly one attempt, no backoff retry"


@pytest.mark.asyncio
async def test_other_4xx_403_404_also_fail_fast():
    # The whole 4xx band (except Cloudflare 520-530) short-circuits.
    for code in (403, 404, 400, 422):
        client = _ScriptedClient([_FakeResponse(code), _FakeResponse(200, {})])
        ok = await runtime_main.register_with_platform(
            client,
            platform_url="https://agents-team.moleculesai.app",
            workspace_id="ws-1",
            workspace_url="http://10.0.0.5:8080",
            agent_card={"name": "pm"},
            headers={},
        )
        assert ok is False, f"HTTP {code} must not be treated as success"
        assert client.calls == 1, f"HTTP {code} must fail fast (1 attempt), got {client.calls}"


@pytest.mark.asyncio
async def test_5xx_is_still_retried():
    # A genuine server error (502/503) is transient — keep the retry behavior.
    client = _ScriptedClient([
        _FakeResponse(503),
        _FakeResponse(502),
        _FakeResponse(200, {}),
    ])
    ok = await runtime_main.register_with_platform(
        client,
        platform_url="https://agents-team.moleculesai.app",
        workspace_id="ws-1",
        workspace_url="http://10.0.0.5:8080",
        agent_card={"name": "pm"},
        headers={},
    )
    assert ok is True
    assert client.calls == 3, "5xx is transient server-side and must be retried"
