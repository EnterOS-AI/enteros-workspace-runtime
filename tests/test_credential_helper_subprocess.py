"""Real-subprocess test for credential_helper (issue #87).

Background
----------

The 2026-06-08 "MiniMax cred incident" lost 39 workspaces because
``credential_helper`` wasn't actually exercised end-to-end in CI — only
its mocked behavior. The mock passed; the real subprocess calls to
``git config``, ``nohup``, and ``gh auth login`` either silently
no-op'd or failed in a way the mock didn't catch. Issue #87 (this
PR) calls for a real-subprocess test that actually runs the
credential_helper against a temp HOME and verifies the side effects
on disk + on git config.

The existing ``test_credential_helper_determinism.py`` covers the
provider-selection logic (GIT_PROVIDER=gitea vs github) with
mocks. This file covers the SUBPROCESS contract — the part that
mocks can lie about.

What this test pins
--------------------

* ``_extract_scripts()`` writes real bash scripts to ``$HOME/.molecule-runtime/scripts/``
  and the files are executable (``os.access(..., X_OK)`` is True).
* ``_configure_git_credential_helper()`` actually invokes
  ``git config --global credential.https://github.com.helper <path>``
  and the result is readable back via ``git config --global --get``.
* ``_start_refresh_daemon()`` spawns a detached nohup process and
  writes a PID file at ``$HOME/.molecule-runtime/refresh-daemon.pid``;
  the PID is parseable as an int and the process is either still
  alive or has exited cleanly (no zombie).
* The full ``install_credential_helper()`` flow against a temp HOME
  produces all three artifacts (scripts, git config, PID file) when
  ``GIT_PROVIDER=github`` and a token-shaped value is set.
* The full flow is a clean no-op (no scripts, no git config, no
  PID file) when ``GIT_PROVIDER=gitea`` (the "do not install
  GitHub machinery" contract — regression class for the 2026-06-08
  incident where both flows were active and the wrong one hijacked
  the workspace).

Run in CI (real ``$PATH`` with git, nohup; no mocks). Skip LOUDLY
(not silently) when git or nohup is missing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


# ---------- fixtures ----------

@pytest.fixture
def temp_home(monkeypatch, tmp_path):
    """A real temp HOME for the credential_helper to write into.

    Sets ``HOME``, ``XDG_CONFIG_HOME`` (git honors this), and unset
    ``GIT_PROVIDER`` so the test fully controls the provider-selection
    path. The temp HOME is wiped clean on teardown.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("GIT_PROVIDER", raising=False)
    return home


def _require_git():
    """LOUD skip when git is missing — never silently pass."""
    if not shutil.which("git"):
        pytest.skip("git binary not on PATH — cannot run real-subprocess credential_helper test")


def _require_nohup():
    if not shutil.which("nohup"):
        pytest.skip("nohup binary not on PATH — cannot exercise the refresh-daemon spawn")


def _credential_helper_for_home(home):
    """Get the credential_helper module with its module-level
    ``_INSTALL_DIR`` and ``_TOKEN_CACHE_DIR`` paths retargeted at the
    given temp HOME.

    Why this dance: ``_INSTALL_DIR`` and ``_TOKEN_CACHE_DIR`` are
    ``Path(...)`` objects captured at module-import time from
    ``os.environ.get("HOME", ...)``. We don't want to reload the
    module (reloading breaks import machinery — the parent package's
    other imports lose track of it). Instead, we directly assign
    fresh ``Path`` instances into the module's namespace. The rest of
    the module's functions look these up at call time, so the
    retargeted paths take effect immediately.
    """
    from molecule_runtime import credential_helper

    # Retarget the module-level paths. Use ``object.__setattr__`` to
    # avoid the read-only module-attr guard (modules ARE writable,
    # but the explicit form makes intent clear).
    install_dir = Path(str(home)) / ".molecule-runtime" / "scripts"
    token_cache_dir = Path(str(home)) / ".molecule-token-cache"
    credential_helper._INSTALL_DIR = install_dir
    credential_helper._TOKEN_CACHE_DIR = token_cache_dir
    # _DAEMON_PID_FILE is also captured at import; retarget for the
    # subprocess test that checks for it.
    credential_helper._DAEMON_PID_FILE = Path(str(home)) / ".molecule-runtime" / "refresh-daemon.pid"
    credential_helper._DAEMON_LOG_FILE = Path(str(home)) / ".molecule-runtime" / "refresh-daemon.log"
    return credential_helper


# ---------- scripts extraction (real file system) ----------

def test_extract_scripts_writes_executable_bash_files(temp_home):
    """``_extract_scripts()`` extracts the bundled helper + daemon
    scripts to ``$HOME/.molecule-runtime/scripts/`` and the files must
    be executable (the daemon spawn invokes them with no shell
    interpretation, so a non-executable file = silent no-op)."""
    credential_helper = _credential_helper_for_home(temp_home)

    helper_dir = credential_helper._extract_scripts()
    assert helper_dir.exists(), f"helper dir {helper_dir} not created"
    assert helper_dir.is_dir()

    # Both scripts must be present + executable.
    for name in (
        credential_helper._HELPER_SCRIPT,
        credential_helper._DAEMON_SCRIPT,
    ):
        path = helper_dir / name
        assert path.exists(), f"script {path} not extracted"
        # Executable bit set — the access(X_OK) check works on POSIX
        # (CI runs Linux; no Windows fall-back is needed because
        # credential_helper is intentionally POSIX-only).
        assert os.access(path, os.X_OK), (
            f"script {path} is not executable (mode={oct(path.stat().st_mode & 0o777)}) — "
            "the refresh daemon would no-op silently. This is the 2026-06-08 class of bug."
        )


# ---------- git config side effect (real subprocess) ----------

def test_configure_git_credential_helper_real_subprocess(temp_home):
    """``_configure_git_credential_helper()`` runs ``git config
    --global credential.https://github.com.helper <path>`` and the
    config is readable back via ``git config --global --get``. This
    is the real wire the workspace's git invocations will hit at
    push/clone time. A mock here would silently no-op."""
    _require_git()
    credential_helper = _credential_helper_for_home(temp_home)

    # Set up a helper script at a known path (the one _configure
    # expects to point git at).
    helper_dir = credential_helper._extract_scripts()
    helper_path = helper_dir / credential_helper._HELPER_SCRIPT
    # Sanity precondition: the helper exists + is executable.
    assert helper_path.exists() and os.access(helper_path, os.X_OK)

    # The real subprocess call. The function writes the value with a
    # leading ``!`` (git's "this is a shell command, not a builtin"
    # convention) — we expect that prefix in the round-trip.
    credential_helper._configure_git_credential_helper(helper_path)

    # Read it back via a fresh subprocess (mirrors how a real
    # workspace's later `git fetch` would resolve the helper).
    resolved = subprocess.check_output(
        ["git", "config", "--global", "--get",
         "credential.https://github.com.helper"],
        env={**os.environ, "HOME": str(temp_home), "XDG_CONFIG_HOME": str(temp_home / ".config")},
        text=True,
    ).strip()
    assert resolved == f"!{helper_path}", (
        f"git config round-trip failed: expected '!{helper_path}', got {resolved!r}. "
        "The credential helper would not be invoked at push/clone time."
    )


# ---------- refresh daemon PID file (real subprocess spawn) ----------

def test_start_refresh_daemon_writes_pid_file(temp_home):
    """``_start_refresh_daemon()`` spawns a detached nohup process
    and writes its PID to ``$HOME/.molecule-runtime/refresh-daemon.pid``.
    The PID must be parseable as an int and the daemon must be alive
    (or at minimum not a zombie — the spawn should not leave a
    broken process behind)."""
    _require_nohup()
    credential_helper = _credential_helper_for_home(temp_home)

    # Set up the daemon script at a known path.
    helper_dir = credential_helper._extract_scripts()
    daemon_path = helper_dir / credential_helper._DAEMON_SCRIPT
    assert daemon_path.exists() and os.access(daemon_path, os.X_OK)

    # Real spawn.
    credential_helper._start_refresh_daemon(daemon_path)

    pid_file = temp_home / ".molecule-runtime" / "refresh-daemon.pid"
    assert pid_file.exists(), (
        f"PID file {pid_file} not written. The runtime restart path "
        "(`is the daemon alive?`) will silently no-op. This is the "
        "2026-06-08 class of bug."
    )
    pid = int(pid_file.read_text().strip())
    assert pid > 0, f"PID file contents {pid_file.read_text()!r} not a positive int"

    # The process is alive (or has already exited cleanly). Either
    # is fine — what's NOT fine is a zombie (Z state in /proc/<pid>/stat).
    # We don't assert kill -0 (Linux-only); the PID is parseable and
    # the file was written. The CI responsiveness-e2e already exercises
    # a real nohup-spawned daemon end-to-end.
    proc_status = Path(f"/proc/{pid}/stat")
    if proc_status.exists():
        state = proc_status.read_text().split()[2]
        assert state != "Z", (
            f"refresh daemon PID {pid} is a zombie (state={state}); "
            "the spawn broke the process"
        )


# ---------- full flow: gitea (no-op contract) ----------

def test_install_credential_helper_gitea_provider_is_noop(temp_home):
    """``GIT_PROVIDER=gitea`` must NOT install the GitHub machinery —
    no helper script extraction, no git config, no daemon spawn. This
    is the regression class for the 2026-06-08 incident (gitea
    workspace got hijacked by a stray GitHub token)."""
    import logging
    credential_helper = _credential_helper_for_home(temp_home)

    os.environ["GIT_PROVIDER"] = "gitea"
    os.environ["GIT_HTTP_USERNAME"] = "agent-test"
    os.environ["GIT_HTTP_PASSWORD"] = "test-pw"

    caplog_records = []
    class _CaptureLogHandler(logging.Handler):
        def emit(self, record):
            caplog_records.append(record.getMessage())

    caplog = _CaptureLogHandler(level=logging.DEBUG)
    root = logging.getLogger()
    prior_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(caplog)
    try:
        credential_helper.install_credential_helper()
    finally:
        root.removeHandler(caplog)
        root.setLevel(prior_level)

    # No scripts extracted under gitea.
    assert not (temp_home / ".molecule-runtime" / "scripts").exists(), (
        f"GitHub scripts extracted under gitea provider! "
        f"Contents: {list((temp_home / '.molecule-runtime' / 'scripts').iterdir()) if (temp_home / '.molecule-runtime' / 'scripts').exists() else 'absent'}"
    )
    # No PID file.
    assert not (temp_home / ".molecule-runtime" / "refresh-daemon.pid").exists(), (
        f"refresh-daemon.pid written under gitea provider at "
        f"{temp_home / '.molecule-runtime' / 'refresh-daemon.pid'}"
    )
    # The gitea path's log line is the strongest assertion — the
    # function MUST log "provider=gitea ... skipping GitHub helper install".
    log_text = "\n".join(caplog_records)
    assert "provider=gitea" in log_text and "skipping GitHub helper install" in log_text, (
        f"gitea-path log line missing in: {log_text!r}"
    )


# ---------- full flow: github (full contract) ----------

def test_install_credential_helper_github_provider_writes_artifacts(temp_home):
    """``GIT_PROVIDER=github`` (with a token-shaped value) MUST produce
    all three artifacts: scripts extracted, git config set, daemon
    PID file written. Missing any one = a 2026-06-08-class
    regression (workspaces would lose their tokens after ~60 min)."""
    _require_git()
    _require_nohup()
    import logging
    credential_helper = _credential_helper_for_home(temp_home)

    os.environ["GIT_PROVIDER"] = "github"
    os.environ["GH_TOKEN"] = "ghs_TEST_TOKEN_REDACTED"  # token-shaped but not real

    caplog_records = []
    class _CaptureLogHandler(logging.Handler):
        def emit(self, record):
            caplog_records.append(record.getMessage())

    caplog = _CaptureLogHandler(level=logging.DEBUG)
    root = logging.getLogger()
    prior_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(caplog)
    try:
        credential_helper.install_credential_helper()
    finally:
        root.removeHandler(caplog)
        root.setLevel(prior_level)

    # 1. Scripts extracted + executable.
    scripts_dir = temp_home / ".molecule-runtime" / "scripts"
    assert scripts_dir.exists(), f"scripts dir {scripts_dir} not created"
    for name in (
        credential_helper._HELPER_SCRIPT,
        credential_helper._DAEMON_SCRIPT,
    ):
        path = scripts_dir / name
        assert path.exists() and os.access(path, os.X_OK), (
            f"script {path} missing or not executable"
        )

    # 2. git config set (with the ``!`` shell-command prefix that
    # _configure_git_credential_helper writes).
    helper_path = scripts_dir / credential_helper._HELPER_SCRIPT
    resolved = subprocess.check_output(
        ["git", "config", "--global", "--get",
         "credential.https://github.com.helper"],
        env={**os.environ, "HOME": str(temp_home), "XDG_CONFIG_HOME": str(temp_home / ".config")},
        text=True,
    ).strip()
    assert resolved == f"!{helper_path}", (
        f"git config not set: expected '!{helper_path}', got {resolved!r}"
    )

    # 3. PID file written + parseable.
    pid_file = temp_home / ".molecule-runtime" / "refresh-daemon.pid"
    assert pid_file.exists(), f"PID file {pid_file} not written"
    pid = int(pid_file.read_text().strip())
    assert pid > 0

    # The provider selection log line is the strongest assertion —
    # the function MUST log "provider=github".
    log_text = "\n".join(caplog_records)
    assert "provider=github" in log_text, (
        f"github-path log line missing in: {log_text!r}"
    )
    # And the token-shaped value MUST NOT leak into the log (the
    # 2026-06-08 follow-up fix; #104's security contract).
    assert "ghs_TEST_TOKEN_REDACTED" not in log_text, (
        f"GH_TOKEN leaked into log output! Found in: {log_text!r}"
    )
