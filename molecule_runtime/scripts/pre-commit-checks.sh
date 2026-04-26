#!/bin/bash
# pre-commit hook — defense-in-depth checks against agent foot-guns.
#
# Two independent gates, run in order:
#
#   1. Secret scan (ALL repos, including third-party). Refuses any commit
#      whose staged additions contain a recognisable credential. Catches
#      the canonical leak vectors regardless of how the secret arrived in
#      the working tree (npm copying _authToken into package.json, an
#      agent persisting its own GITHUB_TOKEN to a config file, a
#      mis-configured `git clone https://x-access-token:GHS@github.com/...`
#      remote, etc.). Same regex set as the tenant-proxy CI scanner —
#      keep them aligned.
#
#   2. Internal-paths block (Molecule-AI public repos only). Refuses
#      commits that add `research/`, `marketing/`, etc. to the public
#      monorepo. Was the original purpose of this hook; lives here so
#      every agent commit only pays one hook-install + one git config
#      surface area, not two.
#
# Installed via `core.hooksPath` set by molecule_runtime.precommit_hook
# at workspace startup.

set -e

# Skip silently when GIT_AUTHOR_NAME/COMMITTER_NAME is unset — likely a
# non-agent context (operator manually running git inside the container
# for debug). Both gates assume the agent profile.
if [ -z "${GIT_AUTHOR_NAME:-}${GIT_COMMITTER_NAME:-}" ]; then
    exit 0
fi

# Skip during rebase / cherry-pick / merge / revert — these REPLAY
# existing commits and the staged file set is whatever was already
# committed upstream. Blocking them forces interactive history rewriting
# that most agents won't do, leaving DIRTY PRs unmergeable. Both gates
# share this skip.
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null || echo .git)
for state_dir in rebase-merge rebase-apply CHERRY_PICK_HEAD MERGE_HEAD REVERT_HEAD; do
    if [ -e "${GIT_DIR}/${state_dir}" ]; then
        exit 0
    fi
done

REMOTE=$(git remote get-url origin 2>/dev/null || echo "")

# ─── 1. Secret scan ────────────────────────────────────────────────────
#
# Scans the staged diff (added lines only) for credential patterns.
# Refuses the commit if any pattern matches. The error message names the
# file + the pattern that matched but never echoes the secret value
# itself — avoids round-tripping the leaked credential into terminal
# scrollback / agent context windows.
#
# Pattern set covers the high-value GitHub family (the actual #2090
# incident vector), the most common cloud + LLM provider tokens, and
# AWS access keys. Regex anchored on prefixes that have low false-
# positive rates against agent-generated content (no plain hex blobs).
SECRET_PATTERNS=(
    'ghp_[A-Za-z0-9]{36,}'           # GitHub PAT (classic)
    'ghs_[A-Za-z0-9]{36,}'           # GitHub App installation token
    'gho_[A-Za-z0-9]{36,}'           # GitHub OAuth user-to-server
    'ghu_[A-Za-z0-9]{36,}'           # GitHub OAuth user
    'ghr_[A-Za-z0-9]{36,}'           # GitHub OAuth refresh
    'github_pat_[A-Za-z0-9_]{82,}'   # GitHub fine-grained PAT
    'sk-ant-[A-Za-z0-9_-]{40,}'      # Anthropic API key
    'sk-proj-[A-Za-z0-9_-]{40,}'     # OpenAI project key
    'sk-svcacct-[A-Za-z0-9_-]{40,}'  # OpenAI service-account key
    'xox[baprs]-[A-Za-z0-9-]{20,}'   # Slack tokens (bot/app/user/refresh)
    'AKIA[0-9A-Z]{16}'               # AWS access key ID
    'ASIA[0-9A-Z]{16}'               # AWS STS temp access key ID
)

# Only check ADDITIONS (lines starting with + but not the +++ header).
# This avoids re-flagging unchanged secrets that may already exist on
# disk from a previous commit (those are tracked separately by repo
# scanners; the local hook only enforces "don't add new ones").
DIFF=$(git diff --cached --no-color --unified=0 2>/dev/null | grep -E '^\+[^+]' || true)

if [ -n "$DIFF" ]; then
    SECRET_HITS=""
    for pattern in "${SECRET_PATTERNS[@]}"; do
        # Use grep -lE on the diff to find which file the match came from.
        # `git diff --cached --name-only -G<pattern>` would be cleaner but
        # -G operates on hunks not added lines, and we explicitly want
        # only added lines. Walk staged files individually for the file
        # attribution.
        if echo "$DIFF" | grep -qE "$pattern"; then
            for f in $(git diff --cached --name-only --diff-filter=AM); do
                if git diff --cached --no-color --unified=0 -- "$f" 2>/dev/null \
                    | grep -E '^\+[^+]' \
                    | grep -qE "$pattern"; then
                    SECRET_HITS="${SECRET_HITS}  - ${f}  (matched: ${pattern})\n"
                fi
            done
        fi
    done

    if [ -n "$SECRET_HITS" ]; then
        {
            echo
            echo "Refusing commit: staged additions contain credential-shaped strings."
            echo
            echo "Offending files:"
            printf "$SECRET_HITS"
            echo
            echo "The actual matched values are NOT printed here, deliberately —"
            echo "echoing them back into scrollback / agent context would round-trip"
            echo "the leak into another surface."
            echo
            echo "Recovery:"
            echo "  1. git restore --staged <file>"
            echo "  2. Remove the secret from <file> and replace with an env var"
            echo "     reference (e.g. \${GITHUB_TOKEN}). Never inline credentials."
            echo "  3. If the credential was already exposed (committed locally OR"
            echo "     visible to the agent in any prior tool output), treat it as"
            echo "     compromised — rotate it immediately, do not just remove it."
            echo "  4. git add <file> && git commit"
            echo
            echo "If the match is a false positive (test fixture / docs example),"
            echo "use a clearly-fake placeholder like ghs_EXAMPLE_TOKEN_DO_NOT_USE"
            echo "that doesn't satisfy the length suffix."
            echo
            echo "Hook source: molecule_runtime/scripts/pre-commit-checks.sh"
        } >&2
        exit 1
    fi
fi

# ─── 2. Internal-paths block (public Molecule-AI repos only) ──────────
#
# Despite SHARED_RULES.md, .gitignore, and a CI gate, agents still try
# to `git add /research/...` from their cwd in `molecule-monorepo`. Each
# leak attempt costs ~5 cycles (PR opens, CI fails, agent retries with
# workaround) and pollutes git history with reverts. This gate converts
# the failure mode from "PR fails" → "commit refused at the agent's
# local git" — instant feedback with the redirect command in the same
# error message.
case "$REMOTE" in
    *Molecule-AI/molecule-monorepo*|*Molecule-AI/molecule-core*)
        ;;
    *)
        # Non-target repo (internal, plugins, templates, third-party) — let it through.
        exit 0
        ;;
esac

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
    echo "Hook source: molecule_runtime/scripts/pre-commit-checks.sh"
} >&2

exit 1
