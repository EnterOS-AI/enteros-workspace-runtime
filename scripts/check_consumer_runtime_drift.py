#!/usr/bin/env python3
"""Fail if runtime consumers vendor editable runtime source.

The standalone molecule-ai-workspace-runtime repo is the SSOT for
``molecule_runtime``. Template repos and molecule-core may pin/install the
package, but they must not carry their own editable copy of the runtime package
or resurrect the old top-level ``workspace/`` runtime tree.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit


DEFAULT_CONSUMERS = (
    "molecule-ai-workspace-template-claude-code",
    "molecule-ai-workspace-template-hermes",
    "molecule-ai-workspace-template-openclaw",
    "molecule-ai-workspace-template-codex",
    "molecule-ai-workspace-template-langgraph",
    "molecule-ai-workspace-template-autogen",
    "molecule-core",
)

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class DriftFinding:
    repo: str
    path: str
    reason: str


def find_runtime_drift(repo_name: str, repo_path: Path) -> list[DriftFinding]:
    findings: list[DriftFinding] = []

    workspace_dir = repo_path / "workspace"
    if workspace_dir.is_dir():
        findings.append(
            DriftFinding(
                repo=repo_name,
                path="workspace/",
                reason="top-level workspace/ runtime tree is forbidden; use the runtime package",
            )
        )

    for root, dirs, _files in os.walk(repo_path):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        current = Path(root)
        for dirname in list(dirs):
            if dirname != "molecule_runtime":
                continue
            rel = (current / dirname).relative_to(repo_path).as_posix() + "/"
            findings.append(
                DriftFinding(
                    repo=repo_name,
                    path=rel,
                    reason="vendored molecule_runtime/ package is forbidden; import the SSOT package",
                )
            )
    return findings


def clone_consumers(
    workdir: Path,
    repos: tuple[str, ...],
    *,
    gitea_url: str,
    token: str,
) -> dict[str, Path]:
    if not token:
        raise RuntimeError("GITEA_TOKEN is required when --root is not provided")

    paths: dict[str, Path] = {}
    parsed_url = urlsplit(gitea_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError(f"invalid Gitea URL: {gitea_url}")
    safe_token = quote(token, safe="")
    base_url = f"{parsed_url.scheme}://x-access-token:{safe_token}@{parsed_url.netloc}"
    for repo in repos:
        dest = workdir / repo
        clone_url = f"{base_url}/molecule-ai/{repo}.git"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(dest)],
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


def consumer_paths_from_root(root: Path, repos: tuple[str, ...]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    missing: list[str] = []
    for repo in repos:
        path = root / repo
        if path.is_dir():
            paths[repo] = path
        else:
            missing.append(repo)
    if missing:
        raise RuntimeError(f"missing consumer checkout(s) under {root}: {', '.join(missing)}")
    return paths


def format_findings(findings: list[DriftFinding]) -> str:
    lines = ["Runtime SSOT drift detected:"]
    for finding in findings:
        lines.append(f"- {finding.repo}:{finding.path} - {finding.reason}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="Directory containing checked-out consumer repos; skips cloning when set.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Consumer repo to check. May be repeated. Defaults to all canonical consumers.",
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
    repos = tuple(args.repos or DEFAULT_CONSUMERS)

    tempdir: Path | None = None
    try:
        if args.root:
            paths = consumer_paths_from_root(args.root, repos)
        else:
            tempdir = Path(tempfile.mkdtemp(prefix="runtime-consumer-drift-"))
            paths = clone_consumers(
                tempdir,
                repos,
                gitea_url=args.gitea_url,
                token=os.environ.get(args.token_env, ""),
            )

        findings: list[DriftFinding] = []
        for repo, path in paths.items():
            findings.extend(find_runtime_drift(repo, path))

        if findings:
            print(format_findings(findings), file=sys.stderr)
            return 1

        print(f"Runtime SSOT drift guard passed for {len(paths)} consumer repo(s).")
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
