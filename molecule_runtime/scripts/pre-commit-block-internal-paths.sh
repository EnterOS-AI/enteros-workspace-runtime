#!/bin/bash
# pre-commit hook — refuse commits that add internal-flavored paths to the
# public monorepo. Only enforces in the Molecule-AI public repos; no-op in
# every other repo (including the canonical internal one) so agents can
# still write `research/foo.md` inside `Molecule-AI/internal`.
#
# Why this hook exists
# ====================
#
# Despite SHARED_RULES.md, .gitignore, and a CI gate, agents still try to
# `git add /research/...` from their cwd in `molecule-monorepo`. Each leak
# attempt costs ~5 cycles (PR opens, CI fails, agent retries with
# workaround) and pollutes git history with reverts. This hook converts
# the failure mode from "PR fails" → "commit refused at the agent's local
# git" — instant feedback with the redirect command in the same error
# message.
#
# Installed via `core.hooksPath` set by molecule_runtime.precommit_hook
# at workspace startup.

set -e

# Skip silently when GIT_AUTHOR_EMAIL/USER is unset — likely a non-agent
# context (operator manually running git inside the container for debug).
# Agents always have the provisioner-set GIT_AUTHOR_NAME.
if [ -z "${GIT_AUTHOR_NAME:-}${GIT_COMMITTER_NAME:-}" ]; then
    exit 0
fi

# Determine if we're in a public Molecule-AI repo. `git remote get-url`
# returns nothing in repos without a remote (fine — exit clean).
REMOTE=$(git remote get-url origin 2>/dev/null || echo "")

case "$REMOTE" in
    *Molecule-AI/molecule-monorepo*|*Molecule-AI/molecule-core*)
        # Continue — this is a public repo we enforce on.
        ;;
    *)
        # Non-target repo (internal, plugins, templates, third-party) — let it through.
        exit 0
        ;;
esac

# Files added or modified in this commit. --diff-filter=AM excludes
# deletions so cleanup commits don't trip the gate.
STAGED=$(git diff --cached --name-only --diff-filter=AM)
[ -z "$STAGED" ] && exit 0

FORBIDDEN_PATTERNS=(
    "^research/"
    "^marketing/"
    "^docs/marketing/"
    "^comment-[0-9]+\.json$"
    "^test-pmm.*\.(txt|md)$"
    "^tick-reflections.*\.(txt|md)$"
    ".*-temp\.(md|txt)$"
)

OFFENDING=""
for path in $STAGED; do
    for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
        if echo "$path" | grep -qE "$pattern"; then
            OFFENDING="${OFFENDING}  - ${path}  (matched: ${pattern})\n"
            break
        fi
    done
done

[ -z "$OFFENDING" ] && exit 0

# Refuse the commit with the redirect instructions in the same message.
{
    echo
    echo "Refusing commit: internal-flavored paths cannot live in the public monorepo."
    echo
    echo "Offending files:"
    printf "$OFFENDING"
    echo
    echo "These belong in Molecule-AI/internal. Redirect:"
    echo
    echo "  mkdir -p ~/repos"
    echo "  test -d ~/repos/internal || gh repo clone Molecule-AI/internal ~/repos/internal"
    echo "  cd ~/repos/internal"
    echo "  git pull origin main"
    echo "  git checkout -b <my-role>/<topic>-<date>"
    echo "  mkdir -p <area>            # research, marketing, runbooks, etc."
    echo "  # move your file from the monorepo into <area>/<slug>.md"
    echo "  git add <area>/<slug>.md"
    echo "  git commit -m '<area>: add <slug>'"
    echo "  git push -u origin HEAD"
    echo "  gh pr create --base main --fill"
    echo
    echo "If your file is genuinely public-facing (final blog post, public"
    echo "tutorial, customer-shippable doc), use one of these monorepo paths"
    echo "instead — these are not blocked:"
    echo "  - docs/blog/<slug>.md"
    echo "  - docs/tutorials/<slug>.md"
    echo "  - docs/devrel/<slug>.md"
    echo "  - docs/api/<slug>.md"
    echo
    echo "If you legitimately need a new top-level path that matches a"
    echo "forbidden pattern, edit:"
    echo "  .github/workflows/block-internal-paths.yml"
    echo "with reviewer signoff and a public-facing justification — do NOT"
    echo "work around the gate by renaming."
    echo
    echo "Hook source: molecule_runtime/scripts/pre-commit-block-internal-paths.sh"
} >&2

exit 1
