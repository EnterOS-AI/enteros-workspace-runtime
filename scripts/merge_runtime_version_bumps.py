#!/usr/bin/env python3
"""Auto-merge propagated `.runtime-version` bump PRs (runtime#131).

`scripts/propagate_runtime_version.py` opens a bump PR on every consumer
template repo whenever the runtime version is bumped. Each PR requires
1 approval on the template repo — but nothing automates the approval,
so the bumps pile up unmerged and the consumer-drift check on runtime
main goes red until someone hand-merges them.

This script closes the loop: for each consumer template, find open
PRs authored by the OPENER identity (resolved dynamically as the account
behind the read/DISPATCH token — the same Infisical /shared/runtime-bot
credential propagate_runtime_version.py opens the PRs with) whose diff
touches ONLY `.runtime-version` (and optionally `requirements.txt` for
templates that pin there too — propagate bumps both atomically), and the
combined commit status is `success`. Approve as the (distinct) merge
identity (if not already) and merge. Close any superseded older-version
bump PRs to prevent downgrades (e.g. three stacked bumps → close the two
older ones once the latest lands; the system holds at the latest only).

Tightly scoped:
  - only PRs authored by the opener identity (whoami of the read token;
    never hardcoded — see the identity-drift RC note below)
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

# NOTE (RC 2026-07-05, identity drift): the expected bump-PR author is NOT
# hardcoded any more. It is resolved at runtime as `whoami(read_token)` — by
# construction the same identity the propagate script opens the PRs with,
# because both workflows feed the same Infisical /shared/runtime-bot
# credential. The previous hardcode ("molecule-runtime-release-bot") went
# stale when the credential was consolidated onto `core-devops`: the sweeper
# matched zero PRs and no-opped green while consumer pins drifted 5+
# releases. The merge identity is likewise resolved dynamically and the
# sweeper refuses to run if opener == merger (non-author contract,
# runtime#131).

# Required commit-status gate for a bump PR, matched by SUBSTRING against
# the posted context names. Exact names went stale immediately: the old
# tuple ("validate-runtime", "t4-conformance") never matched the names the
# template CIs actually emit ("CI / Template validation (runtime)
# (pull_request)", "CI / T4 tier-4 conformance (live) (push)", ...), so
# even an eligible PR would have been skipped forever (RC 2026-07-05).
#
# Semantics: the combined status must be `success` (Gitea computes it over
# the latest status per context, so ANY posted failure/pending — including
# T4 conformance wherever a template defines it — blocks) AND at least one
# posted-success context must contain each substring below. The substring
# anchor guards the just-pushed window where CI has not registered yet
# (zero/few statuses could otherwise read as mergeable). "Template
# validation (runtime)" is the universal wheel-install gate every consumer
# template defines — the core risk surface of a pin bump; T4 conformance is
# not anchored because not every template has a T4 lane (crewai), but where
# it exists the combined state already enforces it.
REQUIRED_STATUS_SUBSTRINGS: tuple[str, ...] = (
    "Template validation (runtime)",
)


class GiteaClient:
    """Thin Gitea API client for the consumer-repos + this repo.

    Two distinct token identities are used (runtime#131):
      - `merge_token` (default: `CONSUMER_BUMP_MERGE_TOKEN`): the
        non-author identity that performs approve+merge+close on the
        consumer template repos. Must be a non-author of the bump PR;
        run() resolves BOTH identities via whoami() and refuses to run
        when they coincide, so the contract holds regardless of which
        accounts the credentials are rotated onto. If absent the sweeper
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
        anonymous: bool = False,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        token = self.merge_token if use_merge_token else self.read_token
        headers = {
            "Accept": "application/json",
            # CF edge 403s the default python-urllib UA (error 1010); send a
            # curl UA so calls through the CF-fronted git.moleculesai.app
            # succeed. Same workaround as propagate_runtime_version.py:133 and
            # check_consumer_runtime_drift.py:137 — the sweeper was the ONLY
            # one of the three scripts missing it, which 1010-blocked its very
            # first live API call (GET /user) the moment the tokens were
            # finally provisioned (2026-07-05).
            "User-Agent": "curl/8.4.0",
        }
        # Anonymous calls deliberately omit Authorization entirely: Gitea
        # rejects an out-of-scope token before falling back to public access,
        # so sending it turns a 200 into a 403 (see user_exists).
        if not anonymous:
            headers["Authorization"] = f"token {token}"
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

    def user_exists(self, login: str) -> bool:
        """False only when `login` is DEFINITIVELY not an account (404).

        Two things had to be got right here, both verified against live Gitea
        on 2026-07-28 rather than assumed:

        1. The call must be ANONYMOUS. `GET /users/{login}` is a public
           endpoint, but presenting an out-of-scope token makes Gitea reject
           on scope BEFORE it ever falls back to anonymous access:
               GET /users/molecule-runtime-release-bot
                 with the write:repository-only token -> 403
                 {"message":"token does not have at least one of required
                  scope(s), required=[read:user], token scope=write:repository"}
                 with no Authorization header            -> 200
           Sending the token here would make this guard reject EVERY login and
           re-brick the sweeper — the exact failure it exists to prevent. (Same
           edge the issue #311 liveness probe documents.)

        2. It fails OPEN. Anything that is not a clean 404 — a private
           instance that hides users, a network blip, an edge 5xx — means "I
           could not check", not "this account is fake". Refusing on an
           inconclusive answer would trade a silent no-op for a noisy one. The
           real safety net is the drift alarm in run(); this is only here to
           catch the typo class of error early.
        """
        status, _ = self._request(
            "GET", f"/api/v1/users/{urllib.parse.quote(login, safe='')}", anonymous=True
        )
        return status != 404

    def whoami(self, *, use_merge_token: bool = False) -> str:
        """Login of the identity behind the chosen token; '' on failure.

        Used to resolve the EXPECTED bump-PR author dynamically (the opener
        identity = whoever holds the read/DISPATCH token, i.e. the same
        Infisical /shared/runtime-bot credential the propagate script opens
        the PRs with) and to enforce opener != merger (the runtime#131
        non-author contract) without hardcoding usernames that silently
        drift when the credential is rotated to a different account
        (RC 2026-07-05: RUNTIME_BOT_TOKEN was consolidated onto
        `core-devops`, the sweeper still filtered on
        `molecule-runtime-release-bot`, matched zero PRs, and no-opped
        green while consumer pins drifted 5+ releases).
        """
        status, payload = self._request(
            "GET", "/api/v1/user", use_merge_token=use_merge_token
        )
        if status != 200 or not isinstance(payload, dict):
            return ""
        login = payload.get("login")
        return login if isinstance(login, str) else ""

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
        # the approve). Self-approval is impossible because run()
        # verifies whoami(read_token) != whoami(merge_token) before any
        # mutation — the guard travels with the credentials, not with
        # hardcoded usernames.
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
    expected_author: str,
) -> bool:
    """True iff the PR is a propagation bump — authored by the opener
    identity (resolved dynamically as whoami(read_token); see the identity
    note above) and the only files changed are .runtime-version
    (+ optionally requirements.txt for dual-pin templates)."""
    if not isinstance(pr, dict):
        return False
    if not expected_author:
        # Never fall back to "any author": an unresolved opener identity
        # must have been caught in run() before we get here.
        return False
    user = pr.get("user") or {}
    if not isinstance(user, dict):
        return False
    if user.get("login") != expected_author:
        return False
    if not files:
        return False
    return all(f in ALLOWED_BUMP_FILES for f in files)


def all_required_statuses_success(
    combined: dict[str, Any],
    required_substrings: tuple[str, ...] = REQUIRED_STATUS_SUBSTRINGS,
) -> bool:
    """True iff the head is genuinely mergeable status-wise:

    1. the combined state is `success` — Gitea computes it over the latest
       status per context, so any posted failure/error/pending context
       (T4 conformance included, wherever a template defines one) blocks;
    2. for each required substring, at least one posted-SUCCESS context
       name contains it — guarding the just-pushed window where CI has not
       registered its contexts yet and the combined state alone could read
       as mergeable with the real gates simply absent.
    """
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
    return all(
        any(sub in ctx for ctx in posted) for sub in required_substrings
    )


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


def version_key(version: str) -> tuple[int, ...]:
    """Comparable key for a `0.4.55`-style pin.

    Factored out of the sort lambda because the regression guard now COMPARES
    versions rather than just ordering them, so the two must not drift apart.

    Non-numeric segments are dropped, matching the propagate script's
    numeric-only version contract. Note this deliberately keeps the original
    lambda's semantics — `0.4.55` -> (0, 4, 55) — rather than inventing a
    richer semver parse the rest of the pipeline doesn't share.
    """
    return tuple(int(x) for x in version.strip().split(".") if x.isdigit())


def resolve_identity(
    client: "GiteaClient",
    *,
    configured: str,
    token_env: str,
    login_env: str,
    role: str,
    use_merge_token: bool,
) -> tuple[str, str]:
    """Resolve a token's account login. Returns (login, source).

    Issue #339. The original design resolved this ONLY via `whoami()` and
    refused to run otherwise, deliberately, to avoid the 2026-07-05 failure
    where a HARDCODED author went stale and the sweeper no-opped green while
    consumer pins drifted 5+ releases.

    That is the right instinct but it collided with issue #311's fix: the
    release-bot token was reissued `write:repository`-ONLY (no `read:user`)
    on purpose, so `GET /user` is now *structurally* guaranteed to 401. The
    sweeper has therefore died on every scheduled run since — the fleet
    froze at 0.4.35, then 0.4.38, and each unblock has been manual.

    So: prefer `whoami()` (authoritative — it reflects the credential
    actually in use, and keeps working if the token later regains the
    scope), and fall back to an explicit CONFIGURED login only when whoami
    is unavailable. Config is a named env var, not a constant buried in the
    source, and a wrong value can no longer no-op silently — see the
    drift alarm in run(), which fails LOUD when bump-shaped PRs exist whose
    author doesn't match. That alarm is what makes the fallback safe; it
    is strictly stronger than the pre-#339 behaviour, which only caught
    drift when whoami worked at all.
    """
    live = client.whoami(use_merge_token=use_merge_token)
    configured = configured.strip()
    if live:
        if configured and configured != live:
            # Not fatal: the live token is authoritative. But a stale
            # override is exactly how the 2026-07-05 drift started, so say so.
            print(
                f"::warning::{login_env}={configured!r} disagrees with the live "
                f"{role} identity {live!r} resolved from {token_env}; using the live "
                f"value and ignoring the override. Update or unset {login_env}."
            )
        return live, "whoami"
    if not configured:
        return "", "unresolved"
    # Validate the override names a real account. `/users/{login}` is public,
    # so this works with the write-only token — it catches the typo class of
    # error at startup rather than as a silent zero-match sweep.
    if not client.user_exists(configured):
        print(
            f"::error::{login_env}={configured!r} is not a known Gitea account "
            f"(GET /users/{configured} did not return 200); refusing to sweep "
            f"against an identity that cannot have opened anything.",
            file=sys.stderr,
        )
        return "", "invalid"
    print(
        f"::notice::{role} identity resolved from {login_env}={configured!r} "
        f"(whoami via {token_env} unavailable — expected while that token is "
        f"write:repository-only per issue #311)."
    )
    return configured, login_env


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("GITEA_HOST", "https://git.moleculesai.app"))
    parser.add_argument("--owner", default="molecule-ai")
    parser.add_argument("--merge-token-env", default="CONSUMER_BUMP_MERGE_TOKEN",
                        help="Env var holding the non-author approve+merge identity (runtime#131's contract).")
    parser.add_argument("--read-token-env", default="DISPATCH_TOKEN",
                        help="Env var holding the read-only token (default DISPATCH_TOKEN; used to list PRs, fetch files, check status, read the per-repo opt-out file).")
    parser.add_argument("--opener-login-env", default="BUMP_OPENER_LOGIN",
                        help="Env var holding the EXPECTED bump-PR author login, used only when whoami() "
                             "is unavailable because the read token is write:repository-only (issue #339).")
    parser.add_argument("--merge-login-env", default="BUMP_MERGE_LOGIN",
                        help="Env var holding the approve+merge identity login, used only when whoami() "
                             "is unavailable for the merge token.")
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

    # Resolve BOTH identities dynamically (see the identity-drift note at the
    # top): the opener identity is whatever account holds the read/DISPATCH
    # token — the exact same credential the propagate script opens the bump
    # PRs with — and the merge identity is whatever account holds the merge
    # token. Fail LOUD (non-zero) if either cannot be resolved or if they
    # coincide: a sweeper that guesses the author or can self-approve is
    # worse than a delayed sweep.
    expected_author, opener_src = resolve_identity(
        client,
        configured=os.environ.get(args.opener_login_env, ""),
        token_env=args.read_token_env,
        login_env=args.opener_login_env,
        role="opener",
        use_merge_token=False,
    )
    if not expected_author:
        print(
            f"::error::cannot resolve the opener identity: GET /user with "
            f"{args.read_token_env} failed (the release-bot token is "
            f"write:repository-only BY DESIGN — see issue #311's least-privilege "
            f"fix — so whoami is structurally unavailable) and {args.opener_login_env} "
            f"is unset. Set {args.opener_login_env} to the account that opens the "
            f"bump PRs; refusing to guess the bump-PR author.",
            file=sys.stderr,
        )
        return 2
    merge_identity, merge_src = ("", "dry-run")
    if not (args.dry_run and not merge_token):
        merge_identity, merge_src = resolve_identity(
            client,
            configured=os.environ.get(args.merge_login_env, ""),
            token_env=args.merge_token_env,
            login_env=args.merge_login_env,
            role="merger",
            use_merge_token=True,
        )
    if not args.dry_run:
        if not merge_identity:
            print(
                f"::error::cannot resolve the merge identity (GET /user with "
                f"{args.merge_token_env} failed and {args.merge_login_env} is unset); "
                f"refusing to approve+merge blind.",
                file=sys.stderr,
            )
            return 2
        if merge_identity == expected_author:
            print(
                f"::error::opener and merger resolve to the SAME identity "
                f"({expected_author!r}) — the runtime#131 non-author "
                f"approve+merge contract is violated; fix the token wiring "
                f"({args.read_token_env} vs {args.merge_token_env}).",
                file=sys.stderr,
            )
            return 2

    repos = tuple(args.repos) if args.repos else CONSUMER_TEMPLATE_REPOS
    print(
        f"runtime#131 sweep: {len(repos)} consumer repos, "
        f"mode={'DRY-RUN' if args.dry_run else 'MERGE'}, "
        f"opener={expected_author!r} (via {opener_src}), "
        f"merger={merge_identity or '<dry-run>'!r} (via {merge_src})"
    )

    total_merged = 0
    total_closed = 0
    total_skipped = 0
    rc = 0
    # Issue #339 drift alarm. Authors seen on PRs that are bump-SHAPED (they
    # touch only ALLOWED_BUMP_FILES) but whose author != expected_author. If
    # this ends up non-empty while nothing matched, the configured/resolved
    # opener is wrong and the sweep is a silent no-op — the precise 2026-07-05
    # failure. We turn that into a red run instead of a green one.
    unmatched_authors: set[str] = set()
    matched_any = False

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
            if not is_runtime_bump_pr(pr, files, expected_author):
                # Bump-SHAPED but authored by someone else → record for the
                # drift alarm below. File shape alone is never enough to act
                # on; it is only enough to notice that we might be filtering
                # on the wrong identity.
                if files and all(f in ALLOWED_BUMP_FILES for f in files):
                    other = ((pr.get("user") or {}).get("login") or "?") if isinstance(pr.get("user"), dict) else "?"
                    if other != expected_author:
                        unmatched_authors.add(other)
                continue
            matched_any = True
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
        bump_prs.sort(key=lambda t: version_key(t[0]), reverse=True)

        # Never merge a bump that is not STRICTLY newer than what main already
        # pins. Discovered live 2026-07-28: main was at 0.4.55 (PR #357 had
        # been merged by hand during an outage unblock) while the newest OPEN
        # bump was 0.4.54, so "merge the latest open bump" meant merging a
        # REGRESSION of the production runtime pin.
        #
        # Today that merge happens to fail on a git conflict — but a conflict
        # is luck, not a safety property: one rebase of the branch resolves it
        # in the PR's favour and the pin silently goes backwards. Worse, the
        # unconditional attempt also *wedged* the sweeper: it picked the stale
        # PR, failed to merge, `continue`d, and so never closed the superseded
        # ones — no progress, forever, even with the identity fix in place.
        #
        # So: compare against main and treat anything <= current as superseded
        # (close it), leaving only strictly-newer bumps eligible to merge.
        current_pin = (client.get_file(repo, ".runtime-version") or "").strip()
        if current_pin:
            newer = [b for b in bump_prs if version_key(b[0]) > version_key(current_pin)]
            superseded = [b for b in bump_prs if version_key(b[0]) <= version_key(current_pin)]
            if superseded:
                print(
                    f"  {repo}: main pins {current_pin}; "
                    f"{len(superseded)} bump PR(s) at or below it are superseded"
                )
                for old_version, old_pr, _b in superseded:
                    if args.dry_run:
                        print(f"    DRY-RUN: would close superseded PR #{old_pr} ({old_version} <= {current_pin})")
                        total_skipped += 1
                        continue
                    close_status = client.close_pr(repo, old_pr)
                    if close_status in (200, 201, 204):
                        print(f"    closed superseded PR #{old_pr} ({old_version} <= main's {current_pin})")
                        total_closed += 1
                    else:
                        print(f"    close PR #{old_pr} failed (HTTP {close_status}); will retry next sweep")
            bump_prs = newer
            if not bump_prs:
                print(f"  {repo}: no bump newer than {current_pin} — up to date")
                continue
        else:
            # Couldn't read the pin (new repo, transient). Fall back to the
            # old behaviour rather than blocking the sweep; the merge itself
            # still gates on all-green status.
            print(f"  {repo}: ::warning::could not read .runtime-version on main; skipping the regression guard")

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

        # Approve if not already approved by the merge identity.
        already_approved = any(
            isinstance(r, dict)
            and r.get("user", {}).get("login") == merge_identity
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

    # Issue #339 drift alarm. If we matched NOTHING anywhere yet there are
    # open PRs that look exactly like propagation bumps by file shape, the
    # identity we filtered on is almost certainly wrong. Historically this
    # combination produced a GREEN no-op run while every consumer template
    # silently fell behind by releases at a time. Make it red and name both
    # sides so the fix is a one-liner rather than an investigation.
    if not matched_any and unmatched_authors:
        print(
            f"::error::sweep matched ZERO bump PRs as {expected_author!r} "
            f"(resolved via {opener_src}), but found bump-shaped PRs authored by "
            f"{sorted(unmatched_authors)!r}. The opener identity is stale or "
            f"misconfigured — set {args.opener_login_env} to the correct account. "
            f"Failing loudly: a silent no-op here is what let consumer pins drift "
            f"5+ releases on 2026-07-05 and again on 2026-07-20/24.",
            file=sys.stderr,
        )
        rc = 2

    print(f"runtime#131 sweep complete: merged={total_merged} closed={total_closed} skipped={total_skipped} mode={'DRY-RUN' if args.dry_run else 'MERGE'}")
    return rc


if __name__ == "__main__":
    sys.exit(run())
