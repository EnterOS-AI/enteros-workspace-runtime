#!/usr/bin/env python3
"""Propagate the just-published runtime version to consumer templates (runtime#91).

``molecule-ai-workspace-runtime`` is the SSOT for ``molecule_runtime``. Each
consumer template pins ``.runtime-version`` (reproducible builds need an explicit
version, never ``latest``). On every ``runtime-v*`` release the pins drift until a
human hand-bumps them, leaving re-provisioned workspaces on a stale runtime.

This script closes that loop: for each consumer template whose ``.runtime-version``
is behind the released version, it opens a PR bumping the pin. Templates that also
pin the runtime in ``requirements.txt`` (e.g., codex-style templates) get BOTH
files bumped atomically so publish-image's cross-check stays green.

It does NOT merge — each template's normal CI + 1-approval gate still applies;
the automation removes the discovery + hand-authoring toil, not the human review.

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
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ORG = "molecule-ai"

# SSOT for the set of template repos that pin .runtime-version and therefore get
# an auto-bump PR on every release.
#
# runtime#83/#91 BUG: this list used to be a HAND-MAINTAINED 4-template subset
# (claude-code, hermes, openclaw, codex) while the consumer-drift guard then
# covered six templates plus molecule-core. The two lists silently diverged:
# google-adk/crewai were active pin consumers but the propagation bot never
# opened their bump PRs, so runtime ``main`` stayed red as releases outpaced
# those pins. Those two templates are now retired and explicitly exempted by the
# guard; deriving this list still prevents the active sets from diverging again.
#
# FIX: derive TEMPLATE_CONSUMERS from the guard's DEFAULT_CONSUMERS so the
# propagate set can never again be narrower than the set the guard enforces.
# We take every DEFAULT_CONSUMERS entry that is a ``*-workspace-template-*`` repo
# (i.e. carries a .runtime-version pin) and is not EXEMPT. molecule-core is
# excluded by construction: it installs the wheel but carries no .runtime-version
# pin (not a ``-template-`` repo), so there is nothing to bump. A consumer that is
# behind but has no .runtime-version file is handled at runtime by plan_consumer
# ("no-pin" → skipped), so over-inclusion is safe.
try:  # normal import when run from the repo (scripts/ on sys.path)
    from check_consumer_runtime_drift import (
        DEFAULT_CONSUMERS as _GUARD_CONSUMERS,
        EXEMPT_CONSUMERS as _GUARD_EXEMPT,
    )
except ImportError:  # pragma: no cover - allow running this file by absolute path
    import importlib.util as _ilu
    import pathlib as _pl

    _spec = _ilu.spec_from_file_location(
        "check_consumer_runtime_drift",
        _pl.Path(__file__).resolve().parent / "check_consumer_runtime_drift.py",
    )
    _mod = _ilu.module_from_spec(_spec)
    # Register before exec so @dataclass(frozen=True) inside the module can
    # resolve cls.__module__ in sys.modules (else AttributeError on exec).
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    _GUARD_CONSUMERS = _mod.DEFAULT_CONSUMERS
    _GUARD_EXEMPT = _mod.EXEMPT_CONSUMERS

TEMPLATE_CONSUMERS = tuple(
    repo
    for repo in _GUARD_CONSUMERS
    if "-workspace-template-" in repo and repo not in _GUARD_EXEMPT
)

# Regex for the runtime pin line in requirements.txt. Matches lines like:
#   molecules-workspace-runtime==0.3.84     (canonical dist name)
#   molecule-ai-workspace-runtime==0.3.26   (legacy pre-rename name)
#
# RC 2026-07-05: the dist was renamed `molecule-ai-workspace-runtime` ->
# `molecules-workspace-runtime` (dependency-confusion fix, 2026-07-03) but
# this regex still matched only the LEGACY name, so dual-pin templates
# (codex, google-adk) kept getting `.runtime-version`-only bump PRs while
# their requirements.txt stayed frozen on versions the registry retention
# had already purged (0.3.75 / 0.3.70) — template validation then failed on
# `No matching distribution found`, the bump PRs could never merge, and the
# consumer-drift gate on runtime main went red. Both names stay matched so
# any straggler template still on the legacy name keeps receiving atomic
# dual-pin bumps.
RUNTIME_PIN_RE = re.compile(
    r"^((?:molecules-workspace-runtime|molecule-ai-workspace-runtime)==)"
    r"([0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.-]*))",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ConsumerPlan:
    repo: str
    pinned: str | None
    action: str  # "open-pr" | "already-pinned" | "pr-exists" | "ahead" | "no-pin"
    branch: str
    detail: str
    req_pin: str | None = None  # requirements.txt pin, if present


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
    # CF edge 403s the default urllib UA (error 1010); send a curl UA so
    # /raw/ reads (requirements.txt, .runtime-version) succeed.
    req.add_header("User-Agent", "curl/8.4.0")
    if token:
        req.add_header("Authorization", f"token {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# Transient HTTP failures worth retrying. 5xx are server-side and may recover;
# connection errors / timeouts are network blips the next attempt often clears.
# 4xx are client errors and are NOT retried — a 401/403/404/422 won't fix itself.
_RETRIABLE_5XX = frozenset({500, 502, 503, 504})


def _http_with_retry(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    sleep: "callable | None" = None,
) -> tuple[int, str]:
    """Same contract as ``_http`` plus bounded retry/backoff for transient failures.

    runtime#52 (audit 2026-05-24, medium-severity finding): a single transient
    Gitea/network blip on the PR POST marks a template failed even though the
    branch + file writes already succeeded upstream. This helper retries
    5xx + connection errors / timeouts with exponential backoff (1s, 2s, 4s by
    default — caller passes a custom ``sleep`` in tests for instant replay).

    4xx responses are returned on the first attempt (no retry) because they
    indicate a client-side problem (auth, schema, not-found) that retrying
    cannot fix.

    ``max_retries`` is the ADDITIONAL attempts after the first call. ``max_retries=3``
    means up to 4 total HTTP calls before raising the final error.

    The ``sleep`` parameter resolves to ``time.sleep`` at call time (not at
    function-definition time) so tests can patch ``time.sleep`` and have
    ``_http_with_retry`` pick the patch up. Production callers leave it as
    ``None`` and get the real ``time.sleep``.
    """
    if sleep is None:
        sleep = time.sleep
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            status, body = _http(
                url, token=token, method=method, payload=payload, timeout=timeout
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            sleep(2 ** attempt)
            continue
        if status in _RETRIABLE_5XX and attempt < max_retries:
            sleep(2 ** attempt)
            continue
        return status, body
    # Unreachable: the loop above either returns or raises on the last attempt.
    assert last_exc is not None  # pragma: no cover
    raise last_exc


def read_pinned_version(repo: str, *, gitea_url: str, token: str | None = None) -> str | None:
    """Read a consumer's .runtime-version. None if the file is absent."""
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/raw/.runtime-version"
    status, body = _http(url, token=token)
    if status == 200:
        return body.strip()
    if status == 404:
        return None
    raise RuntimeError(f"{repo}: unexpected HTTP {status} reading .runtime-version: {body[:200]}")


def read_requirements_pin(repo: str, *, gitea_url: str, token: str | None = None) -> str | None:
    """Read a consumer's requirements.txt runtime pin, if any.

    Returns the pinned version string (e.g. "0.3.84") if a
    ``molecules-workspace-runtime==<ver>`` (or legacy
    ``molecule-ai-workspace-runtime==<ver>``) line exists, else None.
    Returns None on 404 (no requirements.txt).
    """
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/raw/requirements.txt"
    status, body = _http(url, token=token)
    if status == 200:
        match = RUNTIME_PIN_RE.search(body)
        return match.group(2) if match else None
    if status == 404:
        return None
    raise RuntimeError(f"{repo}: unexpected HTTP {status} reading requirements.txt: {body[:200]}")


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

    req_pin = read_requirements_pin(repo, gitea_url=gitea_url, token=token)
    detail = f"would bump .runtime-version {pinned} -> {target}"
    if req_pin:
        detail += f"; requirements.txt pin {req_pin} -> {target}"

    # Behind: would open a PR. Check idempotency only when we can authenticate
    # (the branch/PR list endpoints need the token for these repos).
    if token:
        if _branch_exists(repo, branch, gitea_url=gitea_url, token=token):
            return ConsumerPlan(repo, pinned, "pr-exists", branch, f"branch {branch} already exists", req_pin=req_pin)
        existing = _open_pr_for_branch(repo, branch, gitea_url=gitea_url, token=token)
        if existing:
            return ConsumerPlan(repo, pinned, "pr-exists", branch, f"open PR already exists: {existing}", req_pin=req_pin)

    return ConsumerPlan(repo, pinned, "open-pr", branch, detail, req_pin=req_pin)


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


def _get_file_sha(repo: str, path: str, base: str, *, gitea_url: str, token: str) -> str | None:
    url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/contents/{path}?ref={base}"
    status, body = _http(url, token=token)
    if status == 200:
        try:
            return json.loads(body).get("sha")
        except json.JSONDecodeError:
            return None
    return None


def _commit_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    *,
    branch: str,
    base: str,
    create_branch: bool,
    gitea_url: str,
    token: str,
) -> None:
    """Write one file to a branch via the Gitea contents API.

    If ``create_branch`` is True, the commit is made on ``base`` and ``branch``
    is created. Otherwise the commit is made on the existing ``branch``.
    """
    sha = _get_file_sha(repo, path, base if create_branch else branch, gitea_url=gitea_url, token=token)
    if sha is None and not create_branch:
        # File may not exist on the bump branch yet; try base.
        sha = _get_file_sha(repo, path, base, gitea_url=gitea_url, token=token)

    content_b64 = base64.b64encode(content.encode()).decode()
    put_url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/contents/{path}"
    put_payload: dict = {
        "branch": base if create_branch else branch,
        "content": content_b64,
        "message": message,
    }
    if create_branch:
        put_payload["new_branch"] = branch
    if sha is not None:
        put_payload["sha"] = sha

    status, body = _http(put_url, token=token, method="PUT", payload=put_payload)
    if status not in (200, 201):
        raise RuntimeError(f"{repo}: failed to write {path} (HTTP {status}): {body[:300]}")


def _update_requirements_content(content: str, target: str) -> str | None:
    """Return requirements.txt content with the runtime pin bumped to target.

    Returns None if no runtime pin is present (nothing to update).
    """
    def repl(match: re.Match) -> str:
        return f"{match.group(1)}{target}"

    new_content, n = RUNTIME_PIN_RE.subn(repl, content)
    return new_content if n > 0 else None


def open_bump_pr(plan: ConsumerPlan, target: str, *, gitea_url: str, token: str) -> str:
    """Create branch + commit the .runtime-version bump (+ requirements.txt if
    dual-pinned) + open a PR. Returns html_url.

    Uses the Gitea contents + pulls API only (no git clone), so no token ever
    lands in a clone URL on disk.
    """
    repo = plan.repo
    base = _get_default_branch(repo, gitea_url=gitea_url, token=token)

    # 1. Commit .runtime-version bump; this creates the branch.
    _commit_file(
        repo,
        ".runtime-version",
        f"{target}\n",
        f"chore(runtime): bump .runtime-version to {target}",
        branch=plan.branch,
        base=base,
        create_branch=True,
        gitea_url=gitea_url,
        token=token,
    )

    # 2. If requirements.txt also pins the runtime, bump it on the same branch.
    updated_paths = [".runtime-version"]
    if plan.req_pin:
        req_url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/raw/requirements.txt?ref={base}"
        status, req_body = _http(req_url, token=token)
        if status == 200:
            new_req = _update_requirements_content(req_body, target)
            if new_req is not None and new_req != req_body:
                _commit_file(
                    repo,
                    "requirements.txt",
                    new_req,
                    f"chore(runtime): bump requirements.txt runtime pin to {target}",
                    branch=plan.branch,
                    base=base,
                    create_branch=False,
                    gitea_url=gitea_url,
                    token=token,
                )
                updated_paths.append("requirements.txt")

    title = f"chore(runtime): bump .runtime-version to {target}"
    files_clause = " and ".join(f"`{p}`" for p in updated_paths)
    body_md = (
        f"Automated runtime SSOT propagation from "
        f"`molecule-ai-workspace-runtime` release `runtime-v{target}` (runtime#91).\n\n"
        f"Bumps {files_clause} so re-provisioned workspaces pick up the new runtime wheel.\n\n"
        f"This PR runs this template's normal CI and requires the normal approval — "
        f"a human still gates the merge. Close it if this template is intentionally "
        f"held back; `consumer-drift` will then flag it as an intentional pin."
    )
    pr_url = f"{gitea_url}/api/v1/repos/{ORG}/{repo}/pulls"
    pr_payload = {"base": base, "head": plan.branch, "title": title, "body": body_md}
    # runtime#52: PR POST is the most transient-prone step — a 503/504/network
    # blip after the branch + file writes have already succeeded would orphan
    # the bump branch with no PR. Wrap with bounded retry/backoff so a single
    # blip does not cascade into a manual open.
    status, body = _http_with_retry(
        pr_url, token=token, method="POST", payload=pr_payload
    )
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
