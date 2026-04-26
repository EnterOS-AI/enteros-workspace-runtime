"""End-to-end test for the bundled pre-commit hook script.

The hook runs as a real bash subprocess inside a real temp git repo with
real staged content — there's no Python-side simulation. This is the
only way to exercise the actual contract (refuses commit on secret
match, lets clean commits through) and catch shell-level regressions
like accidental ``set -e`` interactions or pattern-matching drift.

Two paths covered:

1. **Secret scan** — refuses any repo when staged additions contain a
   credential-shaped string. Tested with a ``ghs_*`` token shape (the
   actual #2090 incident vector) so the most important regression case
   is locked.

2. **Clean commit through** — verifies the hook is a no-op for benign
   content, confirming we haven't shipped a check that fails open or
   blocks every commit.

Skipped on platforms without ``bash`` on PATH (Windows CI without WSL).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

_BASH = shutil.which("bash")


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess + return result. Always capture both streams."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd, cwd=cwd, env=full_env,
        capture_output=True, text=True, check=False,
    )


def _init_repo(repo: Path) -> None:
    """Create a fresh git repo with the agent identity set so the hook
    doesn't bail on the GIT_AUTHOR_NAME-empty fast path."""
    _run(["git", "init", "-q", "-b", "main"], cwd=repo).check_returncode()
    _run(["git", "config", "user.email", "agent@molecule.ai"], cwd=repo).check_returncode()
    _run(["git", "config", "user.name", "test-agent"], cwd=repo).check_returncode()


def _install_hook(repo: Path) -> Path:
    """Copy the bundled hook into the repo's local .git/hooks/pre-commit
    and chmod +x. Return the installed path."""
    src = resources.files("molecule_runtime").joinpath("scripts", "pre-commit-checks.sh")
    hook_dir = repo / ".git" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    target = hook_dir / "pre-commit"
    target.write_bytes(src.read_bytes())
    target.chmod(0o755)
    return target


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialised git repo with the bundled hook installed."""
    _init_repo(tmp_path)
    _install_hook(tmp_path)
    return tmp_path


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH")
def test_secret_scan_refuses_github_installation_token(repo: Path) -> None:
    """A staged file containing a ghs_-prefixed token must abort the commit.

    Lock for the #2090 incident: ``package.json`` with a
    ``"_authToken": "ghs_..."`` entry should never reach git history.
    """
    pkg = repo / "package.json"
    pkg.write_text(
        '{\n'
        '  "name": "tenant-proxy",\n'
        '  "publishConfig": {\n'
        '    "//npm.pkg.github.com/:_authToken": "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n'
        '  }\n'
        '}\n'
    )
    _run(["git", "add", "package.json"], cwd=repo).check_returncode()

    result = _run(
        ["git", "commit", "-m", "feat: add token", "--no-gpg-sign"],
        cwd=repo,
        env={"GIT_AUTHOR_NAME": "test-agent", "GIT_COMMITTER_NAME": "test-agent"},
    )
    assert result.returncode != 0, "commit should be refused"
    assert "Refusing commit" in result.stderr
    assert "credential-shaped" in result.stderr
    assert "package.json" in result.stderr
    assert "ghs_" in result.stderr  # the pattern name is OK to surface
    # The actual matched value must NOT appear — the secret stays out of
    # scrollback. Spot-check the exact suffix string.
    assert "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in result.stderr


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH")
def test_clean_commit_passes_through(repo: Path) -> None:
    """Benign content must commit cleanly — the hook is not allowed to
    fail open OR block every commit. This is the regression guard
    against shipping a hook that breaks every agent's git workflow."""
    f = repo / "README.md"
    f.write_text("# Test\n\nNo secrets here.\n")
    _run(["git", "add", "README.md"], cwd=repo).check_returncode()

    result = _run(
        ["git", "commit", "-m", "docs: readme", "--no-gpg-sign"],
        cwd=repo,
        env={"GIT_AUTHOR_NAME": "test-agent", "GIT_COMMITTER_NAME": "test-agent"},
    )
    assert result.returncode == 0, f"clean commit refused: {result.stderr}"


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH")
def test_secret_scan_runs_on_third_party_repos(repo: Path) -> None:
    """The secret scan must NOT be scoped to Molecule-AI public repos —
    it runs on every repo. Internal-paths block was the original gate
    and was scoped; secrets are universal."""
    # No remote set → not a Molecule-AI repo. Internal-paths block would
    # exit clean here (good); secret scan must still fire.
    leaky = repo / "config.yml"
    leaky.write_text("anthropic_key: sk-ant-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP\n")
    _run(["git", "add", "config.yml"], cwd=repo).check_returncode()

    result = _run(
        ["git", "commit", "-m", "config: anthropic", "--no-gpg-sign"],
        cwd=repo,
        env={"GIT_AUTHOR_NAME": "test-agent", "GIT_COMMITTER_NAME": "test-agent"},
    )
    assert result.returncode != 0, "secret scan must fire even without a Molecule-AI remote"
    assert "sk-ant-" in result.stderr
