"""A Light run pod never heartbeats, registers, polls the inbox or emits boot steps.

A run pod (``MOLECULE_RUN_MODE`` set, contract C1) runs one task for a PARKED
agent. A heartbeat or register from it would write the parked agent's row and
could wake it, and an inbox poller would consume messages meant for the agent
itself. The switch is ``workspace_mode.in_run_pod()``, read from the
environment, because the MCP server and the CLIs a template starts in the same
container inherit the env but share no in-process state with the runtime.

``test_every_registry_post_site_is_reviewed`` is the permanent gate: a new
``/registry/heartbeat`` or ``/registry/register`` call site anywhere in the
package fails it until someone decides what that site does in a run pod.
"""
from __future__ import annotations

import ast
import importlib
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import molecule_runtime.platform_auth as platform_auth

P = "http://platform.test"
WS = "11111111-2222-3333-4444-555555555555"
RUN_ID = "light-ws-abc-123"
PKG_ROOT = pathlib.Path(platform_auth.__file__).resolve().parent


class _Http:
    """Records every POST made through httpx (sync or async client)."""

    def __init__(self):
        self.calls: list[tuple[str, object, dict]] = []

    def _record(self, url, json=None, headers=None, **_kw):
        self.calls.append((str(url), json, dict(headers or {})))
        return SimpleNamespace(status_code=200, json=lambda: {}, read=lambda: b"")

    def async_client(self, *_a, **_k):
        rec = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, url, **kw):
                return rec._record(url, **kw)

        return _Client()

    def sync_client(self, *_a, **_k):
        rec = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def post(self, url, **kw):
                return rec._record(url, **kw)

        return _Client()

    def post(self, url, **kw):
        return self._record(url, **kw)

    @property
    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def run_pod(monkeypatch):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    monkeypatch.setenv("MOLECULE_RUN_ID", RUN_ID)
    monkeypatch.setenv("WORKSPACE_ID", WS)
    monkeypatch.setenv("PLATFORM_URL", P)
    monkeypatch.delenv("MOLECULE_RUNTIME_PREPARE_ONLY", raising=False)
    platform_auth.clear_cache()
    yield
    platform_auth.clear_cache()


@pytest.fixture
def serving(monkeypatch):
    monkeypatch.delenv("MOLECULE_RUN_MODE", raising=False)
    monkeypatch.setenv("WORKSPACE_ID", WS)
    monkeypatch.setenv("PLATFORM_URL", P)
    platform_auth.clear_cache()
    yield
    platform_auth.clear_cache()


# --- executor_helpers.set_current_task ---------------------------------------


@pytest.mark.asyncio
async def test_set_current_task_counts_but_never_posts(run_pod, monkeypatch):
    import molecule_runtime.executor_helpers as eh

    client = AsyncMock()
    client.is_closed = False
    monkeypatch.setattr(eh, "_http_client", client)
    hb = SimpleNamespace(current_task="", active_tasks=0)

    await eh.set_current_task(hb, "x")
    assert hb.active_tasks == 1 and hb.current_task == "x"
    await eh.set_current_task(hb, "")
    assert hb.active_tasks == 0 and hb.current_task == ""
    client.post.assert_not_called()


# --- a2a_tools.report_activity ------------------------------------------------


async def _report_activity(monkeypatch) -> _Http:
    import molecule_runtime.a2a_tools as a2a_tools

    http = _Http()
    monkeypatch.setattr(a2a_tools, "PLATFORM_URL", P)
    monkeypatch.setattr(a2a_tools.httpx, "AsyncClient", http.async_client)
    await a2a_tools.report_activity("a2a_send", "peer-1", summary="Delegating")
    return http


@pytest.mark.asyncio
async def test_report_activity_posts_activity_not_heartbeat(run_pod, monkeypatch):
    http = await _report_activity(monkeypatch)
    assert http.urls == [f"{P}/workspaces/{WS}/activity"]


# --- builtin_tools/memory._maybe_log_skill_promotion --------------------------


async def _skill_promotion(monkeypatch) -> _Http:
    from molecule_runtime.builtin_tools import memory

    http = _Http()
    monkeypatch.setattr(memory, "WORKSPACE_ID", WS)
    monkeypatch.setattr(memory, "PLATFORM_URL", P)
    monkeypatch.setattr(memory.httpx, "AsyncClient", http.async_client)
    content = json.dumps({"promote_to_skill": True, "summary": "weekly audit"})
    await memory._maybe_log_skill_promotion(content, "workspace", {"id": "m1"})
    return http


@pytest.mark.asyncio
async def test_skill_promotion_log_skips_heartbeat(run_pod, monkeypatch):
    http = await _skill_promotion(monkeypatch)
    assert http.urls == [f"{P}/workspaces/{WS}/activity"]


# --- molecule_ai_status.set_status (shell CLI) --------------------------------


def _set_status(monkeypatch) -> _Http:
    import molecule_runtime.molecule_ai_status as status

    status = importlib.reload(status)  # module reads WORKSPACE_ID/PLATFORM_URL at import
    http = _Http()
    monkeypatch.setattr(status.httpx, "post", http.post)
    status.set_status("Running audit")
    return http


def test_status_cli_skips_heartbeat(run_pod, monkeypatch):
    http = _set_status(monkeypatch)
    assert http.urls == [f"{P}/workspaces/{WS}/activity"]


# --- serve mode keeps every heartbeat -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["report_activity", "skill_promotion", "set_status"])
async def test_heartbeat_still_posted_when_serving(serving, monkeypatch, site):
    if site == "report_activity":
        http = await _report_activity(monkeypatch)
    elif site == "skill_promotion":
        http = await _skill_promotion(monkeypatch)
    else:
        http = _set_status(monkeypatch)
    assert f"{P}/registry/heartbeat" in http.urls


# --- heartbeat.HeartbeatLoop.start --------------------------------------------


def test_heartbeat_loop_start_is_a_noop(run_pod):
    from molecule_runtime.heartbeat import HeartbeatLoop

    hb = HeartbeatLoop(P, WS)
    hb.start()  # no running event loop needed: it must return before arming anything
    assert hb._hb_thread is None
    assert hb._task is None
    assert hb._mcp_prober is None


# --- mcp_cli.main (molecule-mcp) ----------------------------------------------


def test_mcp_cli_skips_register_heartbeat_and_inbox(run_pod, monkeypatch):
    import molecule_runtime.mcp_cli as mcp_cli

    mcp_cli = importlib.reload(mcp_cli)
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "run-token")
    monkeypatch.delenv("MOLECULE_WORKSPACES", raising=False)
    monkeypatch.delenv("MOLECULE_MCP_DISABLE_HEARTBEAT", raising=False)
    monkeypatch.delenv("MOLECULE_MCP_DISABLE_INBOX", raising=False)
    register, heartbeat, inbox, served = [], [], [], []
    monkeypatch.setattr(mcp_cli, "_platform_register", lambda *a: register.append(a))
    monkeypatch.setattr(mcp_cli, "_start_heartbeat_thread", lambda *a: heartbeat.append(a))
    monkeypatch.setattr(mcp_cli, "_start_inbox_pollers", lambda *a, **k: inbox.append(a))
    monkeypatch.setattr(
        "molecule_runtime.a2a_mcp_server.cli_main", lambda: served.append(True), raising=False
    )

    mcp_cli.main()

    assert register == [] and heartbeat == [] and inbox == []
    assert served == [True]  # the MCP server still serves its tools


# --- mcp_doctor.check_token_auth ----------------------------------------------


def test_doctor_skips_token_probe(run_pod, monkeypatch):
    import molecule_runtime.mcp_doctor as mcp_doctor

    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "run-token")
    opened = []
    monkeypatch.setattr(mcp_doctor.urllib_request, "urlopen", lambda *a, **k: opened.append(a))
    assert mcp_doctor.check_token_auth() == "warn"
    assert opened == []


# --- boot_step_emit.emit_boot_step --------------------------------------------


def test_emit_boot_step_silent(run_pod, monkeypatch):
    import molecule_runtime.boot_step_emit as boot_step_emit

    http = _Http()
    monkeypatch.setattr(boot_step_emit, "_is_concierge", lambda: True)
    monkeypatch.setattr(boot_step_emit.httpx, "Client", http.sync_client)
    boot_step_emit.emit_boot_step("PLG", "Install plugins", "running", step=1, total=8)
    assert http.calls == []


# --- the permanent gate --------------------------------------------------------

_REGISTRY_ROUTES = ("/registry/heartbeat", "/registry/register")
_HTTP_CALL_NAMES = {"post", "Request"}

# Every reviewed call site, with what it does in a run pod.
REVIEWED = {
    ("a2a_tools.py", "report_activity"),  # heartbeat skipped, activity kept
    ("builtin_tools/memory.py", "_maybe_log_skill_promotion"),  # heartbeat skipped
    ("executor_helpers.py", "set_current_task"),  # counts only, no POST
    ("heartbeat.py", "_send_heartbeat"),  # unreachable: HeartbeatLoop.start() is a no-op
    ("main.py", "register_with_platform"),  # unreachable: the once branch exits first
    ("mcp_doctor.py", "check_token_auth"),  # probe skipped
    ("mcp_heartbeat.py", "platform_register"),  # unreachable: mcp_cli.main skips it
    ("mcp_heartbeat.py", "heartbeat_loop"),  # unreachable: mcp_cli.main skips it
    ("molecule_ai_status.py", "set_status"),  # heartbeat skipped, activity kept
}


def _registry_post_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(PKG_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _HTTP_CALL_NAMES:
                continue
            if not any(route in ast.unparse(node.args[0]) for route in _REGISTRY_ROUTES):
                continue
            owner = node
            while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents[owner]
            sites.add((path.relative_to(PKG_ROOT).as_posix(), getattr(owner, "name", "<module>")))
    return sites


def test_every_registry_post_site_is_reviewed():
    assert _registry_post_sites() == REVIEWED, (
        "A /registry/heartbeat or /registry/register call site was added or moved. "
        "Decide what it does in a Light run pod (workspace_mode.in_run_pod()) and add "
        "it to REVIEWED with that decision."
    )


def test_the_sweep_detects_a_new_site(tmp_path, monkeypatch):
    """Negative control: the analyzer must see a hand-written registry POST."""
    fake_root = tmp_path / "pkg"
    fake_root.mkdir()
    (fake_root / "rogue.py").write_text(
        "async def beat(client, platform_url):\n"
        "    await client.post(f'{platform_url}/registry/heartbeat', json={})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(__import__(__name__), "PKG_ROOT", fake_root)
    assert _registry_post_sites() == {("rogue.py", "beat")}


# --- C3b: the activity POST a run makes carries its run id --------------------


@pytest.mark.asyncio
async def test_activity_post_carries_run_id_in_run_pod(run_pod, monkeypatch):
    http = await _report_activity(monkeypatch)
    assert http.calls[0][2].get("X-Molecule-Run-Id") == RUN_ID
