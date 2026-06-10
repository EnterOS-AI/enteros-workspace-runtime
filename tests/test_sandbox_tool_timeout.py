"""Agent-Liveness RFC, Layer 1 (A1) — bounded tool execution.

Proves that a shell/subprocess tool can NEVER wedge the agent:

* A command that sleeps past the hard timeout is KILLED and returns the
  structured ``tool_timeout`` error (not a hang).
* The whole process GROUP is reaped — a grandchild spawned by the command
  does not survive as an orphan.
* A fast command is unaffected (normal result, no timeout).
* ``MOLECULE_TOOL_TIMEOUT_S`` controls the budget (default 300).
* Non-interactive command preprocessing injects flags / closes stdin for
  the known interactive CLIs (vercel/npm via npx, etc.) and leaves
  unrecognised commands untouched.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from molecule_runtime.builtin_tools import sandbox
from molecule_runtime.builtin_tools.command_preprocessing import (
    INTERACTIVE_CLIS,
    make_noninteractive,
    stdin_should_be_closed,
)


# ---------------------------------------------------------------------------
# Timeout + process-group kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_timeout_returns_structured_error_not_hang(monkeypatch):
    """A command that sleeps past the timeout is killed and returns
    {"error": "tool_timeout", ...} — the call RETURNS instead of hanging."""
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "1")

    start = time.monotonic()
    result = await sandbox._run_subprocess("sleep 30", "bash")
    elapsed = time.monotonic() - start

    assert result["error"] == "tool_timeout"
    assert "killed after 1s" in result["detail"]
    assert result["exit_code"] == -1
    # It returned promptly (timeout 1s + a few s SIGTERM grace), nowhere
    # near the 30s sleep — i.e. it did NOT hang on the child.
    assert elapsed < 15, f"took {elapsed:.1f}s — looks like it hung"


@pytest.mark.asyncio
async def test_timeout_reaps_whole_process_group_no_orphan(monkeypatch):
    """The grandchild a bash command spawns is reaped on timeout — no orphan.

    The command writes the grandchild's PID to a temp file, then the
    grandchild sleeps far past the timeout. After the tool times out, that
    PID must be dead (process-group kill), not a survivor.
    """
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "1")

    pidfile = os.path.join(
        sandbox.tempfile.gettempdir(), f"a1_orphan_{os.getpid()}_{time.time_ns()}"
    )
    # Background a child that sleeps 60s and record ITS pid (the grandchild
    # of our bash leader). The leader then waits on it.
    cmd = f"sleep 60 & echo $! > {pidfile}; wait"

    try:
        result = await sandbox._run_subprocess(cmd, "bash")
        assert result["error"] == "tool_timeout"

        # Give the SIGTERM->SIGKILL escalation a moment to finish reaping.
        await asyncio.sleep(0.2)

        with open(pidfile) as f:
            child_pid = int(f.read().strip())

        # The grandchild must be gone. os.kill(pid, 0) raises
        # ProcessLookupError when the pid no longer exists.
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        try:
            os.unlink(pidfile)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_child_ignoring_sigterm_is_sigkilled(monkeypatch):
    """A child that traps/ignores SIGTERM is still killed (SIGKILL escalation)."""
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "1")
    # Shorten the SIGTERM grace so the test is fast. terminate_process_group
    # reads its default grace from the module constant at call time.
    from molecule_runtime.builtin_tools import proc_group
    monkeypatch.setattr(proc_group, "SIGTERM_GRACE_S", 0.5, raising=False)

    pidfile = os.path.join(
        sandbox.tempfile.gettempdir(), f"a1_trap_{os.getpid()}_{time.time_ns()}"
    )
    # trap '' TERM => ignore SIGTERM; only SIGKILL can stop it.
    cmd = f"trap '' TERM; echo $$ > {pidfile}; sleep 60"

    try:
        result = await sandbox._run_subprocess(cmd, "bash")
        assert result["error"] == "tool_timeout"

        await asyncio.sleep(0.3)
        with open(pidfile) as f:
            leader_pid = int(f.read().strip())
        with pytest.raises(ProcessLookupError):
            os.kill(leader_pid, 0)
    finally:
        try:
            os.unlink(pidfile)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_fast_command_unaffected(monkeypatch):
    """A fast command returns its normal result, no timeout error."""
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "10")
    result = await sandbox._run_subprocess("echo hello-a1", "bash")
    assert "error" not in result
    assert result["exit_code"] == 0
    assert "hello-a1" in result["stdout"]
    assert result["backend"] == "subprocess"


@pytest.mark.asyncio
async def test_python_language_still_works(monkeypatch):
    """Non-shell languages are unaffected by the preprocessing path."""
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "10")
    result = await sandbox._run_subprocess("print('hi-from-py')", "python")
    assert result["exit_code"] == 0
    assert "hi-from-py" in result["stdout"]


# ---------------------------------------------------------------------------
# Timeout knob
# ---------------------------------------------------------------------------


def test_tool_timeout_default_is_300(monkeypatch):
    monkeypatch.delenv("MOLECULE_TOOL_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SANDBOX_TIMEOUT", raising=False)
    assert sandbox._tool_timeout_s() == 300


def test_tool_timeout_env_override(monkeypatch):
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "42")
    assert sandbox._tool_timeout_s() == 42


def test_tool_timeout_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "not-a-number")
    assert sandbox._tool_timeout_s() == 300


def test_tool_timeout_legacy_sandbox_timeout_honoured(monkeypatch):
    monkeypatch.delenv("MOLECULE_TOOL_TIMEOUT_S", raising=False)
    monkeypatch.setenv("SANDBOX_TIMEOUT", "55")
    assert sandbox._tool_timeout_s() == 55


def test_tool_timeout_new_knob_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "11")
    monkeypatch.setenv("SANDBOX_TIMEOUT", "999")
    assert sandbox._tool_timeout_s() == 11


# ---------------------------------------------------------------------------
# Non-interactive command preprocessing
# ---------------------------------------------------------------------------


def test_vercel_gets_yes_flag():
    out, interactive = make_noninteractive("vercel deploy")
    assert interactive is True
    assert "--yes" in out
    # Flag binds to the CLI, before the subcommand.
    assert out.split() == ["vercel", "--yes", "deploy"]


def test_npx_vercel_gets_yes_flag():
    out, interactive = make_noninteractive("npx vercel deploy --prod")
    assert interactive is True
    assert "--yes" in out
    assert out.split() == ["npx", "vercel", "--yes", "deploy", "--prod"]


def test_npm_gets_yes_flag():
    out, interactive = make_noninteractive("npm install left-pad")
    assert interactive is True
    assert out.split() == ["npm", "--yes", "install", "left-pad"]


def test_vercel_yes_flag_idempotent():
    out, interactive = make_noninteractive("vercel --yes deploy")
    assert interactive is True
    # No duplicate flag.
    assert out.split().count("--yes") == 1


def test_vercel_short_alias_not_duplicated():
    out, _ = make_noninteractive("npm -y install foo")
    assert "--yes" not in out  # -y already present, don't add --yes


def test_git_is_interactive_but_no_flag_injected():
    out, interactive = make_noninteractive("git push origin main")
    assert interactive is True
    # git gets env/stdin hardening, NOT a flag rewrite.
    assert out == "git push origin main"


def test_gh_is_interactive_but_no_flag_injected():
    out, interactive = make_noninteractive("gh pr create")
    assert interactive is True
    assert out == "gh pr create"


def test_unknown_command_untouched():
    out, interactive = make_noninteractive("ls -la /tmp")
    assert interactive is False
    assert out == "ls -la /tmp"


def test_python_oneliner_untouched():
    cmd = "python3 -c 'print(1)'"
    out, interactive = make_noninteractive(cmd)
    assert interactive is False
    assert out == cmd


def test_piped_interactive_cli_not_rewritten_but_flagged_interactive():
    """A compound/piped command keeps its exact text (no risky rewrite) but
    is still reported interactive so the caller applies env hardening."""
    cmd = "vercel ls | grep prod"
    out, interactive = make_noninteractive(cmd)
    assert interactive is True
    assert out == cmd  # unchanged — we don't rewrite compound commands


def test_unbalanced_quotes_left_untouched():
    cmd = 'echo "unterminated'
    out, interactive = make_noninteractive(cmd)
    assert interactive is False
    assert out == cmd


def test_absolute_path_cli_recognised():
    out, interactive = make_noninteractive("/usr/local/bin/vercel deploy")
    assert interactive is True
    assert "--yes" in out


def test_interactive_clis_set_contents():
    assert {"vercel", "npm", "gh", "git"} <= INTERACTIVE_CLIS


def test_stdin_should_be_closed_default():
    assert stdin_should_be_closed() is True


# ---------------------------------------------------------------------------
# stdin is actually closed for the child (anti-hang on a prompt)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdin_closed_command_reading_stdin_does_not_hang(monkeypatch):
    """A command that reads stdin gets EOF immediately (stdin=/dev/null),
    so it returns fast instead of blocking forever on input."""
    monkeypatch.setenv("MOLECULE_TOOL_TIMEOUT_S", "10")
    start = time.monotonic()
    # `cat` with no args reads stdin until EOF; with /dev/null it's instant.
    result = await sandbox._run_subprocess("cat", "bash")
    elapsed = time.monotonic() - start
    assert "error" not in result
    assert result["exit_code"] == 0
    assert elapsed < 5, "cat hung — stdin was not closed"
