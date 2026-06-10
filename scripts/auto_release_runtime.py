#!/usr/bin/env python3
"""Auto-bump + tag the next runtime release on a green `main` (runtime: auto-release).

CTO standing directive (2026-06-10): a green merge to ``main`` must AUTO-bump the
version and publish to prod — no manual tag / approval gate. This script is the
"bump" half: it is invoked by ``.gitea/workflows/auto-release.yml`` ONLY AFTER that
workflow has re-run the merge-blocking gates (``unit-tests`` + ``responsiveness-e2e``)
inline and they are green (Gitea has no ``workflow_run`` trigger, so the release
workflow cannot listen on the ``ci`` workflow's success — it re-runs the gate itself).

What it does, all via the Gitea HTTP API (no git clone, so the token never lands in
an on-disk clone URL — mirrors scripts/propagate_runtime_version.py):

  1. Compute the NEXT patch version from the latest ``runtime-v*`` tag
     (e.g. 0.3.13 -> 0.3.14).
  2. Commit a ``pyproject.toml`` ``[project].version`` bump to that version directly
     onto ``main`` via the contents API, message tagged ``[skip-bump]`` (loop guard).
  3. Create the annotated/lightweight tag ``runtime-v<next>`` pointing at that new
     commit. The tag push trips the EXISTING publish-runtime.yml, whose
     ``pyproject==tag`` invariant now holds because step 2 made pyproject match.

Idempotent / safe:
  * If ``main`` HEAD already carries a ``[skip-bump]`` release commit (actor==bot or
    message guard) the caller skips us entirely; we additionally refuse to act if the
    pyproject version already equals the computed target (re-run safety).
  * If the target tag already exists we exit 0 (someone/something already released it).
"""

from __future__ import annotations

import argparse
import base64
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


def get_pyproject(base, token, ref):
    url = f"{_api(base)}/contents/pyproject.toml?ref={ref}"
    status, body = _http(url, token=token)
    if status != 200:
        raise RuntimeError(f"read pyproject failed HTTP {status}: {body[:200]}")
    obj = json.loads(body)
    content = base64.b64decode(obj["content"]).decode()
    return obj["sha"], content


def bump_pyproject_text(text, target):
    # Only the [project] version line (the build-system block has no `version`).
    new, n = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]+"',
        rf'\g<1>"{target}"',
        text,
        count=1,
    )
    if n != 1:
        raise RuntimeError("could not locate a single version = \"...\" line in pyproject.toml")
    return new


def commit_bump(base, token, *, branch, file_sha, new_content, target):
    url = f"{_api(base)}/contents/pyproject.toml"
    payload = {
        "branch": branch,
        "sha": file_sha,
        "content": base64.b64encode(new_content.encode()).decode(),
        # [skip-bump] is the loop guard: auto-release.yml refuses to act when
        # HEAD's message carries it, so this commit cannot retrigger a bump.
        "message": f"chore(release): bump runtime to {target} [skip-bump]",
    }
    status, body = _http(url, token=token, method="PUT", payload=payload)
    if status not in (200, 201):
        raise RuntimeError(f"commit bump failed HTTP {status}: {body[:300]}")
    return json.loads(body)["commit"]["sha"]


def create_tag(base, token, *, tag, commit_sha, target):
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
                   help="Compute + print the plan without mutating anything (no token needed).")
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
        print("::error::no token available to read tags/pyproject", file=sys.stderr)
        return 1

    tag_name, ver = latest_release_tag(base, read_token)
    target = next_patch(ver)
    next_tag = f"{TAG_PREFIX}{target}"
    print(f"latest release tag: {tag_name} -> next: {next_tag}")

    if tag_exists(base, read_token, next_tag):
        print(f"::notice::{next_tag} already exists — nothing to release")
        return 0

    file_sha, text = get_pyproject(base, read_token, args.branch)
    cur = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    cur_ver = cur.group(1) if cur else "?"
    print(f"pyproject [project].version on {args.branch}: {cur_ver}")

    if cur_ver == target:
        # pyproject already bumped but tag missing (a half-done prior run): just tag.
        print(f"::notice::pyproject already at {target}; creating the missing tag only")
        if args.dry_run:
            print(f"DRY-RUN: would create tag {next_tag} at {args.branch} HEAD")
            return 0
        # tag the current HEAD of branch
        st, bd = _http(f"{_api(base)}/branches/{args.branch}", token=token)
        head_sha = json.loads(bd)["commit"]["id"]
        create_tag(base, token, tag=next_tag, commit_sha=head_sha, target=target)
        print(f"::notice::created tag {next_tag} at {head_sha[:9]}")
        return 0

    new_text = bump_pyproject_text(text, target)
    if args.dry_run:
        print(f"DRY-RUN: would bump pyproject {cur_ver} -> {target} on {args.branch}, "
              f"then create tag {next_tag}")
        return 0

    commit_sha = commit_bump(base, token, branch=args.branch, file_sha=file_sha,
                             new_content=new_text, target=target)
    print(f"::notice::committed bump {cur_ver} -> {target} on {args.branch} @ {commit_sha[:9]}")
    create_tag(base, token, tag=next_tag, commit_sha=commit_sha, target=target)
    print(f"::notice::created tag {next_tag} @ {commit_sha[:9]} — publish-runtime will fire")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
