"""core#2606: normal/external workspaces can raise user-facing task+approval
requests into the unified inbox via the a2a bridge."""
import json
import pytest
import molecule_runtime.a2a_tools_requests as req


class _Resp:
    def __init__(self, code=201, body=None):
        self.status_code = code
        self._body = body or {"request_id": "rq-1", "status": "pending"}
        self.text = json.dumps(self._body)
    def json(self): return self._body


class _Client:
    def __init__(self, capture): self._cap = capture
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, headers=None):
        self._cap["url"] = url; self._cap["json"] = json; self._cap["headers"] = headers
        return _Resp()


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(req, "_resolve_workspace_id", lambda: "ws-self")
    monkeypatch.setattr(req, "_resolve_platform_url", lambda ws: "http://platform:8080")
    monkeypatch.setattr(req, "_auth_headers_for_heartbeat", lambda ws: {"Authorization": "Bearer T"})


@pytest.mark.asyncio
async def test_create_request_task_posts_unified_endpoint(monkeypatch):
    cap = {}
    monkeypatch.setattr(req.httpx, "AsyncClient", lambda *a, **k: _Client(cap))
    out = await req.tool_create_request("task", "Review the report", "by EOD", 5)
    assert cap["url"] == "http://platform:8080/workspaces/ws-self/requests"
    assert cap["json"] == {"kind": "task", "recipient_type": "user", "recipient_id": "",
                           "title": "Review the report", "detail": "by EOD", "priority": 5}
    assert cap["headers"]["Authorization"] == "Bearer T"
    assert "Tasks tab" in out and "rq-1" in out


@pytest.mark.asyncio
async def test_create_approval_maps_to_approval_kind(monkeypatch):
    cap = {}
    monkeypatch.setattr(req.httpx, "AsyncClient", lambda *a, **k: _Client(cap))
    out = await req.tool_create_approval("Delete prod table", "schema migration")
    assert cap["json"]["kind"] == "approval"
    assert cap["json"]["title"] == "Delete prod table"
    assert cap["json"]["detail"] == "schema migration"
    assert "Approvals tab" in out


@pytest.mark.asyncio
async def test_rejects_bad_kind_and_empty_title():
    assert (await req.tool_create_request("memo", "x")).startswith("Error:")
    assert (await req.tool_create_request("task", "  ")).startswith("Error:")


@pytest.mark.asyncio
async def test_registered_in_ssot():
    from molecule_runtime.platform_tools.registry import TOOLS
    names = {t.name for t in TOOLS}
    assert {"create_request", "create_approval"} <= names
