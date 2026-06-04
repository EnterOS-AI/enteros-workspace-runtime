#!/usr/bin/env python3
"""Propagate the just-published runtime version to consumer templates (runtime#91).

``molecule-ai-workspace-runtime`` is the SSOT for ``molecule_runtime``. Each
consumer template pins ``.runtime-version`` (reproducible builds need an explicit
version, never ``latest``). On every ``runtime-v*`` release the pins drift until a
human hand-bumps them, leaving re-provisioned workspaces on a stale runtime.

This script closes that loop: for each consumer template whose ``.runtime-version``
is behind the released version, it opens a PR bumping the pin. It does NOT merge —
each template's normal CI + 1-approval gate still applies; the automation removes
the discovery + hand-authoring toil, not the human review.

Idempotent: skips a consumer that is already pinned to the target, or that already
has the bump branch / an open bump PR.

Reads ``.runtime-version`` via the public raw endpoint (no token needed). Opening
PRs needs a token with ``write`` on the template repos: ``--token-env DISPATCH_TOKEN``
(see the operator action in runtime#83 — a dedicated ``molecule-runtime-release-bot``
identity, NOT a founder PAT). ``--dry-run`` computes + reports the plan without the
token and without mutating anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

# SSOT for the set of template repos that pin .runtime-version. This is the
# template subset of check_consumer_runtime_drift.DEFAULT_CONSUMERS (which also
# lists molecule-core — core installs the wheel but does not pin .runtime-version,
# so it is not a propagation target). langgraph/autogen templates also exist but
# are out of the runtime#91 scope; add them here when they adopt .runtime-version.
TEMPLATE_CONSUMERS = (
    "molecule-ai-workspace-template-claude-code",
    "molecule-ai-workspace-template-hermes",
    "molecule-ai-workspace-template-openclaw",
    "molecule-ai-workspace-template-codex",
)

ORG = "molecule-ai"


@dataclass(frozen=True)
class ConsumerPlan:
    repo: str
    pinned: str | None
    action: str  # "open-pr" | "already-pinned" | "pr-exists" | "ahead" | "no-pin"
    branch: str
    detail: str


def _http(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 30,
) -> tuple[int, str]:
    """Minimal HTTP helper. Returns (status, body). Never raises on HTTP error."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"token {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def read_pinned_version(repo: str, *, gitea_url: str, token: str | None = None) -> str | None:
    """Read a consumer's .runtime-version. None if the file is absent."""
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/raw/.runtime-version"
    status, body = _http(url, token=token)
    if status == 200:
        return body.strip()
    if status == 404:
        return None
    raise RuntimeError(f"{repo}: unexpected HTTP {status} reading .runtime-version: {body[:200]}")


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a release version into a comparable tuple. Pre-release suffixes are
    dropped to the numeric core (best-effort; pins are always plain releases)."""
    core = v.strip().split("-")[0].split("+")[0]
    parts = []
    for chunk in core.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def plan_consumer(repo: str, target: str, *, gitea_url: str, token: str | None = None) -> ConsumerPlan:
    branch = f"bump/runtime-{target}"
    pinned = read_pinned_version(repo, gitea_url=gitea_url, token=token)

    if pinned is None:
        return ConsumerPlan(repo, None, "no-pin", branch, "no .runtime-version file; skipping")
    if pinned == target:
        return ConsumerPlan(repo, pinned, "already-pinned", branch, f"already at {target}")
    if _version_tuple(pinned) > _version_tuple(target):
        return ConsumerPlan(
            repo, pinned, "ahead", branch,
            f"pinned {pinned} is ahead of release {target}; not downgrading",
        )

    # Behind: would open a PR. Check idempotency only when we can authenticate
    # (the branch/PR list endpoints need the token for these repos).
    if token:
        if _branch_exists(repo, branch, gitea_url=gitea_url, token=token):
            return ConsumerPlan(repo, pinned, "pr-exists", branch, f"branch {branch} already exists")
        existing = _open_pr_for_branch(repo, branch, gitea_url=gitea_url, token=token)
        if existing:
            return ConsumerPlan(repo, pinned, "pr-exists", branch, f"open PR already exists: {existing}")

    return ConsumerPlan(repo, pinned, "open-pr", branch, f"would bump {pinned} -> {target}")


def _branch_exists(repo: str, branch: str, *, gitea_url: str, token: str) -> bool:
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/branches/{branch}"
    status, _ = _http(url, token=token)
    return status == 200


def _open_pr_for_branch(repo: str, branch: str, *, gitea_url: str, token: str) -> str | None:
    """Return the html_url of an open PR whose head is `branch`, else None."""
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/pulls?state=open&limit=50"
    status, body = _http(url, token=token)
    if status != 200:
        return None
    try:
        for pr in json.loads(body):
            head = (pr.get("head") or {}).get("ref")
            if head == branch:
                return pr.get("html_url") or f"#{pr.get('number')}"
    except (json.JSONDecodeError, AttributeError):
        return None
    return None


def _get_default_branch(repo: str, *, gitea_url: str, token: str) -> str:
    status, body = _http(f"{gitea_url}/api/v1/repos/{ORG}/{repo}", token=token)
    if status == 200:
        try:
            return json.loads(body).get("default_branch") or "main"
        except json.JSONDecodeError:
            pass
    return "main"


def _get_file_sha(repo: str, base: str, *, gitea_url: str, token: str) -> str | None:
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/contents/.runtime-version?ref={base}"
    status, body = _http(url, token=token)
    if status == 200:
        try:
            return json.loads(body).get("sha")
        except json.JSONDecodeError:
            return None
    return None


def open_bump_pr(plan: ConsumerPlan, target: str, *, gitea_url: str, token: str) -> str:
    """Create branch + commit the .runtime-version bump + open a PR. Returns html_url.

    Uses the Gitea contents + pulls API only (no git clone), so no token ever
    lands in a clone URL on disk.
    """
    repo = plan.repo
    base = _get_default_branch(repo, gitea_url=gitea_url, token=token)
    sha = _get_file_sha(repo, base, gitea_url=gitea_url, token=token)
    if sha is None:
        raise RuntimeError(f"{repo}: could not read .runtime-version sha on {base}")

    # Commit the bump onto the new branch via the contents API (creates the branch).
    import base64

    content_b64 = base64.b64encode(f"{target}\n".encode()).decode()
    put_url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/contents/.runtime-version"
    put_payload = {
        "branch": plan.branch,
        "new_branch": plan.branch,
        "sha": sha,
        "content": content_b64,
        "message": f"chore(runtime): bump .runtime-version to {target}",
    }
    status, body = _http(put_url, token=token, method="PUT", payload=put_payload)
    if status not in (200, 201):
        raise RuntimeError(f"{repo}: failed to write bump commit (HTTP {status}): {body[:300]}")

    title = f"chore(runtime): bump .runtime-version to {target}"
    body_md = (
        f"Automated runtime SSOT propagation from "
        f"`molecule-ai-workspace-runtime` release `runtime-v{target}` (runtime#91).\n\n"
        f"Bumps `.runtime-version` `{plan.pinned}` -> `{target}` so re-provisioned "
        f"workspaces pick up the new runtime wheel.\n\n"
        f"This PR runs this template's normal CI and requires the normal approval — "
        f"a human still gates the merge. Close it if this template is intentionally "
        f"held back; `consumer-drift` will then flag it as an intentional pin."
    )
    pr_url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/pulls"
    pr_payload = {"base": base, "head": plan.branch, "title": title, "body": body_md}
    status, body = _http(pr_url, token=token, method="POST", payload=pr_payload)
    if status == 201:
        try:
            return json.loads(body).get("html_url", "(created)")
        except json.JSONDecodeError:
            return "(created)"
    if "pull request already exists" in body.lower():
        return "(already exists)"
    raise RuntimeError(f"{repo}: failed to open PR (HTTP {status}): {body[:300]}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="The just-published runtime version (e.g. 0.3.9).")
    parser.add_argument(
        "--repo", action="append", dest="repos",
        help="Consumer template repo to propagate to. Repeatable. Defaults to TEMPLATE_CONSUMERS.",
    )
    parser.add_argument(
        "--gitea-url", default=os.environ.get("GITEA_URL", "https://git.moleculesai.app"),
        help="Gitea base URL.",
    )
    parser.add_argument("--token-env", default="DISPATCH_TOKEN", help="Env var holding the write token.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute + print the plan without opening any PR (no token required).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    target = args.version.lstrip("v")
    if target.startswith("runtime-v"):
        target = target[len("runtime-v"):]
    repos = tuple(args.repos or TEMPLATE_CONSUMERS)
    token = os.environ.get(args.token_env, "").strip()

    if not args.dry_run and not token:
        # Graceful degradation: no token => report the plan as a notice, do not fail.
        print(
            f"::warning::{args.token_env} not set; runtime propagation runs in report-only mode "
            f"(no PRs opened). Provision the DISPATCH_TOKEN secret to enable auto-bump PRs (runtime#83).",
            file=sys.stderr,
        )
        args.dry_run = True

    plans: list[ConsumerPlan] = []
    for repo in repos:
        try:
            plans.append(plan_consumer(repo, target, gitea_url=args.gitea_url, token=token or None))
        except RuntimeError as exc:
            print(f"::warning::{exc}", file=sys.stderr)
            plans.append(ConsumerPlan(repo, None, "error", f"bump/runtime-{target}", str(exc)))

    opened: list[str] = []
    failures: list[str] = []
    for plan in plans:
        if plan.action == "open-pr" and not args.dry_run:
            try:
                url = open_bump_pr(plan, target, gitea_url=args.gitea_url, token=token)
                print(f"{plan.repo}: opened PR {url}")
                opened.append(f"{plan.repo}={url}")
            except RuntimeError as exc:
                print(f"::warning::{exc}", file=sys.stderr)
                failures.append(plan.repo)
        else:
            verb = "WOULD open PR" if (plan.action == "open-pr" and args.dry_run) else plan.action
            print(f"{plan.repo}: {verb} ({plan.detail})")

    print(
        f"\nruntime propagation -> {target}: "
        f"{len([p for p in plans if p.action == 'open-pr'])} behind, "
        f"{len([p for p in plans if p.action == 'already-pinned'])} current, "
        f"{len([p for p in plans if p.action == 'pr-exists'])} pending, "
        f"opened={len(opened)}, dry_run={args.dry_run}"
    )

    # Surfacing template drift is the WHOLE point; a behind-but-no-PR-yet state in
    # report-only mode is expected, not a failure. Only a genuine API failure while
    # actually opening PRs is an error.
    if failures:
        print(f"::error::failed to open bump PRs for: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
