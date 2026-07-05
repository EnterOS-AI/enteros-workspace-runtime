#!/usr/bin/env python3
"""Auto-merge propagated `.runtime-version` bump PRs (runtime#131).

`scripts/propagate_runtime_version.py` opens a bump PR on every consumer
template repo whenever the runtime version is bumped. Each PR requires
1 approval on the template repo — but nothing automates the approval,
so the bumps pile up unmerged and the consumer-drift check on runtime
main goes red until someone hand-merges them.

This script closes the loop: for each consumer template, find open
PRs authored by `molecule-runtime-release-bot` whose diff touches ONLY
`.runtime-version` (and optionally `requirements.txt` for templates that
pin there too — propagate_runtime_version.py bumps both atomically), and
the combined commit status is `success`. Approve as `devops-engineer`
(if not already) and merge. Close any superseded older-version bump PRs
to prevent downgrades (e.g. three stacked bumps → close the two older
ones once the latest lands; the system holds at the latest only).

Tightly scoped:
  - only `molecule-runtime-release-bot` (the identity the propagate
    script uses, per scripts/auto_release_runtime.py)
  - only `.runtime-version` (and optionally `requirements.txt` for the
    dual-pin templates)
  - only all-green commit status
  - opt-out via `--dry-run` (report only) and via a per-repo file flag
    `runtime-merge-bumps: false` in `.gitea/REPO.yaml` (operator knob)

Read-only when `--dry-run` (default in CI before green-flag). Mutable
when the bot identity + commit-status gate pass. Idempotent: re-running
on an already-merged PR is a no-op (the PR is no longer open).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Hardcoded SSOT for the consumer list — same source-of-truth as
# scripts/check_consumer_runtime_drift.py's DEFAULT_CONSUMERS + EXEMPT,
# derived from a single chain so the two stay in sync. Adding a consumer
# to either list requires updating this tuple + the drift script.
#
# We duplicate the list (rather than `import`) because propagate is a
# standalone CLI that the bot runs from a one-shot container; sharing a
# module across repos would force the runtime repo to publish a Python
# package. Sync drift is caught by `reconcile_org_consumers` in the
# drift check.
CONSUMER_TEMPLATE_REPOS: tuple[str, ...] = (
    "molecule-ai-workspace-template-claude-code",
    "molecule-ai-workspace-template-hermes",
    "molecule-ai-workspace-template-openclaw",
    "molecule-ai-workspace-template-codex",
    "molecule-ai-workspace-template-google-adk",
    "molecule-ai-workspace-template-crewai",
)

# Files that count as "runtime-version bump only" — the propagate script
# atomically bumps `.runtime-version` and (for dual-pin templates like
# codex) `requirements.txt` together. A PR touching ONLY these files is
# safe to auto-merge; any other file touched → skip (it's not a
# propagation bump, the bot wasn't the only author of intent, or the
# bump was hand-edited to add unrelated noise).
ALLOWED_BUMP_FILES: frozenset[str] = frozenset({
    ".runtime-version",
    "requirements.txt",
})

# Bot identity that the propagate script uses to open the PR. The
# devops-engineer approves on its behalf (server-side; the bot can't
# approve its own PR on any of the consumer repos that require
# 1 approval).
BOT_AUTHOR_USERNAME = "molecule-runtime-release-bot"

# The reviewer identity that will approve + merge. Must be:
#   1. a non-author identity (per Gitea self-approval policy on every
#      template repo that requires 1 approval)
#   2. a member of the repo's required-reviewers team (the same
#      devops-engineer identity that the sweeper uses for force-merge
#      cleanup; same credentials throughout the runtime fleet)
REVIEWER_USERNAME = "devops-engineer"

# Required commit status contexts — both must be `success` to be
# mergeable. The propagate script's bump PRs are deliberately tiny
# (single-file pin bumps) so the test surface is small; these two
# gates cover the main risk surfaces.
REQUIRED_STATUS_CONTEXTS: tuple[str, ...] = (
    "validate-runtime",
    "t4-conformance",
)


class GiteaClient:
    """Thin Gitea API client for the consumer-repos + this repo.

    Two distinct token identities are used (runtime#131):
      - `merge_token` (default: `CONSUMER_BUMP_MERGE_TOKEN`): the
        non-author identity that performs approve+merge+close on the
        consumer template repos. Must be a non-author of the bump PR
        (the bot opens them as `molecule-runtime-release-bot`, so
        `devops-engineer` is the natural choice). If absent the sweeper
        exits 0 with a loud warning (no green-light for ops to notice).
      - `read_token` (default: `DISPATCH_TOKEN`): used for read-only
        API calls (listing open PRs, fetching files, combined status,
        opt-out file). The opener script (propagate_runtime_version.py)
        uses this same identity to write the bump PRs in the first
        place; the sweeper is read-only on it.

    Both identities are scoped to the runtime fleet per runtime#83.
    The script keeps them strictly separated so an absent merge token
    never falls back to the opener (which would self-approve).
    """

    def __init__(
        self,
        base_url: str,
        merge_token: str,
        read_token: str,
        owner: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.merge_token = merge_token
        self.read_token = read_token
        self.owner = owner

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        *,
        use_merge_token: bool = False,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        token = self.merge_token if use_merge_token else self.read_token
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/json",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, None
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return exc.code, raw

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        *,
        use_merge_token: bool = False,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        token = self.merge_token if use_merge_token else self.read_token
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/json",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, None
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return exc.code, raw

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """All open PRs on a repo, newest first. Pages through the default
        Gitea pagination (50/page) and returns the flat list."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            status, payload = self._request(
                "GET",
                f"/api/v1/repos/{self.owner}/{repo}/pulls",
                params={"state": "open", "limit": "50", "page": str(page)},
            )
            if status != 200 or not isinstance(payload, list):
                return out
            out.extend(payload)
            if len(payload) < 50:
                return out
            page += 1

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        """Filenames changed in a PR. Paginates Gitea (50/page)."""
        out: list[str] = []
        page = 1
        while True:
            status, payload = self._request(
                "GET",
                f"/api/v1/repos/{self.owner}/{repo}/pulls/{pr_number}/files",
                params={"limit": "50", "page": str(page)},
            )
            if status != 200 or not isinstance(payload, list):
                return out
            for entry in payload:
                if isinstance(entry, dict) and "filename" in entry:
                    out.append(entry["filename"])
            if len(payload) < 50:
                return out
            page += 1

    def combined_status(self, repo: str, sha: str) -> dict[str, Any]:
        status, payload = self._request(
            "GET", f"/api/v1/repos/{self.owner}/{repo}/commits/{sha}/status"
        )
        if status != 200:
            return {"state": "unknown", "statuses": []}
        if not isinstance(payload, dict):
            return {"state": "unknown", "statuses": []}
        return payload

    def approve_pr(self, repo: str, pr_number: int) -> int:
        # Approve uses the merge token (the non-author identity doing
        # the approve). Self-approval is impossible because the bot
        # author is `molecule-runtime-release-bot` and the merge token
        # belongs to a different identity (typically devops-engineer).
        status, _ = self._request(
            "POST",
            f"/api/v1/repos/{self.owner}/{repo}/pulls/{pr_number}/reviews",
            body={"event": "APPROVED", "body": "auto-approve runtime#131: bot-authored .runtime-version bump; all-green commit status; non-author merge identity."},
            use_merge_token=True,
        )
        return status

    def merge_pr(self, repo: str, pr_number: int, merge_method: str = "merge") -> int:
        # Gitea PR merge: Do = merge commit, Rebase = rebase, Squash =
        # squash. Template repos default to "merge" so we keep that.
        # Uses the merge token (must have write on the consumer repo).
        status, _ = self._request(
            "POST",
            f"/api/v1/repos/{self.owner}/{repo}/pulls/{pr_number}/merge",
            body={"Do": merge_method},
            use_merge_token=True,
        )
        return status

    def close_pr(self, repo: str, pr_number: int) -> int:
        # Close uses the merge token (write scope on the consumer repo).
        status, _ = self._request(
            "PATCH",
            f"/api/v1/repos/{self.owner}/{repo}/pulls/{pr_number}",
            body={"state": "closed"},
            use_merge_token=True,
        )
        return status

    def get_file(self, repo: str, path: str, ref: str = "main") -> str | None:
        # get_file is a read-only operation; uses the read token
        # (which is the DISPATCH_TOKEN / opener identity, sufficient
        # for the per-repo opt-out file in `.gitea/REPO.yaml`).
        status, payload = self._request(
            "GET",
            f"/api/v1/repos/{self.owner}/{repo}/contents/{urllib.parse.quote(path, safe='/')}",
            params={"ref": ref},
        )
        if status != 200 or not isinstance(payload, dict):
            return None
        import base64

        try:
            return base64.b64decode(payload.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None


def is_runtime_bump_pr(
    pr: dict[str, Any],
    files: list[str],
) -> bool:
    """True iff the PR is a propagation bump — bot-authored and the
    only files changed are .runtime-version (+ optionally requirements.txt
    for dual-pin templates)."""
    if not isinstance(pr, dict):
        return False
    user = pr.get("user") or {}
    if not isinstance(user, dict):
        return False
    if user.get("login") != BOT_AUTHOR_USERNAME:
        return False
    if not files:
        return False
    return all(f in ALLOWED_BUMP_FILES for f in files)


def all_required_statuses_success(
    combined: dict[str, Any],
    required: tuple[str, ...] = REQUIRED_STATUS_CONTEXTS,
) -> bool:
    """True iff every context in `required` posted success on the PR's
    head. The combined-state being 'success' is necessary but not
    sufficient — a required context may be `pending` and the combined
    is still `success` if the only failures are unrelated contexts."""
    if combined.get("state") != "success":
        return False
    posted: set[str] = set()
    for s in combined.get("statuses", []):
        if not isinstance(s, dict):
            continue
        # Gitea's combined-status CHILD rows carry the per-context result in
        # `status` (the top-level combined uses `state`, read above). Reading
        # only `state` here made every child evaluate false against real Gitea
        # payloads, so the sweeper skipped genuinely-green PRs (RC 13421).
        # Accept both for robustness across payload shapes.
        if (s.get("status") or s.get("state")) == "success":
            posted.add(s.get("context", ""))
    return all(ctx in posted for ctx in required)


def is_repo_opted_in(repo: str, client: GiteaClient) -> bool:
    """Per-repo opt-out via `.gitea/REPO.yaml` (or `runtime-merge-bumps.yaml`).
    Defaults to opted-in. The flag is `runtime-merge-bumps: false` at the
    root of the YAML file. The file is optional; absence = opted-in."""
    for path in (".gitea/REPO.yaml", ".gitea/runtime-merge-bumps.yaml"):
        raw = client.get_file(repo, path, ref="main")
        if raw is None:
            continue
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Naive key match: `runtime-merge-bumps: <value>` on a single line.
            # We don't need full YAML parsing — the format is fixed and
            # one-liner; the operator action is documented as a single
            # boolean field. This keeps the dep surface to stdlib only.
            if ":" in stripped and not stripped.startswith("-"):
                key, _, value = stripped.partition(":")
                if key.strip() == "runtime-merge-bumps":
                    return value.strip().lower() in ("true", "yes", "1", "on")
    return True


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("GITEA_HOST", "https://git.moleculesai.app"))
    parser.add_argument("--owner", default="molecule-ai")
    parser.add_argument("--merge-token-env", default="CONSUMER_BUMP_MERGE_TOKEN",
                        help="Env var holding the non-author approve+merge identity (runtime#131's contract).")
    parser.add_argument("--read-token-env", default="DISPATCH_TOKEN",
                        help="Env var holding the read-only token (default DISPATCH_TOKEN; used to list PRs, fetch files, check status, read the per-repo opt-out file).")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the would-merge PRs + would-close PRs without mutating.")
    parser.add_argument("--repos", nargs="*", default=None,
                        help="Restrict to a subset of the consumer template repos (default: all).")
    args = parser.parse_args()

    # runtime#131 RC 13418: GITEA_HOST is documented as the FULL API
    # base URL (with scheme). If a workflow forgets the scheme (just
    # "git.moleculesai.app"), normalize by prepending "https://" so
    # the first real API call doesn't fail with `ValueError: unknown
    # url type`. We DON'T fail-closed here — that would mask a real
    # workflow YAML bug as a runtime regression (and the next workflow
    # edit would silently re-introduce the bare host). Instead, log a
    # ::warning:: so the next operator notices, and proceed.
    if not args.base_url.startswith(("http://", "https://")):
        print(f"::warning::GITEA_HOST is missing URL scheme ({args.base_url!r}); prepending https://. Fix the workflow env to use a full URL (RC 13418).")
        args.base_url = "https://" + args.base_url

    merge_token = os.environ.get(args.merge_token_env, "")
    read_token = os.environ.get(args.read_token_env, "")

    # Gate on the merge token ONLY. If absent, the sweeper cannot
    # approve+merge anything, so exit 0 with a loud warning rather
    # than painting runtime main red. This is the runtime#83 pattern
    # (config gap, not runtime regression).
    if not merge_token and not args.dry_run:
        print(f"::warning::{args.merge_token_env} is not set; skipping consumer runtime-version bump merge sweeper. Provision a non-author identity with write on the consumer template repos (runtime#131's contract) and re-run, or pass --dry-run.")
        return 0
    # The read token is required for the per-PR pre-flight (list
    # open PRs, fetch files, check status, read the opt-out file).
    # If absent, fail-closed: dry-run still works for diagnosis, but
    # a real merge sweep can't proceed.
    if not read_token:
        print(f"::error::{args.read_token_env} is not set; cannot list PRs / check status. Provision the read token and re-run, or pass --dry-run to skip pre-flight checks.", file=sys.stderr)
        return 2

    client = GiteaClient(args.base_url, merge_token, read_token, args.owner)

    repos = tuple(args.repos) if args.repos else CONSUMER_TEMPLATE_REPOS
    print(f"runtime#131 sweep: {len(repos)} consumer repos, mode={'DRY-RUN' if args.dry_run else 'MERGE'}")

    total_merged = 0
    total_closed = 0
    total_skipped = 0
    rc = 0

    for repo in repos:
        if not is_repo_opted_in(repo, client):
            print(f"  {repo}: opted-out via .gitea/REPO.yaml — skip")
            total_skipped += 1
            continue

        prs = client.list_open_prs(repo)
        if not prs:
            continue

        # First pass: collect Bump PRs grouped by version (so we can close
        # the older ones once the latest lands). The propagate script
        # uses the version as part of the branch name (`bump-runtime-0.3.23`
        # or similar); we extract the version from the .runtime-version
        # in the diff via the file endpoint OR the branch name. Cheaper:
        # use the branch name (no extra API call).
        bump_prs: list[tuple[str, int, str]] = []  # (version, pr_number, branch)
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            pr_number = pr.get("number")
            branch = (pr.get("head") or {}).get("ref", "") or ""
            if not isinstance(pr_number, int):
                continue
            files = client.get_pr_files(repo, pr_number)
            if not is_runtime_bump_pr(pr, files):
                continue
            # Extract version from branch name. The propagate script's
            # actual branch grammar (per scripts/propagate_runtime_version.py)
            # is `bump/runtime-{target}` (with a slash); we also accept
            # the historical `bump-runtime-{target}` / `bump-runtime-v{target}`
            # forms so older bumps that pre-date the slash grammar still
            # get auto-merged. Order matters — try the most-specific
            # prefix first.
            version = ""
            for prefix in ("bump/runtime-v", "bump/runtime-", "bump-runtime-v", "bump-runtime-"):
                if branch.startswith(prefix):
                    version = branch[len(prefix):]
                    break
            if not version:
                # Fallback: title like "chore: bump molecule-ai-workspace-runtime to 0.3.23"
                title = pr.get("title", "")
                if "to " in title:
                    version = title.rsplit("to ", 1)[-1].strip()
            if not version:
                # Last resort: skip rather than guess. The script is
                # tightly scoped — we'd rather miss one than auto-merge
                # an unrelated PR.
                continue
            bump_prs.append((version, pr_number, branch))

        if not bump_prs:
            continue

        # Sort newest version first (semver-style tuple sort works
        # for the 0.3.21 / 0.3.22 / 0.3.23 cases the propagate
        # script emits; non-numeric suffixes sort lexicographically but
        # the propagate script's version-string contract is numeric
        # segments only).
        bump_prs.sort(key=lambda t: tuple(int(x) for x in t[0].split(".") if x.isdigit()), reverse=True)

        latest_version, latest_pr, latest_branch = bump_prs[0]
        print(f"  {repo}: latest bump = {latest_version} (PR #{latest_pr}, branch {latest_branch!r})")

        if args.dry_run:
            print(f"    DRY-RUN: would approve+merge PR #{latest_pr} and close {len(bump_prs) - 1} older bump(s)")
            total_skipped += len(bump_prs)
            continue

        # Verify commit status of the latest.
        sha = (client.list_open_prs(repo) or [])
        # Re-fetch in case state changed mid-pass; cheaper to just pull
        # the head sha from the latest PR we already saw.
        latest_pr_full = next(
            (p for p in client.list_open_prs(repo) if isinstance(p, dict) and p.get("number") == latest_pr),
            None,
        )
        if latest_pr_full is None:
            print(f"    PR #{latest_pr} no longer open; skip")
            continue
        head_sha = (latest_pr_full.get("head") or {}).get("sha", "")
        if not head_sha:
            print(f"    PR #{latest_pr} has no head.sha; skip")
            continue
        combined = client.combined_status(repo, head_sha)
        if not all_required_statuses_success(combined):
            statuses = [
                f"{s.get('context', '?')}={s.get('state', '?')}"
                for s in combined.get("statuses", [])
            ]
            print(f"    PR #{latest_pr} not all-green (combined={combined.get('state')}, statuses={statuses}); skip")
            total_skipped += 1
            continue

        # Approve if not already approved by the reviewer.
        already_approved = any(
            isinstance(r, dict)
            and r.get("user", {}).get("login") == REVIEWER_USERNAME
            and r.get("state") == "APPROVED"
            for r in (client._request("GET", f"/api/v1/repos/{args.owner}/{repo}/pulls/{latest_pr}/reviews") or ([], None))[1] or []
        )
        if not already_approved:
            approve_status = client.approve_pr(repo, latest_pr)
            if approve_status not in (200, 201, 204):
                print(f"    approve PR #{latest_pr} failed (HTTP {approve_status}); skip")
                total_skipped += 1
                continue

        # Merge.
        merge_status = client.merge_pr(repo, latest_pr)
        if merge_status not in (200, 201, 204):
            print(f"    merge PR #{latest_pr} failed (HTTP {merge_status}); skip")
            total_skipped += 1
            continue
        print(f"    merged PR #{latest_pr} ({latest_version})")
        total_merged += 1

        # Close the older superseded bumps.
        for older_version, older_pr, _older_branch in bump_prs[1:]:
            close_status = client.close_pr(repo, older_pr)
            if close_status in (200, 201, 204):
                print(f"    closed superseded PR #{older_pr} ({older_version})")
                total_closed += 1
            else:
                print(f"    close PR #{older_pr} failed (HTTP {close_status}); will retry next sweep")

    print(f"runtime#131 sweep complete: merged={total_merged} closed={total_closed} skipped={total_skipped} mode={'DRY-RUN' if args.dry_run else 'MERGE'}")
    return rc


if __name__ == "__main__":
    sys.exit(run())
