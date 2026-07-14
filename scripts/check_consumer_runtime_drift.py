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
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


# SSOT for the set of repos that pin/install the runtime and MUST stay current
# with the latest published runtime-v<semver> tag. Every workspace template whose
# Dockerfile installs ``molecules-workspace-runtime==${RUNTIME_VERSION}`` (where
# RUNTIME_VERSION is read from its ``.runtime-version`` file) belongs here, plus
# molecule-core (installs the wheel; carries no .runtime-version pin but must not
# vendor the source). This list was previously only a hand-maintained subset of
# templates the runtime#91 propagation bot bumped + molecule-core. At the time,
# google-adk/crewai were active pin consumers but were omitted, so the guard stayed
# green while those pins drifted (16-26 releases behind). They are now retired and
# explicitly exempted below; ``reconcile_org_consumers`` still makes any future
# unclassified template LOUD.
DEFAULT_CONSUMERS = (
    "molecule-ai-workspace-template-claude-code",
    "molecule-ai-workspace-template-hermes",
    "molecule-ai-workspace-template-openclaw",
    "molecule-ai-workspace-template-codex",
    "molecule-core",
)

# Org template repos that are intentionally NOT runtime-wheel consumers and must
# be EXPLICITLY exempted (not silently omitted) from the drift check. Keeping
# them here — rather than dropping them on the floor — is what makes
# ``reconcile_org_consumers`` able to assert "every template repo is either
# enumerated or deliberately exempt".
#
#   molecule-ai-workspace-template-seo-agent — a Claude-Code config/prompts
#     template (config.yaml + prompts/ transported through the control plane).
#     It has no Dockerfile, no publish-image pipeline, and does not install the
#     molecule_runtime wheel, so it carries no .runtime-version and there is
#     nothing to keep in sync. If it ever adopts a .runtime-version (i.e. becomes
#     a wheel consumer), remove it here and add it to DEFAULT_CONSUMERS — the
#     reconcile check will force that decision.
EXEMPT_CONSUMERS = {
    "molecule-ai-workspace-template-seo-agent": (
        "config/prompts-only Claude-Code template; no Dockerfile / runtime wheel "
        "install / .runtime-version pin"
    ),
    "molecule-ai-workspace-template-google-adk": (
        "RETIRED 2026-07-09 (runtime#264/#265, sdk#80, core#3730) — google-adk "
        "dropped from the maintained runtime set; the template repo is archived and "
        "no longer wheel-bumped, so its frozen .runtime-version pin is expected, not "
        "drift. Kept here (not silently omitted) so reconcile_org_consumers still "
        "accounts for every template repo."
    ),
    "molecule-ai-workspace-template-crewai": (
        "RETIRED 2026-07-09 (runtime#264/#265, sdk#80, core#3730) — crewai dropped "
        "from the maintained runtime set; the template repo is archived and no longer "
        "wheel-bumped, so its frozen .runtime-version pin is expected, not drift."
    ),
}

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


class ReconcileUnavailable(RuntimeError):
    """The org-scan reconciliation could not run for a CONFIG/PERMISSION reason
    (e.g. the CI token lacks ``read:organization`` so ``/orgs/{org}/repos`` 403s),
    as opposed to discovering a real blind spot.

    runtime#83: a token-scope gap is a config gap, not a runtime regression. The
    primary pin-drift check still runs against the explicit ``DEFAULT_CONSUMERS``
    set (which is read per-repo, not via the org listing), so the guard must NOT
    paint runtime ``main`` red just because the *advisory* blind-spot reconcile
    can't enumerate the org. ``main`` degrades to a loud warning + skip in this
    case, exactly like the absent-token path in consumer-drift.yml.
    """


class PropagationStatusUnavailable(RuntimeError):
    """The propagation-in-flight probe (listing a consumer repo's open PRs) could
    not be completed for an INFRASTRUCTURE reason — absent token, network blip, or
    an auth/API failure — as opposed to returning a definitive "no in-flight bump
    PR" answer.

    Propagation-lag tolerance (this file): a consumer pin that lags the runtime
    SSOT is a HARD FAILURE only when we can AFFIRMATIVELY confirm there is no open
    ``.runtime-version`` bump PR resolving it — i.e. the runtime#91 propagation bot
    is genuinely stuck. If we merely could not check (this exception), we must NOT
    block a runtime PR on an undeterminable cross-repo signal: we degrade to an
    ADVISORY warning (fail-soft), symmetric with the absent-token skip in
    consumer-drift.yml and the ReconcileUnavailable org-scan degrade.
    """


@dataclass(frozen=True)
class DriftFinding:
    repo: str
    path: str
    reason: str


def _pyproject_version(runtime_root: Path) -> str:
    """Dev-tree version floor from pyproject.toml (stale after tag-stamped releases)."""
    pyproject = runtime_root / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    try:
        import tomli as _tomli

        return _tomli.load(pyproject.open("rb")).get("project", {}).get("version", "")
    except Exception:
        # Fallback: regex scan if tomli unavailable
        content = pyproject.read_text()
        for line in content.splitlines():
            if line.strip().startswith("version"):
                return line.split("=")[1].strip().strip('"').strip("'")
        return ""


def _latest_release_version() -> str:
    """Highest published runtime-v<semver> tag, via the Gitea API.

    Releases are TAG-stamped: auto-release computes the next version from
    tags and the publish workflow stamps it into the BUILD checkout only --
    pyproject.toml on main is a stale floor (it said 0.3.15 while v0.3.20
    was published). Comparing consumer pins to pyproject made this lane go
    permanently red the moment propagation started WORKING (consumers
    correctly pinned 0.3.19+ and read as drifted from 0.3.15).
    """
    import json
    import os
    import urllib.request

    token = os.environ.get("GITEA_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    url = "https://git.moleculesai.app/api/v1/repos/molecule-ai/molecule-ai-workspace-runtime/tags?limit=50"
    headers = {"Authorization": f"token {token}"} if token else {}
    headers.setdefault("User-Agent", "curl/8.4.0")  # CF edge 403s python-urllib UA (error 1010)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tags = json.load(resp)
    except Exception:
        return ""
    best = None
    for t in tags if isinstance(tags, list) else []:
        name = t.get("name", "")
        if not name.startswith("runtime-v"):
            continue
        try:
            ver = tuple(int(x) for x in name[len("runtime-v"):].split("."))
        except ValueError:
            continue
        if best is None or ver > best:
            best = ver
    return ".".join(str(x) for x in best) if best else ""


def current_runtime_version(runtime_root: Path) -> str:
    """The SSOT version consumers should pin: the latest PUBLISHED release
    tag, falling back to pyproject.toml (pre-first-release or offline)."""
    return _latest_release_version() or _pyproject_version(runtime_root)


def find_runtime_drift(repo_name: str, repo_path: Path, runtime_root: Path | None = None) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    sso_runtime_version = current_runtime_version(runtime_root or Path(__file__).resolve().parents[1])

    runtime_version_path = repo_path / ".runtime-version"
    if runtime_version_path.is_file():
        pinned = runtime_version_path.read_text().strip()
        if pinned and sso_runtime_version and pinned != sso_runtime_version:
            findings.append(
                DriftFinding(
                    repo=repo_name,
                    path=".runtime-version",
                    reason=f"runtime pin drift: pinned={pinned}, SSOT={sso_runtime_version}",
                )
            )

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


def _org_template_repos(gitea_url: str, token: str, *, org: str = "molecule-ai") -> list[str]:
    """Enumerate LIVE ``molecule-ai-workspace-template-*`` repos in the org via
    the Gitea API (paginated). Returns repo names. Raises on a hard API failure.

    ARCHIVED repos are excluded: archiving makes a Gitea repo read-only, so a
    ``.runtime-version`` pin in an archived repo is frozen by definition — a
    propagation bump PR cannot land there, and the repo is no longer a live
    wheel consumer. Flagging it as a "blind spot" would demand an action
    (enumerate or exempt) that can never converge back to green via the pin
    itself. Concrete instance: the four retired-runtime templates
    (langgraph / autogen / deepagents / gemini-cli) were archived org-wide on
    2026-07-04 as part of the 4-runtime removal, still carry their last-frozen
    pin, and painted runtime main red until this filter.
    """
    import json
    import urllib.request
    import urllib.error

    names: list[str] = []
    page = 1
    while True:
        url = f"{gitea_url}/api/v1/orgs/{org}/repos?limit=50&page={page}"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0", **({"Authorization": f"token {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                batch = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:200]
            # 401/403 here is a token-SCOPE gap (org listing needs
            # read:organization), not a real blind spot — surface it as a
            # reconcile-unavailable so main() can warn+skip instead of failing.
            if exc.code in (401, 403):
                raise ReconcileUnavailable(
                    f"org repo listing requires a token with read:organization "
                    f"(HTTP {exc.code}): {detail}"
                )
            raise RuntimeError(f"org repo listing failed (HTTP {exc.code}): {detail}")
        except Exception as exc:  # pragma: no cover - network errors
            raise RuntimeError(f"org repo listing failed: {exc}")
        if not isinstance(batch, list) or not batch:
            break
        for repo in batch:
            if repo.get("archived"):
                # Read-only: pin frozen, bump PRs impossible — not a live
                # consumer (see docstring).
                continue
            name = repo.get("name", "")
            if name.startswith("molecule-ai-workspace-template-"):
                names.append(name)
        if len(batch) < 50:
            break
        page += 1
    return names


def _repo_has_runtime_version(repo: str, gitea_url: str, token: str, *, org: str = "molecule-ai") -> bool:
    """True if the repo's default branch carries a ``.runtime-version`` file."""
    import urllib.request
    import urllib.error

    url = f"{gitea_url}/api/v1/repos/{org}/{repo}/raw/.runtime-version"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0", **({"Authorization": f"token {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"{repo}: unexpected HTTP {exc.code} probing .runtime-version")
    except Exception as exc:  # pragma: no cover - network errors
        raise RuntimeError(f"{repo}: error probing .runtime-version: {exc}")


def reconcile_org_consumers(
    enumerated: tuple[str, ...],
    *,
    gitea_url: str,
    token: str,
    org: str = "molecule-ai",
) -> list[str]:
    """Close the DEFAULT_CONSUMERS blind spot dynamically.

    Scan every ``molecule-ai-workspace-template-*`` repo in the org; any repo
    that carries a ``.runtime-version`` pin (i.e. is a real runtime-wheel
    consumer) MUST be either enumerated in ``DEFAULT_CONSUMERS`` or explicitly
    listed in ``EXEMPT_CONSUMERS``. Returns the list of un-accounted-for repos
    (empty == reconciled). This is what turns "someone forgot to add the new
    template to the guard list" from a silent green into a loud red.
    """
    enumerated_set = set(enumerated)
    unaccounted: list[str] = []
    for repo in _org_template_repos(gitea_url, token, org=org):
        if repo in enumerated_set or repo in EXEMPT_CONSUMERS:
            continue
        if _repo_has_runtime_version(repo, gitea_url, token, org=org):
            unaccounted.append(repo)
    return unaccounted


def _version_tuple(v: str) -> tuple[int, ...]:
    """Best-effort parse of a runtime version pin into a comparable tuple.

    Mirrors the propagation bot's tolerant parser (propagate_runtime_version.py):
    strips any pre-release/build suffix and coerces each dotted chunk to its
    leading integer so pins compare numerically (0.3.6 < 0.3.20)."""
    parts: list[int] = []
    for chunk in v.strip().split("-")[0].split("+")[0].split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _extract_bump_target(pr: dict) -> str | None:
    """Return the version an open PR bumps ``.runtime-version`` to, or None if the
    PR is not a runtime propagation bump PR.

    Keys on the runtime#91 propagation bot's canonical shape — either is
    sufficient (title is primary; head branch is the fallback if a title was
    hand-edited):
      - title:  ``chore(runtime): bump .runtime-version to <ver>``
      - head:   ``bump/runtime-<ver>``
    """
    import re

    title = (pr.get("title") or "").strip()
    m = re.match(r"chore\(runtime\):\s*bump\s+\.runtime-version\s+to\s+(\S+)", title)
    if m:
        return m.group(1)
    head = (pr.get("head") or {}).get("ref") or ""
    m = re.match(r"^bump/runtime-(\S+)$", head)
    if m:
        return m.group(1)
    return None


def _open_bump_pr_target(
    repo: str,
    *,
    pinned: str,
    gitea_url: str,
    token: str,
    org: str = "molecule-ai",
) -> str | None:
    """Return the version an OPEN ``.runtime-version`` bump PR on ``repo`` would
    advance the pin to — the highest bump target strictly greater than the lagging
    ``pinned`` value — or None if no such in-flight PR exists.

    This is the propagation-in-flight probe. After a runtime release the runtime#91
    bot opens a ``chore(runtime): bump .runtime-version to <ver>`` PR on each
    consumer and a human gates the merge, so during a burst of back-to-back
    releases a consumer pin legitimately lags the SSOT for the window between
    "release cut" and "bump PR merged". An OPEN bump PR that raises the pin is
    proof propagation is IN FLIGHT (self-healing) rather than STUCK.

    Raises PropagationStatusUnavailable if the open-PR listing cannot be fetched
    (network / auth / decode), so the caller can fail-soft to ADVISORY instead of
    blocking a runtime PR on an undeterminable cross-repo signal.
    """
    import json
    import urllib.request

    url = f"{gitea_url}/api/v1/repos/{org}/{repo}/pulls?state=open&limit=50"
    headers = {"User-Agent": "curl/8.4.0"}  # CF edge 403s the default urllib UA
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            prs = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - any failure => undeterminable => fail-soft
        raise PropagationStatusUnavailable(
            f"{repo}: cannot list open PRs to determine propagation-in-flight status: {exc}"
        )
    pinned_t = _version_tuple(pinned) if pinned else ()
    best: str | None = None
    for pr in prs if isinstance(prs, list) else []:
        target = _extract_bump_target(pr)
        if not target:
            continue
        # Only count a PR that actually advances the pin above its current lag
        # value (would resolve the lag). If we couldn't parse the pin, accept any
        # bump PR (fail toward advisory).
        if not pinned_t or _version_tuple(target) > pinned_t:
            if best is None or _version_tuple(target) > _version_tuple(best):
                best = target
    return best


def classify_pin_drift(
    findings: list[DriftFinding],
    *,
    pins: dict[str, str],
    gitea_url: str,
    token: str,
    org: str = "molecule-ai",
) -> tuple[list[DriftFinding], list[str]]:
    """Apply propagation-lag tolerance, splitting findings into
    ``(blocking, advisory_messages)``.

    Disposition:
      * NON pin-drift findings (forbidden top-level ``workspace/`` tree, vendored
        ``molecule_runtime/`` package) are ALWAYS blocking — they concern vendoring
        runtime SOURCE and have nothing to do with release-propagation timing.
      * A ``.runtime-version`` pin-drift finding is BLOCKING only when the consumer
        is STUCK: it lags the SSOT AND has no open bump PR advancing the pin. When
        an open ``chore(runtime): bump .runtime-version to <ver>`` PR (runtime#91)
        is in flight, the lag is transient/self-healing -> ADVISORY (not counted as
        a failure).
      * If in-flight status is UNDETERMINABLE (absent token, or the open-PR query
        failed) we fail-soft to ADVISORY rather than block a runtime PR on a signal
        we could not check.
    """
    blocking: list[DriftFinding] = []
    advisory: list[str] = []
    for finding in findings:
        is_pin_drift = (
            finding.path == ".runtime-version"
            and finding.reason.startswith("runtime pin drift")
        )
        if not is_pin_drift:
            blocking.append(finding)
            continue

        pinned = pins.get(finding.repo, "")
        if not token:
            advisory.append(
                f"{finding.repo}: {finding.reason} — cannot verify "
                f"propagation-in-flight status without a token; treating as ADVISORY "
                f"(fail-soft, not blocking)."
            )
            continue
        try:
            target = _open_bump_pr_target(
                finding.repo, pinned=pinned, gitea_url=gitea_url, token=token, org=org
            )
        except PropagationStatusUnavailable as exc:
            advisory.append(
                f"{finding.repo}: {finding.reason} — could not determine "
                f"propagation-in-flight status ({exc}); treating as ADVISORY "
                f"(fail-soft, not blocking)."
            )
            continue
        if target:
            advisory.append(
                f"{finding.repo}: {finding.reason} — propagation IN FLIGHT "
                f"(open bump PR advancing pin to {target}); ADVISORY, transient/"
                f"self-healing (bot filed the bump; awaiting merge)."
            )
        else:
            blocking.append(finding)
    return blocking, advisory


def _git_clone_with_token(dest: Path, url: str, token: str) -> subprocess.CompletedProcess[str]:
    """Clone using GIT_ASKPASS so the token never appears in argv or remote URL.

    Re-introduced on the runtime#86 branch after Kimi's prior GIT_ASKPASS attempt
    (commit 061716f) was reverted twice on main with no documented reason; the
    current re-application passes the existing test suite AND adds a regression
    gate so the URL-embedded pattern cannot return without a CI red.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write("#!/bin/sh\n")
        f.write('case "$1" in\n')
        f.write('  *Username*) echo "x-access-token" ;;\n')
        f.write(f'  *Password*) echo {shlex.quote(token)} ;;\n')
        f.write("esac\n")
        askpass = f.name
    os.chmod(askpass, 0o700)
    try:
        return subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env={**os.environ, "GIT_ASKPASS": askpass},
        )
    finally:
        os.unlink(askpass)


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
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    for repo in repos:
        dest = workdir / repo
        clone_url = f"{base_url}/molecule-ai/{repo}.git"
        for attempt in range(1, 4):
            result = _git_clone_with_token(dest, clone_url, token)
            if result.returncode == 0:
                paths[repo] = dest
                break
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
                continue
            stderr = result.stderr.replace(token, "<redacted>")
            raise RuntimeError(f"failed to clone {repo} after 3 attempts: {stderr.strip()}")
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
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help=(
            "Skip the org-scan reconciliation that fails when a "
            "molecule-ai-workspace-template-* repo carries a .runtime-version pin "
            "but is neither in DEFAULT_CONSUMERS nor EXEMPT_CONSUMERS. Reconcile "
            "is skipped automatically when --root or an explicit --repo set is used."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repos = tuple(args.repos or DEFAULT_CONSUMERS)

    token = os.environ.get(args.token_env, "")
    # Reconcile only when checking the full canonical set from a live org (token
    # present, no offline --root, no hand-picked --repo subset). Under --root or
    # an explicit --repo list there is no org to scan against.
    do_reconcile = (
        not args.no_reconcile
        and not args.root
        and not args.repos
        and bool(token)
    )

    tempdir: Path | None = None
    try:
        if do_reconcile:
            try:
                unaccounted = reconcile_org_consumers(
                    DEFAULT_CONSUMERS, gitea_url=args.gitea_url, token=token
                )
            except ReconcileUnavailable as exc:
                # Config/permission gap, not a runtime regression: warn loudly and
                # skip the blind-spot reconcile. The pin-drift check below still
                # runs against the explicit DEFAULT_CONSUMERS, so SSOT enforcement
                # is unaffected. Provision read:organization on the token to
                # re-enable the org-scan (runtime#83).
                print(
                    f"::warning::skipping org-scan reconcile: {exc}. The pin-drift "
                    f"check still runs against the enumerated DEFAULT_CONSUMERS; "
                    f"grant the CI token read:organization to re-enable the "
                    f"blind-spot reconcile.",
                    file=sys.stderr,
                )
                unaccounted = []
            if unaccounted:
                print(
                    "Runtime SSOT drift guard blind spot: these "
                    "molecule-ai-workspace-template-* repos carry a .runtime-version "
                    "pin but are NOT in DEFAULT_CONSUMERS or EXEMPT_CONSUMERS, so "
                    "their pin drift would go unchecked:\n"
                    + "\n".join(f"- {r}" for r in unaccounted)
                    + "\nAdd each to DEFAULT_CONSUMERS (real consumer) or "
                    "EXEMPT_CONSUMERS (with a reason).",
                    file=sys.stderr,
                )
                return 1

        if args.root:
            paths = consumer_paths_from_root(args.root, repos)
        else:
            tempdir = Path(tempfile.mkdtemp(prefix="runtime-consumer-drift-"))
            paths = clone_consumers(
                tempdir,
                repos,
                gitea_url=args.gitea_url,
                token=token,
            )

        findings: list[DriftFinding] = []
        runtime_root = Path(__file__).resolve().parents[1]
        pins: dict[str, str] = {}
        for repo, path in paths.items():
            findings.extend(find_runtime_drift(repo, path, runtime_root=runtime_root))
            rv = path / ".runtime-version"
            if rv.is_file():
                pins[repo] = rv.read_text().strip()

        # Propagation-lag tolerance: a consumer pin that lags the SSOT is only a
        # HARD FAILURE when the runtime#91 propagation bot is STUCK (no open bump
        # PR resolving it). A lag WITH an in-flight bump PR is a transient,
        # self-healing burst-window state -> ADVISORY, not a failure. Vendoring
        # findings (workspace/ tree, molecule_runtime/ package) always block.
        blocking, advisory = classify_pin_drift(
            findings, pins=pins, gitea_url=args.gitea_url, token=token
        )

        for msg in advisory:
            print(f"::warning::{msg}", file=sys.stderr)

        if blocking:
            print(format_findings(blocking), file=sys.stderr)
            if any(
                f.path == ".runtime-version" and f.reason.startswith("runtime pin drift")
                for f in blocking
            ):
                print(
                    "\nThe .runtime-version pin(s) above lag the runtime SSOT AND "
                    "have NO in-flight `chore(runtime): bump .runtime-version to ...` "
                    "PR — propagation is STUCK, not a transient burst-window lag. "
                    "This is the real signal: investigate/unwedge the runtime#91 "
                    "propagation bot (or merge the missing bump PR) before cutting "
                    "further releases.",
                    file=sys.stderr,
                )
            return 1

        if advisory:
            print(
                f"Runtime SSOT drift guard passed for {len(paths)} consumer repo(s) "
                f"— {len(advisory)} lagging consumer(s) have propagation IN FLIGHT "
                f"(advisory, self-healing; see ::warning:: lines above), 0 stuck."
            )
        else:
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
