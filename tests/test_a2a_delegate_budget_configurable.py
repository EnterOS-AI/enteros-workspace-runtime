"""core#5029 — the A2A delegate ceilings must be OPERABLE and their
failures LEGIBLE.

Two independent ceilings bound a delegated turn:

  * ``_DELEGATE_TOTAL_BUDGET_S_DEFAULT`` (600s) — cumulative wall-clock
    across all retry attempts.
  * ``_DELEGATE_READ_TIMEOUT_S_DEFAULT`` (300s) — per-attempt patience
    for the first byte of the peer's response body.

Before #5029 both were hardcoded module constants: the ONLY
``os.environ`` reads in all 1186 lines of a2a_client.py were
``WORKSPACE_ID`` and ``PLATFORM_URL``. An operator diagnosing a
long-turn failure could neither widen the ceiling without a code change
nor tell WHICH ceiling fired — the terminal string was just the last
httpx exception.

These tests pin BOTH properties, and specifically pin the DEFAULT (the
value production actually runs with when the env var is unset), not
merely the injected override. Injecting a value proves the mechanism,
not production's behaviour.
"""
from __future__ import annotations

import httpx
import pytest


_ENV_BUDGET = "A2A_DELEGATE_TOTAL_BUDGET"
_ENV_READ = "A2A_DELEGATE_READ_TIMEOUT"
_ENV_ATTEMPTS = "A2A_DELEGATE_MAX_ATTEMPTS"

_PEER = "dddd4444-dddd-dddd-dddd-dddddddddddd"


@pytest.fixture(autouse=True)
def _clear_delegate_env(monkeypatch):
    """Every test in this file starts from "operator has set nothing".

    Tests that want an override set it explicitly. This is what makes
    the default-path assertions below meaningful rather than vacuous.
    """
    for name in (_ENV_BUDGET, _ENV_READ, _ENV_ATTEMPTS):
        monkeypatch.delenv(name, raising=False)
    # send_a2a_message resolves a source workspace id; give it a valid one
    # so these tests exercise the delegate ceilings, not id validation.
    monkeypatch.setenv("WORKSPACE_ID", "cccc3333-cccc-cccc-cccc-cccccccccccc")
    yield


# ---------------------------------------------------------------------------
# 1. The DEFAULTS — asserted with the env var UNSET, i.e. production.
# ---------------------------------------------------------------------------


class TestDefaultsWhenEnvUnset:
    def test_total_budget_is_600_when_env_unset(self):
        import molecule_runtime.a2a_client as a2a_client

        assert a2a_client._delegate_total_budget_s() == 600.0

    def test_read_timeout_is_300_when_env_unset(self):
        import molecule_runtime.a2a_client as a2a_client

        assert a2a_client._delegate_read_timeout_s() == 300.0

    def test_max_attempts_is_5_when_env_unset(self):
        import molecule_runtime.a2a_client as a2a_client

        assert a2a_client._delegate_max_attempts() == 5

    def test_declared_default_constants_match_the_pre_5029_hardcoded_values(self):
        """#5029 makes these tunable; it must NOT change what production
        does when nothing is set. Guards against a future 'while I'm here'
        retune sneaking in under the configurability change."""
        import molecule_runtime.a2a_client as a2a_client

        assert a2a_client._DELEGATE_TOTAL_BUDGET_S_DEFAULT == 600.0
        assert a2a_client._DELEGATE_READ_TIMEOUT_S_DEFAULT == 300.0
        assert a2a_client._DELEGATE_MAX_ATTEMPTS_DEFAULT == 5
        assert a2a_client._DELEGATE_CONNECT_TIMEOUT_S == 30.0


# ---------------------------------------------------------------------------
# 2. The OVERRIDES — the mechanism actually reads the environment.
# ---------------------------------------------------------------------------


class TestOverridesWhenEnvSet:
    def test_total_budget_reads_env(self, monkeypatch):
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_BUDGET, "1800")
        assert a2a_client._delegate_total_budget_s() == 1800.0

    def test_read_timeout_reads_env(self, monkeypatch):
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_READ, "900.5")
        assert a2a_client._delegate_read_timeout_s() == 900.5

    def test_max_attempts_reads_env(self, monkeypatch):
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_ATTEMPTS, "9")
        assert a2a_client._delegate_max_attempts() == 9

    @pytest.mark.parametrize("garbage", ["", "   ", "not-a-number", "0", "-5"])
    def test_unparseable_or_nonpositive_falls_back_to_default(self, monkeypatch, garbage):
        """A typo'd env var must NOT collapse the budget to 0 and turn
        every delegation into an instant failure — degrade to the
        documented default instead."""
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_BUDGET, garbage)
        monkeypatch.setenv(_ENV_READ, garbage)
        monkeypatch.setenv(_ENV_ATTEMPTS, garbage)
        assert a2a_client._delegate_total_budget_s() == 600.0
        assert a2a_client._delegate_read_timeout_s() == 300.0
        assert a2a_client._delegate_max_attempts() == 5


# ---------------------------------------------------------------------------
# 3. The knobs reach the REAL send path (not just the accessor).
# ---------------------------------------------------------------------------


def _stub_httpx(monkeypatch, a2a_client, *, post):
    """Install a fake httpx.AsyncClient, capturing the timeout config."""
    captured: dict = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, headers, json):
            captured["url"] = url
            return await post(captured)

    def _factory(timeout):
        captured["timeout"] = timeout
        return _Client()

    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", _factory)
    return captured


class TestSendPathUsesTheConfiguredTimeout:
    @pytest.mark.asyncio
    async def test_default_read_timeout_reaches_httpx(self, monkeypatch):
        """Env UNSET → the httpx client production builds must carry a
        300s read timeout and a 30s connect timeout."""
        import molecule_runtime.a2a_client as a2a_client

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return {"result": {"parts": [{"text": "PONG"}]}}

        async def _post(_captured):
            return _Resp()

        captured = _stub_httpx(monkeypatch, a2a_client, post=_post)
        await a2a_client.send_a2a_message(_PEER, "ping")

        timeout = captured["timeout"]
        assert timeout.read == 300.0
        assert timeout.connect == 30.0

    @pytest.mark.asyncio
    async def test_overridden_read_timeout_reaches_httpx(self, monkeypatch):
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_READ, "900")

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return {"result": {"parts": [{"text": "PONG"}]}}

        async def _post(_captured):
            return _Resp()

        captured = _stub_httpx(monkeypatch, a2a_client, post=_post)
        await a2a_client.send_a2a_message(_PEER, "ping")

        assert captured["timeout"].read == 900.0


# ---------------------------------------------------------------------------
# 4. LEGIBILITY — the terminal error names the ceiling that fired.
# ---------------------------------------------------------------------------


class TestTerminalErrorNamesTheCause:
    @pytest.mark.asyncio
    async def test_attempts_exhaustion_is_named(self, monkeypatch):
        """All attempts consumed while the wall-clock budget still had
        room → cause must say attempts, not budget."""
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_ATTEMPTS, "2")
        monkeypatch.setenv(_ENV_BUDGET, "600")

        async def _post(_captured):
            raise httpx.ReadTimeout("timed out")

        _stub_httpx(monkeypatch, a2a_client, post=_post)
        monkeypatch.setattr(a2a_client, "_delegate_backoff_seconds", lambda _n: 0.0)

        result = await a2a_client.send_a2a_message(_PEER, "ping")

        assert result.startswith(a2a_client._A2A_ERROR_PREFIX)
        assert "cause=attempts_exhausted" in result
        assert "attempts=2/2" in result
        assert "elapsed=" in result
        assert "budget=600.0s" in result
        assert "read_timeout=300.0s" in result
        assert "ReadTimeout" in result

    @pytest.mark.asyncio
    async def test_budget_exhaustion_is_named_distinctly(self, monkeypatch):
        """Wall-clock budget gone while attempts remained → cause must
        say budget, and must NOT be confusable with attempts exhaustion."""
        import molecule_runtime.a2a_client as a2a_client

        import asyncio

        monkeypatch.setenv(_ENV_ATTEMPTS, "5")
        # Each attempt burns 60ms against a 50ms budget, so the deadline
        # is past after attempt #1 while 4 attempts still remain. This is
        # the case a fast in-memory stub would otherwise never reach.
        monkeypatch.setenv(_ENV_BUDGET, "0.05")

        async def _post(_captured):
            await asyncio.sleep(0.06)
            raise httpx.ReadTimeout("timed out")

        _stub_httpx(monkeypatch, a2a_client, post=_post)
        monkeypatch.setattr(a2a_client, "_delegate_backoff_seconds", lambda _n: 0.0)

        result = await a2a_client.send_a2a_message(_PEER, "ping")

        assert result.startswith(a2a_client._A2A_ERROR_PREFIX)
        assert "cause=total_budget_exhausted" in result
        assert "cause=attempts_exhausted" not in result
        assert "attempts=1/5" in result, "budget must stop the loop with attempts left"
        assert "budget=0.05s" in result

    @pytest.mark.asyncio
    async def test_default_budget_is_reported_in_the_terminal_error(self, monkeypatch):
        """No env set at all → the failure names 600.0s, proving the
        DEFAULT (not an injected value) governs the real send path."""
        import molecule_runtime.a2a_client as a2a_client

        monkeypatch.setenv(_ENV_ATTEMPTS, "1")  # keep the test fast; budget untouched

        async def _post(_captured):
            raise httpx.ConnectError("refused")

        _stub_httpx(monkeypatch, a2a_client, post=_post)

        result = await a2a_client.send_a2a_message(_PEER, "ping")

        assert "budget=600.0s" in result
        assert "read_timeout=300.0s" in result
        assert "attempts=1/1" in result

    @pytest.mark.asyncio
    async def test_http_429_is_distinguishable_as_rate_limiting(self, monkeypatch):
        """#5029: a fragment near the errors 'looked like a 429'. A 429
        must be nameable as rate limiting, with Retry-After carried
        through, not folded into a generic non-2xx string."""
        import molecule_runtime.a2a_client as a2a_client

        class _Resp:
            status_code = 429
            headers = {"content-type": "application/json", "retry-after": "30"}
            text = '{"error":"rate limited"}'

        async def _post(_captured):
            return _Resp()

        _stub_httpx(monkeypatch, a2a_client, post=_post)

        result = await a2a_client.send_a2a_message(_PEER, "ping")

        assert result.startswith(a2a_client._A2A_ERROR_PREFIX)
        assert "HTTP 429" in result
        assert "cause=upstream_rate_limited" in result
        assert "retry_after=30" in result

    @pytest.mark.asyncio
    async def test_upstream_gateway_timeout_is_distinguishable(self, monkeypatch):
        """504 from the proxy is an UPSTREAM timeout — a different
        operator action from a local retry-budget exhaustion."""
        import molecule_runtime.a2a_client as a2a_client

        class _Resp:
            status_code = 504
            headers = {"content-type": "text/plain"}
            text = "gateway timeout"

        async def _post(_captured):
            return _Resp()

        _stub_httpx(monkeypatch, a2a_client, post=_post)

        result = await a2a_client.send_a2a_message(_PEER, "ping")

        assert "HTTP 504" in result
        assert "cause=upstream_timeout" in result

    @pytest.mark.asyncio
    async def test_plain_5xx_keeps_its_legacy_shape(self, monkeypatch):
        """Regression guard: the generic non-2xx string that existing
        callers/tests match on must survive the #5029 annotation."""
        import molecule_runtime.a2a_client as a2a_client

        class _Resp:
            status_code = 502
            headers = {"content-type": "text/html"}
            text = "<html>Bad Gateway</html>"

        async def _post(_captured):
            return _Resp()

        _stub_httpx(monkeypatch, a2a_client, post=_post)

        result = await a2a_client.send_a2a_message(_PEER, "ping")

        assert "HTTP 502" in result
        assert "message may have been delivered" in result
        assert "cause=upstream_error" in result
