"""The durable-delegation poll budget must be OPERABLE, DECOUPLED and
DEFAULT-CORRECT — and the durable path must stay default-OFF until the
platform can actually serve a full result.

Three separate properties are pinned here, all of which were broken or
un-pinned before this change:

1. THE DEFAULT WAS HALF THE PATH IT REPLACES.
   ``_SYNC_POLL_BUDGET_S = float(os.environ.get("DELEGATION_TIMEOUT",
   "300.0"))`` gave the durable path a 300s ceiling while the proxy path
   it exists to escape defaults to 600s (``A2A_DELEGATE_TOTAL_BUDGET``).
   Selecting the durable path therefore made long delegations fail
   SOONER, not later — the exact opposite of its purpose. The default is
   now DERIVED from the proxy path's own effective budget, so the two
   ceilings cannot drift and an operator who widened one has widened the
   delegation, not one internal mechanism of it.

2. ONE ENV VAR RETUNED TWO UNRELATED CEILINGS.
   ``DELEGATION_TIMEOUT`` also drives ``builtin_tools/delegation.py``'s
   per-task HTTP client timeout. a2a_client.py:846 already names this
   "the kind of coupling that makes an incident unreadable". The poll
   budget now has its OWN name, ``DELEGATION_POLL_BUDGET_S``; setting it
   must leave the per-task HTTP timeout untouched.

3. A TYPO WAS BOOT-FATAL.
   The old read was a bare ``float()`` at MODULE IMPORT time, and
   ``a2a_tools`` imports this module at load. ``DELEGATION_TIMEOUT=5m``
   raised ValueError before the runtime could serve anything, and
   ``DELEGATION_TIMEOUT=0`` silently collapsed the budget so every
   durable delegation timed out instantly. Both now degrade to the
   documented default, following the ``_env_positive_number`` convention
   established by core#5029.

Plus the gate itself: ``DELEGATION_SYNC_VIA_INBOX`` stays default-OFF.
That is a deliberate, code-proven decision (see the module docstring in
a2a_tools_delegation.py) — the platform's ``GET /delegations`` returns
``response_preview`` truncated to 300 BYTES, so flipping the default
would silently truncate every delegation reply. These tests pin the
UNSET default so a future accidental flip is loud.

Every assertion about a default is made with the env var UNSET —
injecting a value proves the mechanism, not production's behaviour.
"""
from __future__ import annotations

import pytest


_ENV_POLL_BUDGET = "DELEGATION_POLL_BUDGET_S"
_ENV_LEGACY_TIMEOUT = "DELEGATION_TIMEOUT"
_ENV_PROXY_BUDGET = "A2A_DELEGATE_TOTAL_BUDGET"
_ENV_GATE = "DELEGATION_SYNC_VIA_INBOX"

_SRC = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PEER = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _clear_delegation_env(monkeypatch):
    """Start every test from "the operator has set nothing".

    This is what makes the default-path assertions below meaningful
    rather than vacuous: production runs with all four of these unset.
    """
    for name in (_ENV_POLL_BUDGET, _ENV_LEGACY_TIMEOUT, _ENV_PROXY_BUDGET, _ENV_GATE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORKSPACE_ID", _SRC)
    yield


# ---------------------------------------------------------------------------
# 1. The DEFAULT — asserted with every knob UNSET, i.e. what production runs.
# ---------------------------------------------------------------------------


class TestPollBudgetDefaultWhenEnvUnset:
    def test_default_is_the_proxy_paths_own_budget_not_half_of_it(self):
        """The durable path may not give up before the path it replaces."""
        import molecule_runtime.a2a_client as a2a_client
        import molecule_runtime.a2a_tools_delegation as delegation

        assert delegation._sync_poll_budget_s() == a2a_client._delegate_total_budget_s()

    def test_default_is_600_seconds_when_env_unset(self):
        """Pinned literally so a drift in either default is visible here."""
        import molecule_runtime.a2a_tools_delegation as delegation

        assert delegation._sync_poll_budget_s() == 600.0

    def test_default_is_not_the_old_300s_half_ceiling(self):
        import molecule_runtime.a2a_tools_delegation as delegation

        assert delegation._sync_poll_budget_s() != 300.0

    def test_default_tracks_an_operator_widened_proxy_budget(self, monkeypatch):
        """reno-stars runs A2A_DELEGATE_TOTAL_BUDGET=1800.

        An operator who has declared "30 minutes is tolerable for a
        delegation" must not have to discover a SECOND knob to make the
        durable path agree. The default is derived, not a second literal.
        """
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_PROXY_BUDGET, "1800")
        assert delegation._sync_poll_budget_s() == 1800.0

    def test_default_never_exceeds_the_platform_dispatch_ceiling(self):
        """executeDelegation runs under a 30-minute context (core
        workspace-server/internal/handlers/delegation.go), and the a2a
        proxy's own forward ceiling is the same 30 minutes. Polling past
        that is polling for a row nothing will ever move again."""
        import molecule_runtime.a2a_tools_delegation as delegation

        assert delegation._sync_poll_budget_s() <= 1800.0


# ---------------------------------------------------------------------------
# 2. The knob is DISTINCT — point 2 of the coupling complaint.
# ---------------------------------------------------------------------------


class TestPollBudgetIsDecoupledFromThePerTaskTimeout:
    def test_poll_budget_env_overrides_the_default(self, monkeypatch):
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_POLL_BUDGET, "900")
        assert delegation._sync_poll_budget_s() == 900.0

    def test_poll_budget_env_does_not_retune_the_per_task_http_timeout(
        self, monkeypatch
    ):
        """The whole point of the new name: widening the poll budget must
        leave builtin_tools/delegation.py's per-task client timeout alone."""
        import molecule_runtime.builtin_tools.delegation as builtin_delegation

        before = builtin_delegation.DELEGATION_TIMEOUT
        monkeypatch.setenv(_ENV_POLL_BUDGET, "1500")
        assert builtin_delegation.DELEGATION_TIMEOUT == before

    def test_poll_budget_wins_over_the_legacy_shared_name(self, monkeypatch):
        """When both are set the DISTINCT name is authoritative — that is
        how an operator escapes the coupling."""
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_LEGACY_TIMEOUT, "300")
        monkeypatch.setenv(_ENV_POLL_BUDGET, "1200")
        assert delegation._sync_poll_budget_s() == 1200.0

    def test_legacy_delegation_timeout_is_still_honoured_alone(self, monkeypatch):
        """Back-compat: an operator who already set the shared name keeps
        exactly the behaviour they configured. This change is a no-op for
        them, not a silent re-tune."""
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_LEGACY_TIMEOUT, "450")
        assert delegation._sync_poll_budget_s() == 450.0


# ---------------------------------------------------------------------------
# 3. A typo must not be boot-fatal, and must not be a TIGHTER ceiling.
# ---------------------------------------------------------------------------


class TestMalformedValuesDegradeToTheDefault:
    @pytest.mark.parametrize("raw", ["", "   ", "5m", "abc", "0", "-1", "-0.5"])
    def test_bad_value_falls_back_to_the_default_without_raising(
        self, monkeypatch, raw
    ):
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_POLL_BUDGET, raw)
        assert delegation._sync_poll_budget_s() == 600.0

    @pytest.mark.parametrize("raw", ["5m", "0"])
    def test_bad_legacy_value_falls_back_too(self, monkeypatch, raw):
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_LEGACY_TIMEOUT, raw)
        assert delegation._sync_poll_budget_s() == 600.0

    def test_resolution_is_lazy_not_import_time(self, monkeypatch):
        """The old read happened at module import, so the value was frozen
        at whatever the environment looked like when a2a_tools first
        imported this module — and a bad value there killed the boot."""
        import molecule_runtime.a2a_tools_delegation as delegation

        monkeypatch.setenv(_ENV_POLL_BUDGET, "700")
        assert delegation._sync_poll_budget_s() == 700.0
        monkeypatch.setenv(_ENV_POLL_BUDGET, "800")
        assert delegation._sync_poll_budget_s() == 800.0


# ---------------------------------------------------------------------------
# 4. The resolved budget is what the POLL LOOP actually uses.
#    An accessor nobody calls is a vacuous pass.
# ---------------------------------------------------------------------------


class TestPollLoopUsesTheResolvedBudget:
    @pytest.mark.asyncio
    async def test_timeout_message_reports_the_resolved_budget(self, monkeypatch):
        """Drive the real helper to budget exhaustion and read the ceiling
        it reports. Uses a tiny budget so the test is fast — the ASSERTION
        is that the loop honoured the resolved value, not the old 300.0."""
        import molecule_runtime.a2a_tools_delegation as delegation

        # 0.05 is positive, so it is honoured verbatim; the tiny poll
        # interval lets the loop exhaust it in milliseconds.
        monkeypatch.setenv(_ENV_POLL_BUDGET, "0.05")
        monkeypatch.setattr(delegation, "_SYNC_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(
            delegation, "_resolve_platform_url", lambda src: "http://platform.invalid"
        )
        monkeypatch.setattr(
            delegation, "_auth_headers_for_heartbeat", lambda src: {}
        )

        class _Resp:
            status_code = 202

            def json(self):
                return {"delegation_id": "deleg-123"}

        class _PollResp:
            status_code = 200

            def json(self):
                return [{"delegation_id": "deleg-123", "status": "dispatched"}]

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return _Resp()

            async def get(self, *a, **kw):
                return _PollResp()

        monkeypatch.setattr(delegation.httpx, "AsyncClient", _FakeClient)

        out = await delegation._delegate_sync_via_polling(_PEER, "do work", _SRC)
        assert "polling timeout after 0.05s" in out
        assert "300.0" not in out
        assert "deleg-123" in out


# ---------------------------------------------------------------------------
# 5. THE GATE. Default-OFF is a decision, so it gets a test.
# ---------------------------------------------------------------------------


class TestDurablePathGateSelection:
    """``DELEGATION_SYNC_VIA_INBOX`` is still default-off, and that is
    deliberate: the platform's only result-retrieval surface for the
    durable path (``GET /workspaces/:id/delegations``) returns
    ``response_preview`` truncated to 300 BYTES, while the legacy proxy
    path returns the peer's FULL text. Flipping the default would
    silently truncate every delegation reply. Blocked on a platform-side
    full-result read, NOT on DELEGATION_RESULT_INBOX_PUSH (which gates
    only the a2a_receive inbox row this path never reads).
    """

    @staticmethod
    def _install_common_fakes(monkeypatch, delegation, calls):
        async def _fake_discover(workspace_id, source_workspace_id=None):
            return {"name": "peer", "status": "online"}

        async def _fake_report(*a, **kw):
            return None

        async def _fake_send(workspace_id, task, source_workspace_id=None):
            calls.append("legacy")
            return "full legacy answer"

        async def _fake_poll(workspace_id, task, src):
            calls.append("durable")
            return "durable answer"

        monkeypatch.setattr(delegation, "discover_peer", _fake_discover)
        monkeypatch.setattr(delegation, "send_a2a_message", _fake_send)
        monkeypatch.setattr(delegation, "_delegate_sync_via_polling", _fake_poll)
        monkeypatch.setattr(delegation, "_resolve_workspace_id", lambda: _SRC)
        import molecule_runtime.a2a_tools as a2a_tools

        monkeypatch.setattr(a2a_tools, "report_activity", _fake_report)

    @pytest.mark.asyncio
    async def test_unset_gate_selects_the_legacy_proxy_path(self, monkeypatch):
        """THE DEFAULT. Production has this variable unset in every
        environment; the legacy path is what runs."""
        import molecule_runtime.a2a_tools_delegation as delegation

        calls: list[str] = []
        self._install_common_fakes(monkeypatch, delegation, calls)

        out = await delegation.tool_delegate_task(_PEER, "do work")
        assert calls == ["legacy"]
        assert "full legacy answer" in out

    @pytest.mark.asyncio
    async def test_gate_set_to_one_selects_the_durable_path(self, monkeypatch):
        import molecule_runtime.a2a_tools_delegation as delegation

        calls: list[str] = []
        self._install_common_fakes(monkeypatch, delegation, calls)
        monkeypatch.setenv(_ENV_GATE, "1")

        out = await delegation.tool_delegate_task(_PEER, "do work")
        assert calls == ["durable"]
        assert "durable answer" in out

    @pytest.mark.parametrize("kill", ["0", "", "true", "yes", "off"])
    @pytest.mark.asyncio
    async def test_any_value_other_than_one_selects_legacy(self, monkeypatch, kill):
        """The kill-switch is exact-match ``"1"``. Anything else — an
        explicit ``0``, a blank, or a truthy-looking string an operator
        might reach for — must land on the legacy path rather than
        half-enabling the durable one."""
        import molecule_runtime.a2a_tools_delegation as delegation

        calls: list[str] = []
        self._install_common_fakes(monkeypatch, delegation, calls)
        monkeypatch.setenv(_ENV_GATE, kill)

        await delegation.tool_delegate_task(_PEER, "do work")
        assert calls == ["legacy"]
