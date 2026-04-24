"""Pre-commit hook installer — block internal-flavored paths in public monorepo.

Companion to ``credential_helper`` (#1933). Lifts the per-template wiring
that would otherwise need to live in each Dockerfile + entrypoint.sh:
copy the hook script to a known location, set ``core.hooksPath`` so git
finds it, and now every ``git commit`` inside the public monorepo gets a
local refusal with the redirect command in the same error.

Why a local hook is the right layer
===================================

Agents try to ``git add /research/foo.md`` despite SHARED_RULES, the
``.gitignore`` patterns, and the CI gate. Each leak attempt costs ~5
cycles: PR opens, CI fails (now that ``Block forbidden paths`` is
required), agent retries with workaround. Pollutes git history with
reverts.

A pre-commit hook converts the failure from "PR opens then fails 5
minutes later" to "commit refused immediately, with the recovery
command printed in the same response cycle the agent ran ``git commit``
in." Agents act on what's in the current response context — putting the
redirect command literally in the error message they see is the highest
density of feedback we can provide.

Scoped to public monorepo only
==============================

The hook checks ``git remote get-url origin`` and only enforces when the
repo is ``Molecule-AI/molecule-monorepo`` or ``Molecule-AI/molecule-core``.
Inside ``Molecule-AI/internal`` (where ``research/``, ``marketing/``
etc. legitimately belong) the hook is a no-op. Same for plugin /
template / third-party repos.

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
_BUNDLED_SCRIPT = "pre-commit-block-internal-paths.sh"


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
