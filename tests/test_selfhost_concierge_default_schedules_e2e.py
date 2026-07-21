"""Self-host concierge default schedules — the RUNTIME seed→fire→deliver layer.

Companion to the Go-side coverage (molecule-core #4549 graft + #4556 config
composition, both green at the Go integration level). Those prove that on a
**self-host** deployment (``MOLECULE_ORG_ID`` UNSET →
``SelfHostPlatformSeedEnabled()``) core grafts the platform-agent template's two
default schedules onto the concierge's composed ``config.yaml``. This module
proves what the RUNTIME then does with that grafted payload — the three legs the
runtime actually owns:

  * **SEED** — ``seed_schedules_from_workspace_config`` reads the top-level
    ``schedules:`` block from the grafted ``config.yaml`` and materializes BOTH
    real defaults (``daily-activity-report`` @ ``0 9 * * *`` and
    ``plugin-auto-update`` @ ``0 3 * * *``) onto the volume schedule grid, with
    ``source=template``, ``enabled``, ``timezone: UTC``.
  * **FIRE (signal)** — through the real ``/internal/schedules*`` API the trigger
    daemon + canvas use: ``GET /internal/schedules`` surfaces both defaults, and
    ``POST /internal/schedules/daily-activity-report/run`` enqueues the poke the
    per-workspace trigger daemon consumes on its next tick to actually fire the
    turn.
  * **DELIVER** — ``tool_send_message_to_user`` (the tool the
    ``daily-activity-report`` prompt is instructed to call) POSTs
    ``/workspaces/<id>/notify`` and a 200 means the report reached the user.

SCOPE / what is NOT proven here (honest boundary): the middle of the live flow —
the trigger daemon consuming the poke, spinning an LLM agent turn on the
schedule's prompt, and the model *deciding* to call ``send_message_to_user`` —
lives in the external ``molecule-ai-plugin-scheduler`` daemon + the agent/LLM,
neither of which is exercised in a runtime unit test. Each leg the runtime owns
is proven and negative-controlled here; the daemon's own tests cover the
cron-tick→turn dispatch, and the template's prompt authors the tool call.

The full self-host *behavioral* e2e (provision a real concierge with
``MOLECULE_ORG_ID`` unset, GET /schedules over the wire, cron-fire → real
delivery) is NOT cheaply feasible in the ephemeral CP harness: that lane runs
``test_staging_full_saas.sh`` in SaaS mode (CP-injected org id) where the graft
is gated OFF, and the stub runtime carries no scheduler/LLM. Bolting a self-host
assertion onto that SaaS lane would be vacuous, so the strongest genuinely-runnable
gate is this runtime-level one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from molecule_runtime import internal_schedules, schedule_seed
from molecule_runtime.internal_schedules import (
    GRID_FILENAME,
    POKES_FILENAME,
    add_schedule_routes,
)
from molecule_runtime.schedule_store import ScheduleStore

# ---------------------------------------------------------------------------
# The exact grafted config.yaml a self-host concierge boots with. The two
# schedules below are copied VERBATIM from the platform-agent template's
# top-level `schedules:` block (molecule-ai-workspace-template-platform-agent/
# config.yaml) — the same runtime-native form graftConciergeSchedules passes
# through unchanged. Keeping the real names/crons/prompts (not a generic
# fixture) is what makes this a faithful gate on the shipped default: a drift in
# the template payload shape, or a seeder regression that drops one, fails here.
# ---------------------------------------------------------------------------
DAILY_ACTIVITY_PROMPT = (
    "Every morning, report what happened across this deployment in the last 24 "
    "hours. Retrieve recent activity (e.g. GET /workspaces/<your id>/activity"
    "?since_secs=86400, and /mail/summary if available), write a concise "
    "'Yesterday's report' covering agents/tasks active, work completed, notable "
    "events, and any errors, then deliver it to the user with the "
    "send_message_to_user tool. If nothing of note happened, send a brief "
    "'quiet day' note."
)
PLUGIN_AUTO_UPDATE_PROMPT = (
    "Keep this self-hosted deployment up to date. Use check_plugin_updates to "
    "list plugins with a newer version available, and for each, use "
    "apply_plugin_update to apply it (re-pins and restarts the affected "
    "workspace). Also check whether a newer core or runtime version is available "
    "— you CANNOT apply those (operator deploy needed), so only report them. Then "
    "send the user (send_message_to_user) an audit: which plugins you "
    "auto-updated (name old->new) and any core/runtime updates available to "
    "deploy. If the update tools are not available yet, just report that update "
    "tooling is not yet installed and do nothing else."
)

SELFHOST_CONCIERGE_CONFIG_YAML = f"""\
# Composed self-host concierge config.yaml (base runtime template + grafted
# platform-agent schedules). Only the top-level `schedules:` block is read by
# the seeder; the rest mirrors the composed shape for realism.
name: Concierge
runtime: claude-code
runtime_config:
  required_env: []
prompt_files:
  - concierge.md
schedules:
  - name: daily-activity-report
    cron: "0 9 * * *"
    timezone: UTC
    enabled: true
    prompt: "{DAILY_ACTIVITY_PROMPT}"
  - name: plugin-auto-update
    cron: "0 3 * * *"
    timezone: UTC
    enabled: true
    prompt: "{PLUGIN_AUTO_UPDATE_PROMPT}"
"""

# The SaaS-mode concierge config: byte-for-byte the same base WITHOUT the graft
# (graftConciergeSchedules returns grafted=false when MOLECULE_ORG_ID is set, so
# no `schedules:` node is added). Used as the negative control proving the two
# defaults appear on the grid ONLY because the self-host graft added the block —
# the seeder is inert on a SaaS concierge config.
SAAS_CONCIERGE_CONFIG_YAML = """\
name: Concierge
runtime: claude-code
runtime_config:
  required_env: []
prompt_files:
  - concierge.md
"""

DEFAULT_NAMES = {"daily-activity-report", "plugin-auto-update"}
SECRET = "test-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def _write_config(configs_dir: Path, text: str) -> None:
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "config.yaml").write_text(text)


# ===========================================================================
# 1. SEED — the grafted config.yaml materializes BOTH real defaults onto the grid
# ===========================================================================
def test_selfhost_default_schedules_seed_onto_grid(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _write_config(configs, SELFHOST_CONCIERGE_CONFIG_YAML)
    grid = ScheduleStore(tmp_path / "state" / GRID_FILENAME)
    assert grid.list() == []  # precondition: nothing seeded yet

    seeded = schedule_seed.seed_schedules_from_workspace_config(configs, grid)
    assert seeded == 2

    entries = {e["name"]: e for e in grid.list()}
    assert set(entries) == DEFAULT_NAMES

    dar = entries["daily-activity-report"]
    assert dar["cron"] == "0 9 * * *"
    assert dar["timezone"] == "UTC"
    assert dar["enabled"] is True
    assert dar["source"] == "template"  # core-delivered default, not a user edit
    # The report is delivered via send_message_to_user — pin that the shipped
    # prompt still drives the delivery tool this gate's DELIVER leg exercises.
    assert "send_message_to_user" in dar["prompt"]

    pau = entries["plugin-auto-update"]
    assert pau["cron"] == "0 3 * * *"
    assert pau["timezone"] == "UTC"
    assert pau["enabled"] is True
    assert pau["source"] == "template"


def test_saas_concierge_config_seeds_nothing(tmp_path: Path) -> None:
    """Negative control for the self-host gate at the runtime leg.

    A SaaS concierge config carries NO `schedules:` block (the graft is gated
    off when MOLECULE_ORG_ID is set), so the seeder is a clean no-op and the grid
    stays empty. This proves the two defaults land on the grid ONLY because the
    self-host graft added the block — not because the seeder invents them.
    """
    configs = tmp_path / "configs"
    _write_config(configs, SAAS_CONCIERGE_CONFIG_YAML)
    grid = ScheduleStore(tmp_path / "state" / GRID_FILENAME)

    assert schedule_seed.seed_schedules_from_workspace_config(configs, grid) == 0
    assert grid.list() == []


# ===========================================================================
# 2. FIRE (signal) — seeded defaults are visible via GET /schedules and fireable
# ===========================================================================
@pytest.fixture
def seeded_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A real /internal/schedules* app over a state dir pre-seeded from the
    grafted self-host concierge config — i.e. exactly the grid a booted concierge
    exposes to its trigger daemon + canvas."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    configs = tmp_path / "configs"
    _write_config(configs, SELFHOST_CONCIERGE_CONFIG_YAML)
    # Seed into the SAME grid file the API serves (state_dir/GRID_FILENAME).
    assert (
        schedule_seed.seed_schedules_from_workspace_config(
            configs, ScheduleStore(state_dir / GRID_FILENAME)
        )
        == 2
    )
    monkeypatch.setattr(internal_schedules, "get_inbound_secret", lambda: SECRET)
    app = Starlette()
    add_schedule_routes(app, state_dir_factory=lambda: state_dir)
    client = TestClient(app)
    client._state_dir = state_dir  # type: ignore[attr-defined]
    return client


def test_get_schedules_surfaces_both_defaults(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/internal/schedules", headers=AUTH)
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()["schedules"]}
    assert names == DEFAULT_NAMES


def test_daily_activity_report_run_now_enqueues_poke(seeded_client: TestClient) -> None:
    """RunNow on the seeded daily-activity-report → 202 + the poke the trigger
    daemon consumes next tick to fire the turn. This is the runtime-owned end of
    the FIRE seam (the daemon + LLM turn that follows are external)."""
    state_dir: Path = seeded_client._state_dir  # type: ignore[attr-defined]

    resp = seeded_client.post(
        "/internal/schedules/daily-activity-report/run", headers=AUTH
    )
    assert resp.status_code == 202
    assert resp.json() == {"poked": "daily-activity-report"}

    pokes = json.loads((state_dir / POKES_FILENAME).read_text())
    assert pokes == ["daily-activity-report"]


def test_run_now_unknown_schedule_is_404_and_enqueues_no_poke(
    seeded_client: TestClient,
) -> None:
    """Negative control for FIRE: a name that was never seeded cannot be fired
    and leaves no poke — proving the 202+poke above is load-bearing (a broken
    seed that dropped the schedule would 404 here instead)."""
    state_dir: Path = seeded_client._state_dir  # type: ignore[attr-defined]

    resp = seeded_client.post("/internal/schedules/ghost-report/run", headers=AUTH)
    assert resp.status_code == 404
    assert not (state_dir / POKES_FILENAME).exists()


# ===========================================================================
# 3. DELIVER — the tool the report prompt calls reaches /notify with a 200
# ===========================================================================
class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


class _FakeAsyncClient:
    def __init__(self, responses, captured, timeout=None):
        self._responses = list(responses)
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, files=None, headers=None):
        self._captured.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError(f"unexpected POST: {url}")
        return self._responses.pop(0)


_WS = "22222222-2222-2222-2222-222222222222"
_BASE = "http://platform.local"


@pytest.fixture
def stub_delivery(monkeypatch: pytest.MonkeyPatch):
    """Stub the three platform-resolve helpers so the /notify URL is
    deterministic, matching tests/test_a2a_tools_messaging.py."""
    import molecule_runtime.a2a_tools_messaging as m

    monkeypatch.setattr(m, "_resolve_workspace_id", lambda: _WS)
    monkeypatch.setattr(m, "_resolve_platform_url", lambda workspace_id: _BASE)
    monkeypatch.setattr(
        m, "_auth_headers_for_heartbeat", lambda workspace_id: {"authorization": "token test"}
    )
    return m


def test_report_delivery_reaches_notify_200(monkeypatch, stub_delivery) -> None:
    """The daily-activity-report's send_message_to_user call POSTs
    /workspaces/<id>/notify and a 200 means the report reached the user."""
    m = stub_delivery
    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient([_FakeResponse(200, {})], captured),
    )

    result = asyncio.run(
        m.tool_send_message_to_user("Yesterday's report: 3 agents active, 0 errors.")
    )

    assert result == "Message sent to user"
    assert len(captured) == 1
    assert captured[0]["url"] == f"{_BASE}/workspaces/{_WS}/notify"
    assert captured[0]["json"] == {
        "message": "Yesterday's report: 3 agents active, 0 errors."
    }


def test_report_delivery_non_200_is_surfaced_not_swallowed(monkeypatch, stub_delivery) -> None:
    """Negative control for DELIVER: when /notify does NOT return 200 the tool
    must surface an error, never claim the message was sent. Proves the 200
    assertion above is load-bearing."""
    m = stub_delivery
    captured: list[dict] = []
    monkeypatch.setattr(
        m.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            [_FakeResponse(403, {"error": "talk_to_user_disabled", "hint": "ask a parent"})],
            captured,
        ),
    )

    result = asyncio.run(m.tool_send_message_to_user("Yesterday's report: ..."))

    assert result != "Message sent to user"
    assert "not allowed to send messages" in result
    assert captured[0]["url"] == f"{_BASE}/workspaces/{_WS}/notify"
