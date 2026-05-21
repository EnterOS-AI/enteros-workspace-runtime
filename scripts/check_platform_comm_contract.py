#!/usr/bin/env python3
"""Fail if agent clients drift from the platform communication contract.

This is intentionally a small static guard. Runtime and SDK unit tests still
prove behavior, but this catches the class of cross-repo drift where a consumer
renames the caller/target shape or bypasses the per-workspace platform URL
resolver before an E2E run notices.
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit


DEFAULT_REPOS = (
    "molecule-ai-workspace-runtime",
    "molecule-sdk-python",
)


@dataclass(frozen=True)
class ContractFinding:
    repo: str
    path: str
    reason: str


def _parse_python(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _string_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return parts
    return []


def _has_path(node: ast.AST, *needles: str) -> bool:
    text = "".join(_string_parts(node))
    return all(needle in text for needle in needles)


def _is_self_attr(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _find_class(module: ast.Module, name: str) -> ast.ClassDef | None:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _find_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _dict_value_for_key(node: ast.AST, key_name: str) -> ast.AST | None:
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == key_name:
            return value
    return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _calls_self_auth_headers(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_auth_headers"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def check_sdk_client(repo_path: Path) -> list[ContractFinding]:
    rel_path = Path("molecule_agent/client.py")
    path = repo_path / rel_path
    if not path.exists():
        return [
            ContractFinding(
                repo="molecule-sdk-python",
                path=rel_path.as_posix(),
                reason="missing SDK client module",
            )
        ]

    module = _parse_python(path)
    cls = _find_class(module, "RemoteAgentClient")
    if cls is None:
        return [
            ContractFinding(
                repo="molecule-sdk-python",
                path=rel_path.as_posix(),
                reason="RemoteAgentClient class not found",
            )
        ]

    findings: list[ContractFinding] = []
    register = _find_method(cls, "register")
    if register is None:
        findings.append(
            ContractFinding("molecule-sdk-python", rel_path.as_posix(), "register() not found")
        )
    elif not _register_sends_auth_headers(register):
        findings.append(
            ContractFinding(
                "molecule-sdk-python",
                rel_path.as_posix(),
                "register() must call /registry/register with self._auth_headers()",
            )
        )

    delegate = _find_method(cls, "delegate")
    if delegate is None:
        findings.append(
            ContractFinding("molecule-sdk-python", rel_path.as_posix(), "delegate() not found")
        )
    elif not _delegate_uses_source_url_and_target_body(delegate):
        findings.append(
            ContractFinding(
                "molecule-sdk-python",
                rel_path.as_posix(),
                "delegate() must POST to source workspace URL and send target_id in JSON body",
            )
        )
    return findings


def _register_sends_auth_headers(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        if not _call_name(node.func).lower().endswith("post"):
            continue
        if not node.args or not _has_path(node.args[0], "/registry/register"):
            continue
        headers = _keyword(node, "headers")
        if headers and _calls_self_auth_headers(headers):
            return True
    return False


def _delegate_uses_source_url_and_target_body(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        if not _call_name(node.func).lower().endswith("post"):
            continue
        if not node.args or not _has_path(node.args[0], "/workspaces/", "/delegate"):
            continue
        url_has_source = any(_is_self_attr(part, "workspace_id") for part in ast.walk(node.args[0]))
        json_body = _keyword(node, "json")
        target_value = _dict_value_for_key(json_body, "target_id") if json_body else None
        body_has_target = isinstance(target_value, ast.Name) and target_value.id == "target_id"
        if url_has_source and body_has_target:
            return True
    return False


def check_runtime_delegation(repo_path: Path) -> list[ContractFinding]:
    rel_path = Path("molecule_runtime/a2a_tools_delegation.py")
    path = repo_path / rel_path
    if not path.exists():
        return [
            ContractFinding(
                repo="molecule-ai-workspace-runtime",
                path=rel_path.as_posix(),
                reason="missing runtime delegation module",
            )
        ]

    module = _parse_python(path)
    findings: list[ContractFinding] = []
    imported_platform_url = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "molecule_runtime.a2a_client"
        and any(alias.name == "PLATFORM_URL" for alias in node.names)
        for node in module.body
    )
    if imported_platform_url:
        findings.append(
            ContractFinding(
                "molecule-ai-workspace-runtime",
                rel_path.as_posix(),
                "durable delegation must not import module-level PLATFORM_URL",
            )
        )

    if any(isinstance(node, ast.Name) and node.id == "PLATFORM_URL" for node in ast.walk(module)):
        findings.append(
            ContractFinding(
                "molecule-ai-workspace-runtime",
                rel_path.as_posix(),
                "durable delegation must resolve URLs through _resolve_platform_url(src)",
            )
        )

    for name in ("_delegate_sync_via_polling", "tool_delegate_task_async", "tool_check_task_status"):
        fn = next(
            (
                node
                for node in module.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
            ),
            None,
        )
        if fn is None or not _function_calls_resolver_with_src(fn):
            findings.append(
                ContractFinding(
                    "molecule-ai-workspace-runtime",
                    rel_path.as_posix(),
                    f"{name} must call _resolve_platform_url(src)",
                )
            )

    return findings


def _function_calls_resolver_with_src(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) != "_resolve_platform_url":
            continue
        if len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == "src":
            return True
    return False


def find_platform_comm_drift(repo_name: str, repo_path: Path) -> list[ContractFinding]:
    if repo_name == "molecule-ai-workspace-runtime":
        return check_runtime_delegation(repo_path)
    if repo_name == "molecule-sdk-python":
        return check_sdk_client(repo_path)
    return []


def clone_repos(workdir: Path, repos: tuple[str, ...], *, gitea_url: str, token: str) -> dict[str, Path]:
    if not token:
        raise RuntimeError("GITEA_TOKEN is required when --root is not provided")

    parsed_url = urlsplit(gitea_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError(f"invalid Gitea URL: {gitea_url}")

    safe_token = quote(token, safe="")
    base_url = f"{parsed_url.scheme}://x-access-token:{safe_token}@{parsed_url.netloc}"
    paths: dict[str, Path] = {}
    for repo in repos:
        dest = workdir / repo
        result = subprocess.run(
            ["git", "clone", "--depth", "1", f"{base_url}/molecule-ai/{repo}.git", str(dest)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.replace(token, "<redacted>").replace(safe_token, "<redacted>")
            raise RuntimeError(f"failed to clone {repo}: {stderr.strip()}")
        paths[repo] = dest
    return paths


def repo_paths_from_root(root: Path, repos: tuple[str, ...]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    missing: list[str] = []
    for repo in repos:
        path = root / repo
        if path.is_dir():
            paths[repo] = path
        else:
            missing.append(repo)
    if missing:
        raise RuntimeError(f"missing checkout(s) under {root}: {', '.join(missing)}")
    return paths


def format_findings(findings: list[ContractFinding]) -> str:
    lines = ["Platform communication contract drift detected:"]
    for finding in findings:
        lines.append(f"- {finding.repo}:{finding.path} - {finding.reason}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="Directory containing checked-out repos; skips cloning when set.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Repo to check. May be repeated. Defaults to canonical communication clients.",
    )
    parser.add_argument(
        "--gitea-url",
        default=os.environ.get("GITEA_URL", "https://git.moleculesai.app"),
        help="Gitea base URL used for cloning when --root is omitted.",
    )
    parser.add_argument(
        "--token-env",
        default="GITEA_TOKEN",
        help="Environment variable containing a read token for cloning.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repos = tuple(args.repos or DEFAULT_REPOS)
    tempdir: Path | None = None

    try:
        if args.root:
            paths = repo_paths_from_root(args.root, repos)
        else:
            tempdir = Path(tempfile.mkdtemp(prefix="platform-comm-contract-"))
            paths = clone_repos(
                tempdir,
                repos,
                gitea_url=args.gitea_url,
                token=os.environ.get(args.token_env, ""),
            )

        findings: list[ContractFinding] = []
        for repo, path in paths.items():
            findings.extend(find_platform_comm_drift(repo, path))
        if findings:
            print(format_findings(findings), file=sys.stderr)
            return 1

        print(f"Platform communication contract guard passed for {len(paths)} repo(s).")
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
