"""Regression gate (runtime#86): forbid token-in-URL patterns in workflow + scripts.

The 2026-05-28 family of bugs included ``x-access-token:${DISPATCH_TOKEN}``
embedded in a ``git clone`` URL — the token ended up in subprocess argv
(visible via ``ps``/``/proc/*/cmdline``), the git remote URL (visible via
``git remote -v``), and git diagnostics/logs.

The fix is GIT_ASKPASS / Gitea-API rather than URL-embedded tokens. This
gate makes the regression observable: a CI red on the next PR that
re-introduces the pattern.

Scope
-----
* Walks ``.gitea/workflows/*.yml`` — the CI surface.
* Walks ``scripts/*.py`` — the audit-script surface (clone helpers used by
  workflows).
* Walks ``scripts/*.sh`` — shell helpers (none shipped today, but covered
  for symmetry).

What the gate matches
---------------------
A *leak* is ``x-access-token:`` followed by something that *looks like* a
token or a variable that evaluates to a token — bash ``${...}``,
GitHub Actions ``${{ ... }}``, Python f-string ``{...}``, or a quoted
literal token.

What the gate does NOT match
----------------------------
* ``echo "x-access-token"`` — the GIT_ASKPASS script's *username* response.
  This is the EXPECTED, safe occurrence (the token is fed via the
  *password* prompt, never the URL).
* Comment text describing the pattern (e.g. ``# fix: x-access-token:${...}``).
  These use a leading ``#`` so are skipped.

Failure shape
-------------
A red gate prints every offending (file, line-no, line) tuple so the
offending PR author can fix or justify each in review.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths the gate scans.  Resolved relative to REPO_ROOT and the existence
# of each is asserted at collection time so a misconfigured env surfaces
# a clear failure rather than a vacuous pass.
SCAN_TARGETS = (
    REPO_ROOT / ".gitea" / "workflows",
    REPO_ROOT / "scripts",
)

# Leak pattern: x-access-token: followed by a token-looking value.
# The non-capturing group below covers the four "looks-like-a-token"
# shapes we care about:
#   1. bash ${VAR}
#   2. GitHub Actions ${{ ... }}
#   3. Python f-string {var} (with optional !r/!s and format spec)
#   4. A bare quoted token literal, e.g. "ghs_xxx..." or "abc123..."
# Comment-line filtering is applied at the file-scan layer
# (see _COMMENT_LINE_RE) rather than in the regex — a negative-lookbehind
# for "#" only catches the char immediately before, which is whitespace
# (not "#") on a real comment line, so the regex alone cannot tell.
_LEAK_RE = re.compile(
    r"""
    x-access-token                     # the forbidden prefix
    \s*:\s*                            # the colon separator
    (?:
        \$\{[^}]+\}                    # bash ${VAR}
      | \$\{\{[^}]+\}\}                # GitHub Actions ${{ expr }}
      | \{[a-zA-Z_][a-zA-Z0-9_]*[!:>.<}]?   # Python f-string {var}
      | ["'][A-Za-z0-9_\-]{8,}["']    # quoted token literal (>=8 chars)
    )
    """,
    re.VERBOSE,
)

# Comment-line detector: lines whose first non-whitespace is '#'.
_COMMENT_LINE_RE = re.compile(r"^\s*#")


def _iter_candidate_files() -> list[Path]:
    """Enumerate files the gate scans.  Excludes the test file itself."""
    files: list[Path] = []
    for target in SCAN_TARGETS:
        assert target.exists(), f"scan target missing: {target}"
        if target.is_file():
            files.append(target)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix in {".yml", ".yaml", ".py", ".sh"}:
                files.append(path)
    # Exclude the test file itself (its docstring + tests reference the
    # pattern; gating the gate would loop).
    return [p for p in files if p != Path(__file__)]


def _scan_file(path: Path) -> list[tuple[Path, int, str]]:
    """Return list of (path, line-no, line) for every leak match in path."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    return _scan_file_for_text(text, source_name=str(path))


def _scan_file_for_text(
    text: str, source_name: str = "<text>"
) -> list[tuple[Path, int, str]]:
    """Return list of (path, line-no, line) for every leak match in text.

    ``source_name`` is just a label for the returned tuples; the line
    number is 1-based.  Split out from ``_scan_file`` so the
    ``test_leak_regex_does_not_match_comment_lines`` regression guard can
    drive the same scan pipeline against inline text.
    """
    findings: list[tuple[Path, int, str]] = []
    label = Path(source_name)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _COMMENT_LINE_RE.match(line):
            continue
        if _LEAK_RE.search(line):
            findings.append((label, lineno, line))
    return findings


def test_scan_targets_exist() -> None:
    """The gate's scan targets are present (so a vacuous pass is impossible)."""
    for target in SCAN_TARGETS:
        assert target.exists(), (
            f"scan target {target} must exist; otherwise the gate is a "
            f"vacuous pass — fix the gate, not the test"
        )


def test_no_workflow_or_script_embeds_token_in_clone_url() -> None:
    """No ``.gitea/workflows/*.yml`` or script may embed a token in a URL.

    Runtime#86: the original bug was ``x-access-token:${DISPATCH_TOKEN}`` in
    a ``git clone`` URL inside the publish-runtime workflow.  The fix moved
    to GIT_ASKPASS / Gitea-API.  This regression gate makes the fix durable.
    """
    files = _iter_candidate_files()
    # Defence-in-depth: the gate must actually be scanning something.
    assert files, "no candidate files found — gate is vacuous"

    all_findings: list[tuple[Path, int, str]] = []
    for path in files:
        all_findings.extend(_scan_file(path))

    if all_findings:
        # Pretty-print all offenders so the failing PR author gets a clear
        # diff-shaped report (rather than a single AssertionError blob).
        report = "\n".join(
            f"  {p.relative_to(REPO_ROOT)}:{ln}: {line.strip()}"
            for p, ln, line in all_findings
        )
        pytest.fail(
            "Forbidden token-in-URL pattern detected (runtime#86 regression). "
            "Use GIT_ASKPASS or the Gitea contents+pulls API instead — never "
            "embed a token in a URL passed to git / curl. Offenders:\n"
            f"{report}"
        )


def test_askpass_username_response_is_the_only_allowed_occurrence() -> None:
    """The GIT_ASKPASS helper legitimately writes ``x-access-token`` as the
    *username* response (the token rides the *password* prompt).  This test
    pins that pattern so any future re-introduction of the URL-embedding
    pattern is *visible* in a diff but does not falsely trip the gate.

    It does NOT relax the gate — it just guards the documented exception
    shape so a refactor of the askpass helper can keep working.
    """
    expected_substring = 'echo "x-access-token"'
    matches: list[Path] = []
    for path in _iter_candidate_files():
        if path.suffix != ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if expected_substring in text:
            matches.append(path)

    assert matches, (
        "GIT_ASKPASS helpers no longer write the expected `x-access-token` "
        "username response. If the askpass mechanism was intentionally "
        "replaced, update this test to match the new shape — do not silently "
        "let the gate pass on no occurrences (vacuous protection)."
    )
    # The expected set: both consumer-drift + platform-comm-contract scripts.
    expected_paths = {
        REPO_ROOT / "scripts" / "check_consumer_runtime_drift.py",
        REPO_ROOT / "scripts" / "check_platform_comm_contract.py",
    }
    assert set(matches) == expected_paths, (
        f"GIT_ASKPASS helper moved/added/removed: got {sorted(p.name for p in matches)}, "
        f"expected {sorted(p.name for p in expected_paths)}"
    )


def test_leak_regex_does_not_match_comment_lines() -> None:
    """The gate's scan pipeline must skip comment lines so documentation
    referencing the pattern (commit messages, this file, etc.) does not
    false-positive.

    Regression guard for the gate itself: if someone refactors
    ``_scan_file`` and drops the ``_COMMENT_LINE_RE`` short-circuit, every
    doc line referencing the pattern would trip the gate.
    """
    sample = (
        "# The prior bug was: x-access-token:${DISPATCH_TOKEN}@github.com/...\n"
        'x-access-token:"sk_live_actual_leak"\n'
    )
    # Drive the full scan pipeline (per-line comment skip + regex) rather
    # than the regex in isolation, so the test exercises the *real* guard
    # contract — not just the regex.
    findings = _scan_file_for_text(sample, source_name="<inline>")
    assert len(findings) == 1, (
        f"expected exactly 1 leak finding (the non-comment line), got "
        f"{findings!r}"
    )
    _path, _lineno, line = findings[0]
    assert "sk_live_actual_leak" in line, (
        f"the leak match should be the actual token line, got {line!r}"
    )
