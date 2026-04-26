"""Pre-commit hook installer — secret scan + internal-paths block.

Companion to ``credential_helper`` (#1933). Lifts the per-template wiring
that would otherwise need to live in each Dockerfile + entrypoint.sh:
copy the hook script to a known location, set ``core.hooksPath`` so git
finds it, and now every ``git commit`` runs through two defense-in-depth
gates with the recovery command printed in the same error.

Two gates, single hook file
===========================

The bundled ``pre-commit-checks.sh`` runs:

1. **Secret scan** — ALL repos. Refuses any commit whose staged
   additions contain a recognisable credential (GitHub PAT/installation
   tokens, Anthropic / OpenAI / Slack / AWS keys). Catches the leak
   regardless of how the secret arrived in the working tree (npm
   copying ``_authToken`` into ``package.json``, an agent persisting
   its own ``GITHUB_TOKEN`` to a config file, etc.).

2. **Internal-paths block** — ``Molecule-AI/molecule-monorepo`` and
   ``Molecule-AI/molecule-core`` only. Refuses commits that add
   ``research/``, ``marketing/``, etc. to the public monorepo with a
   redirect to ``Molecule-AI/internal``.

Both gates skip during rebase / cherry-pick / merge / revert (they
replay existing commits and blocking would force interactive history
rewriting).

Why a local hook is the right layer
===================================

Agents act on what's in the current response context. A pre-commit hook
converts "PR opens then fails 5 minutes later" into "commit refused
immediately, with the recovery command printed in the same response
cycle the agent ran ``git commit`` in." That's the highest density of
feedback we can provide.

Idempotence + non-clobber
=========================

If git's ``core.hooksPath`` is already set to something else (operator
configured a custom hook chain), we don't overwrite — we log a warning
and skip. The hook script itself is rewritten on every startup so
runtime upgrades pick up the latest pattern list without manual action.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

_HOOKS_DIR = Path(os.environ.get("HOME", "/home/agent")) / ".molecule-runtime" / "git-hooks"
_HOOK_SCRIPT_NAME = "pre-commit"
_BUNDLED_SCRIPT = "pre-commit-checks.sh"


def _existing_hooks_path() -> str | None:
    """Read git's current global core.hooksPath, or None if unset."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            check=False, capture_output=True, text=True,
        )
        # Exit 1 with empty stdout means "not set" — that's the case we want
        # to fill in. Any other non-empty value means an existing chain we
        # shouldn't clobber.
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except FileNotFoundError:
        # No git binary — install_pre_commit_hook will skip everything anyway.
        return None


def _extract_hook(target: Path) -> None:
    """Copy bundled hook script to the per-user hooks dir + chmod +x."""
    pkg_scripts = resources.files("molecule_runtime").joinpath("scripts")
    src_bytes = pkg_scripts.joinpath(_BUNDLED_SCRIPT).read_bytes()
    if target.exists() and target.read_bytes() == src_bytes:
        return  # idempotent — no change
    target.write_bytes(src_bytes)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_pre_commit_hook() -> None:
    """Install + activate the block-internal-paths pre-commit hook.

    Safe to call multiple times. Only sets ``core.hooksPath`` if it was
    previously unset — never clobbers an operator-configured chain.
    """
    if not shutil.which("git"):
        log.info("precommit_hook: no git binary on PATH — skip installation")
        return

    try:
        _HOOKS_DIR.mkdir(parents=True, exist_ok=True)
        _extract_hook(_HOOKS_DIR / _HOOK_SCRIPT_NAME)
    except (OSError, ModuleNotFoundError) as exc:
        log.warning("precommit_hook: cannot extract hook (%s) — skip", exc)
        return

    existing = _existing_hooks_path()
    if existing and Path(existing).resolve() != _HOOKS_DIR.resolve():
        log.warning(
            "precommit_hook: git core.hooksPath already set to %s — leaving it alone. "
            "Internal-paths gate will NOT fire on commits in the public monorepo. "
            "If this is intentional, point that hooks dir at %s/%s as well.",
            existing, _HOOKS_DIR, _HOOK_SCRIPT_NAME,
        )
        return

    try:
        subprocess.run(
            ["git", "config", "--global", "core.hooksPath", str(_HOOKS_DIR)],
            check=True, capture_output=True,
        )
        log.info("precommit_hook: installed and activated at %s", _HOOKS_DIR / _HOOK_SCRIPT_NAME)
    except subprocess.SubprocessError as exc:
        log.warning("precommit_hook: git config failed (%s) — hook installed but inactive", exc)
