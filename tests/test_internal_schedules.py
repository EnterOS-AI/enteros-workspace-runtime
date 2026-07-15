"""P3: the runtime schedule API (/internal/schedules*).

Exercised through a real Starlette app + TestClient so the routing, auth, and
store-backed CRUD are covered end to end. Auth is the same platform_inbound
forward-auth as the other /internal/* routes; every route is negative-controlled
for the unauthorized case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from molecule_runtime import internal_schedules
from molecule_runtime.internal_schedules import HEALTH_FILENAME, add_schedule_routes

SECRET = "test-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(internal_schedules, "get_inbound_secret", lambda: SECRET)
    app = Starlette()
    add_schedule_routes(app, state_dir_factory=lambda: tmp_path)
    client = TestClient(app)
    client._state_dir = tmp_path  # type: ignore[attr-defined]
    return client


def test_unauthorized_on_every_route(client: TestClient) -> None:
    assert client.get("/internal/schedules").status_code == 401
    assert client.post("/internal/schedules", json={}).status_code == 401
    assert client.patch("/internal/schedules/x", json={}).status_code == 401
    assert client.delete("/internal/schedules/x").status_code == 401
    assert client.get("/internal/schedules/health").status_code == 401


def test_crud_lifecycle(client: TestClient) -> None:
    assert client.get("/internal/schedules", headers=AUTH).json() == {"schedules": []}

    created = client.post(
        "/internal/schedules",
        headers=AUTH,
        json={"name": "sweep", "cron": "0 * * * *", "prompt": "go"},
    )
    assert created.status_code == 201
    assert created.json()["name"] == "sweep"
    assert created.json()["enabled"] is True

    listed = client.get("/internal/schedules", headers=AUTH).json()["schedules"]
    assert [s["name"] for s in listed] == ["sweep"]

    updated = client.patch(
        "/internal/schedules/sweep", headers=AUTH, json={"enabled": False}
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False

    deleted = client.delete("/internal/schedules/sweep", headers=AUTH)
    assert deleted.status_code == 200
    assert client.get("/internal/schedules", headers=AUTH).json() == {"schedules": []}


def test_create_rejects_invalid_entry_with_400(client: TestClient) -> None:
    # unschedulable cron is rejected at write time by the store's cron gate
    resp = client.post(
        "/internal/schedules",
        headers=AUTH,
        json={"name": "bad", "cron": "99 * * * *", "prompt": "p"},
    )
    assert resp.status_code == 400
    assert "cron" in resp.json()["error"]


def test_create_duplicate_is_400(client: TestClient) -> None:
    body = {"name": "dup", "cron": "0 * * * *", "prompt": "p"}
    assert client.post("/internal/schedules", headers=AUTH, json=body).status_code == 201
    assert client.post("/internal/schedules", headers=AUTH, json=body).status_code == 400


def test_update_missing_is_404(client: TestClient) -> None:
    resp = client.patch("/internal/schedules/ghost", headers=AUTH, json={"enabled": False})
    assert resp.status_code == 404


def test_delete_missing_is_404(client: TestClient) -> None:
    assert client.delete("/internal/schedules/ghost", headers=AUTH).status_code == 404


def test_health_before_any_tick_reports_armed_from_grid(client: TestClient) -> None:
    client.post(
        "/internal/schedules",
        headers=AUTH,
        json={"name": "s", "cron": "0 * * * *", "prompt": "p"},
    )
    health = client.get("/internal/schedules/health", headers=AUTH).json()
    assert health["last_tick"] is None
    assert health["armed"] == 1


def test_health_reads_daemon_health_file(client: TestClient) -> None:
    state_dir: Path = client._state_dir  # type: ignore[attr-defined]
    (state_dir / HEALTH_FILENAME).write_text(
        json.dumps({"last_tick": "2026-01-01T00:00:00+00:00", "armed": 3, "errors": {"x": "bad cron"}}),
        encoding="utf-8",
    )
    health = client.get("/internal/schedules/health", headers=AUTH).json()
    assert health["armed"] == 3
    assert health["errors"] == {"x": "bad cron"}


def test_unconfigured_state_dir_is_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(internal_schedules, "get_inbound_secret", lambda: SECRET)

    def _raises():
        raise internal_schedules.ScheduleStoreUnconfigured("MOLECULE_TRIGGER_STATE_DIR is not set")

    app = Starlette()
    add_schedule_routes(app, state_dir_factory=_raises)
    client = TestClient(app)
    assert client.get("/internal/schedules", headers=AUTH).status_code == 503
