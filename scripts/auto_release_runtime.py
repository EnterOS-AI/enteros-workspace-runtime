#!/usr/bin/env python3
"""Tag the next runtime release on a green ``main`` (runtime: auto-release).

CTO standing directive (2026-06-10): a green merge to ``main`` must AUTO-release the
runtime and publish to prod — no manual ``runtime-v*`` tag / approval gate. This
script is the release trigger: invoked by ``.gitea/workflows/auto-release.yml`` ONLY
AFTER that workflow has re-run the merge-blocking gates (``unit-tests`` +
``responsiveness-e2e``) inline and they are green (Gitea has no ``workflow_run``
trigger, so the release workflow cannot listen on the ``ci`` workflow's success — it
re-runs the gate itself).

TAG-ONLY (root cause, 2026-06-10): ``main`` is strictly PR-only — branch protection
sets ``enable_push: false`` + ``required_approvals: 2``, so NO identity (not even the
release bot) may push a commit directly to ``main``. The previous design committed a
``pyproject.toml`` version bump to ``main`` via the contents API; that PUT is a direct
push to a protected branch and FAILED every run (~17s). So we DROP the bump-commit
entirely and only CUT THE TAG:

  1. Compute the NEXT patch version from the latest ``runtime-v*`` tag
     (e.g. 0.3.14 -> 0.3.15).
  2. Create the tag ``runtime-v<next>`` pointing at the current ``main`` HEAD via the
     Gitea tags API (``POST /repos/{owner}/{repo}/tags``). Tag creation targets the
     TAG ref, not the branch ref, so branch *push* protection does not block it
     (empirically proven: the manual ``runtime-v0.3.14`` cut returned HTTP 201 with
     this same release/devops token).

That tag push trips the EXISTING publish-runtime.yml, which now WRITES the tag's
version into ``pyproject.toml`` at build time (ephemeral, in-CI) before
``python -m build`` — so the built wheel carries the correct version WITHOUT a
pre-committed pyproject match. ``pyproject.toml`` on ``main`` therefore LAGS the tag
by design; this is cosmetic (the wheel version is correct by construction).

All API access is over the Gitea HTTP API (no git clone, so the token never lands in
an on-disk clone URL — mirrors scripts/propagate_runtime_version.py).

Idempotent / safe:
  * If the target tag already exists we exit 0 (something already released it).
  * No pyproject mutation, so there is no half-bumped state to recover from.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ORG = "molecule-ai"
REPO = "molecule-ai-workspace-runtime"
TAG_PREFIX = "runtime-v"


def _http(url, *, token, method="GET", payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _api(base):
    return f"{base}/api/v1/repos/{ORG}/{REPO}"


def latest_release_tag(base, token):
    """Return the highest runtime-v<semver> tag as a (tag, (maj,min,patch)) pair."""
    status, body = _http(f"{_api(base)}/tags?limit=100", token=token)
    if status != 200:
        raise RuntimeError(f"list tags failed HTTP {status}: {body[:200]}")
    best = None
    for t in json.loads(body):
        name = t.get("name", "")
        if not name.startswith(TAG_PREFIX):
            continue
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", name[len(TAG_PREFIX):])
        if not m:
            continue
        ver = tuple(int(x) for x in m.groups())
        if best is None or ver > best[1]:
            best = (name, ver)
    if best is None:
        raise RuntimeError(f"no {TAG_PREFIX}<semver> tags found")
    return best


def next_patch(ver):
    maj, minor, patch = ver
    return f"{maj}.{minor}.{patch + 1}"


def tag_exists(base, token, tag):
    status, _ = _http(f"{_api(base)}/tags/{tag}", token=token)
    return status == 200


def branch_head_sha(base, token, branch):
    status, body = _http(f"{_api(base)}/branches/{branch}", token=token)
    if status != 200:
        raise RuntimeError(f"read branch {branch} failed HTTP {status}: {body[:200]}")
    return json.loads(body)["commit"]["id"]


def create_tag(base, token, *, tag, commit_sha, target):
    """Cut runtime-v<target> at commit_sha via the tags API.

    This targets the TAG ref, NOT the protected branch ref, so branch push
    protection (enable_push:false + required_approvals) does not apply. Proven by
    the manual runtime-v0.3.14 cut (HTTP 201) with the same release-bot token.
    """
    url = f"{_api(base)}/tags"
    payload = {"tag_name": tag, "target": commit_sha,
               "message": f"runtime release {target} (auto)"}
    status, body = _http(url, token=token, method="POST", payload=payload)
    if status in (200, 201):
        return
    raise RuntimeError(f"create tag failed HTTP {status}: {body[:300]}")


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gitea-url", default=os.environ.get("GITEA_URL", "https://git.moleculesai.app"))
    p.add_argument("--token-env", default="RELEASE_BOT_TOKEN",
                   help="Env var holding the write token (molecule-runtime-release-bot).")
    p.add_argument("--branch", default="main")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute + print the plan without mutating anything.")
    args = p.parse_args(argv)

    base = args.gitea_url.rstrip("/")
    token = os.environ.get(args.token_env, "")
    if not args.dry_run and not token:
        print(f"::error::{args.token_env} is empty — cannot cut a release", file=sys.stderr)
        return 1
    # For dry-run we still need a token to call the API (private repo); fall back
    # to GITEA_TOKEN if the named one is absent so `--dry-run` works in CI logs.
    read_token = token or os.environ.get("GITEA_TOKEN", "")
    if not read_token:
        print("::error::no token available to read tags", file=sys.stderr)
        return 1

    tag_name, ver = latest_release_tag(base, read_token)
    target = next_patch(ver)
    next_tag = f"{TAG_PREFIX}{target}"
    print(f"latest release tag: {tag_name} -> next: {next_tag}")

    if tag_exists(base, read_token, next_tag):
        print(f"::notice::{next_tag} already exists — nothing to release")
        return 0

    head_sha = branch_head_sha(base, read_token, args.branch)
    print(f"{args.branch} HEAD: {head_sha[:9]}")

    if args.dry_run:
        print(f"DRY-RUN: would create tag {next_tag} at {args.branch} HEAD {head_sha[:9]}")
        return 0

    create_tag(base, token, tag=next_tag, commit_sha=head_sha, target=target)
    print(f"::notice::created tag {next_tag} at {head_sha[:9]} — publish-runtime will fire")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
