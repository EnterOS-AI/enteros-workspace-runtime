from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_platform_comm_contract.py"
SPEC = importlib.util.spec_from_file_location("check_platform_comm_contract", SCRIPT_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


def _write_sdk_client(repo: Path, *, source_url: bool = True, target_body: bool = True, register_headers: bool = True) -> None:
    client = repo / "molecule_agent" / "client.py"
    client.parent.mkdir(parents=True)
    delegate_url = (
        'f"{self.platform_url}/workspaces/{self.workspace_id}/delegate"'
        if source_url
        else 'f"{self.platform_url}/workspaces/{target_id}/delegate"'
    )
    target_entry = '"target_id": target_id,' if target_body else '"source_id": self.workspace_id,'
    headers_entry = "headers=self._auth_headers()," if register_headers else ""
    client.write_text(
        f"""
class RemoteAgentClient:
    def register(self):
        return self._session.post(
            f"{{self.platform_url}}/registry/register",
            json={{"id": self.workspace_id}},
            {headers_entry}
        )

    def delegate(self, task, target_id):
        return self._session.post(
            {delegate_url},
            json={{{target_entry} "task": task}},
            headers=self._auth_headers(),
        )
""".lstrip()
    )


def _write_runtime_delegation(repo: Path, *, uses_resolver: bool = True, imports_platform_url: bool = False) -> None:
    module = repo / "molecule_runtime" / "a2a_tools_delegation.py"
    module.parent.mkdir(parents=True)
    import_name = "PLATFORM_URL, _resolve_platform_url" if imports_platform_url else "_resolve_platform_url"
    base_expr = "_resolve_platform_url(src)" if uses_resolver else "PLATFORM_URL"
    module.write_text(
        f"""
from molecule_runtime.a2a_client import {import_name}

async def _delegate_sync_via_polling(workspace_id, task, src):
    base = {base_expr}
    return f"{{base}}/workspaces/{{src}}/delegate"

async def tool_delegate_task_async(workspace_id, task, source_workspace_id=None):
    src = source_workspace_id or "self"
    base = {base_expr}
    return f"{{base}}/workspaces/{{src}}/delegate"

async def tool_check_task_status(workspace_id, task_id, source_workspace_id=None):
    src = source_workspace_id or "self"
    base = {base_expr}
    return f"{{base}}/workspaces/{{src}}/delegations"
""".lstrip()
    )


def test_sdk_contract_allows_source_workspace_url_and_target_body(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-sdk"
    _write_sdk_client(repo)

    assert guard.find_platform_comm_drift("molecule-ai-sdk", repo) == []


def test_sdk_contract_rejects_delegate_target_url_drift(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-sdk"
    _write_sdk_client(repo, source_url=False)

    findings = guard.find_platform_comm_drift("molecule-ai-sdk", repo)

    assert [(f.path, f.reason) for f in findings] == [
        (
            "molecule_agent/client.py",
            "delegate() must POST to source workspace URL and send target_id in JSON body",
        )
    ]


def test_sdk_contract_rejects_missing_register_routing_headers(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-sdk"
    _write_sdk_client(repo, register_headers=False)

    findings = guard.find_platform_comm_drift("molecule-ai-sdk", repo)

    assert [(f.path, f.reason) for f in findings] == [
        (
            "molecule_agent/client.py",
            "register() must call /registry/register with self._auth_headers()",
        )
    ]


def test_runtime_contract_allows_per_workspace_platform_url_resolver(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-workspace-runtime"
    _write_runtime_delegation(repo)

    assert guard.find_platform_comm_drift("molecule-ai-workspace-runtime", repo) == []


def test_current_runtime_delegation_satisfies_contract() -> None:
    repo = Path(__file__).resolve().parents[1]

    assert guard.find_platform_comm_drift("molecule-ai-workspace-runtime", repo) == []


def test_runtime_contract_rejects_module_platform_url_drift(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-workspace-runtime"
    _write_runtime_delegation(repo, uses_resolver=False, imports_platform_url=True)

    findings = guard.find_platform_comm_drift("molecule-ai-workspace-runtime", repo)

    assert (
        guard.ContractFinding(
            "molecule-ai-workspace-runtime",
            "molecule_runtime/a2a_tools_delegation.py",
            "durable delegation must not import module-level PLATFORM_URL",
        )
        in findings
    )
    assert (
        guard.ContractFinding(
            "molecule-ai-workspace-runtime",
            "molecule_runtime/a2a_tools_delegation.py",
            "_delegate_sync_via_polling must call _resolve_platform_url(src)",
        )
        in findings
    )
