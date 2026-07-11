#!/usr/bin/env python3
"""Decide whether a main-branch change affects the published runtime package."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable


NON_PACKAGE_PREFIXES = (
    ".gitea/",
    ".github/",
    "docs/",
    "tests/",
)
NON_PACKAGE_FILES = {
    ".editorconfig",
    ".gitignore",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _normalize(path: str) -> str:
    path = path.strip()
    return path[2:] if path.startswith("./") else path


def is_package_affecting(path: str) -> bool:
    """Return False only for paths proven not to change a built artifact."""
    normalized = _normalize(path)
    if normalized in NON_PACKAGE_FILES:
        return False
    if normalized.startswith("scripts/test_"):
        return False
    return not normalized.startswith(NON_PACKAGE_PREFIXES)


def release_required(
    paths: Iterable[str], *, event_name: str = "push"
) -> tuple[bool, str]:
    """Classify a push conservatively; uncertainty releases rather than skips."""
    if event_name == "workflow_dispatch":
        return True, "manual workflow dispatch"

    changed = [_normalize(path) for path in paths if _normalize(path)]
    if not changed:
        return True, "empty diff; release fails safe"

    affecting = [path for path in changed if is_package_affecting(path)]
    if affecting:
        return True, "package-affecting changes detected"
    return False, "no package-affecting changes"


def changed_paths(before: str, after: str) -> list[str]:
    if not SHA_RE.fullmatch(before) or not SHA_RE.fullmatch(after):
        raise ValueError("push event is missing valid before/after commit SHAs")
    if before == "0" * 40:
        raise ValueError("push event has no usable before commit")

    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "--diff-filter=ACDMRT",
            before,
            after,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def write_decision(should_release: bool, reason: str, changed_count: int) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output_file:
            output_file.write(f"should_release={str(should_release).lower()}\n")
            output_file.write(f"reason={reason}\n")
            output_file.write(f"changed_count={changed_count}\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        decision = "release required" if should_release else "release skipped"
        with open(summary_path, "a", encoding="utf-8") as summary_file:
            summary_file.write("## Runtime release decision\n\n")
            summary_file.write(f"**Decision:** {decision}\n\n")
            summary_file.write(f"**Reason:** {reason}\n")


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "push")
    paths: list[str] = []
    if event_name != "workflow_dispatch":
        try:
            paths = changed_paths(
                os.environ.get("BEFORE_SHA", ""),
                os.environ.get("AFTER_SHA", ""),
            )
            should_release, reason = release_required(paths, event_name=event_name)
        except (ValueError, subprocess.CalledProcessError) as exc:
            should_release = True
            reason = f"classification unavailable; release fails safe: {exc}"
            print(f"::warning::{reason}")
    else:
        should_release, reason = release_required(paths, event_name=event_name)

    if should_release:
        print(f"::notice::release required: {reason}")
    else:
        print("::notice::release skipped: no package-affecting changes")
    write_decision(should_release, reason, len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
