"""Code sandbox tool for safe code execution.

Executes code in an isolated environment. Three backends are supported:

subprocess (default)
    Runs code locally via asyncio subprocess with a hard timeout.
    Best for Tier 1/2 agents where run_code is lightly used and the
    workspace container itself is the isolation boundary.

docker
    Throwaway Docker-in-Docker container: network disabled, memory capped,
    read-only filesystem. Requires Docker socket access inside the container.
    Best for Tier 3 on-prem deployments.

e2b
    Cloud-hosted microVM sandbox via E2B (https://e2b.dev).
    No local Docker required — code runs in E2B's isolated cloud VMs.
    Supports Python and JavaScript.
    Requires:
      - e2b-code-interpreter Python package (pinned in requirements.txt)
      - E2B_API_KEY workspace secret (set via canvas Secrets panel or API)
    Best for hosted/cloud Molecule AI deployments.

Backend is selected via the SANDBOX_BACKEND env var, which the provisioner
sets from config.yaml → sandbox.backend. Default: "subprocess".

Bounded tool execution (Agent-Liveness RFC, Layer 1 / A1)
=========================================================
Every local subprocess (subprocess + docker backends) runs under a HARD
per-call timeout (``MOLECULE_TOOL_TIMEOUT_S``, default 300s) and, on
timeout, the WHOLE process group is killed (SIGTERM → grace → SIGKILL),
not just the leader — so a ``bash -c 'npx vercel'`` that forks a node
child and blocks on a TTY prompt can never wedge the agent. On timeout a
structured ``{"error": "tool_timeout", ...}`` result is returned so the
agent loop CONTINUES rather than hanging. Shell commands are additionally
run NON-INTERACTIVE (stdin=/dev/null + flag/env hardening for known
interactive CLIs) so they fail fast instead of waiting on a prompt.
"""

import asyncio
import logging
import os
import subprocess
import tempfile

from langchain_core.tools import tool

from molecule_runtime.builtin_tools.command_preprocessing import (
    NONINTERACTIVE_ENV,
    make_noninteractive,
    stdin_should_be_closed,
)
from molecule_runtime.builtin_tools.proc_group import terminate_process_group
from molecule_runtime.runtime_inbox import (
    clear_active_subprocess,
    register_active_subprocess,
)

logger = logging.getLogger(__name__)

SANDBOX_BACKEND = os.environ.get("SANDBOX_BACKEND", "subprocess")
SANDBOX_MEMORY_LIMIT = os.environ.get("SANDBOX_MEMORY_LIMIT", "256m")
MAX_OUTPUT = 10_000

# Languages whose code is executed as a shell command line (vs. a source
# file). These get non-interactive command preprocessing applied.
_SHELL_LANGUAGES = frozenset({"shell", "bash"})

# Default hard per-tool-call timeout, in seconds (A1). Overridable via env.
_DEFAULT_TOOL_TIMEOUT_S = 300


def _tool_timeout_s() -> int:
    """Hard per-tool-call timeout in seconds (A1).

    Sourced from ``MOLECULE_TOOL_TIMEOUT_S`` (default 300). A legacy
    ``SANDBOX_TIMEOUT`` override is still honoured when explicitly set, so
    existing deployments that tuned it keep their value, but the A1 knob
    takes precedence when present. Malformed values fall back to the
    default rather than crashing the tool.
    """
    raw = os.environ.get("MOLECULE_TOOL_TIMEOUT_S")
    if raw is None:
        raw = os.environ.get("SANDBOX_TIMEOUT")
    if raw is None:
        return _DEFAULT_TOOL_TIMEOUT_S
    try:
        val = int(raw)
        return val if val > 0 else _DEFAULT_TOOL_TIMEOUT_S
    except (TypeError, ValueError):
        logger.warning(
            "Invalid tool timeout %r; falling back to %ds",
            raw, _DEFAULT_TOOL_TIMEOUT_S,
        )
        return _DEFAULT_TOOL_TIMEOUT_S


# E2B kernel names differ from internal language names.
_E2B_KERNEL_MAP = {
    "python": "python3",
    "javascript": "js",
    "js": "js",
}


@tool
async def run_code(code: str, language: str = "python") -> dict:
    """Execute code in an isolated sandbox and return the output.

    Args:
        code: The code to execute.
        language: Programming language — python, javascript, or shell.
                  The e2b backend supports python and javascript only.
    """
    if SANDBOX_BACKEND == "docker":
        return await _run_docker(code, language)
    elif SANDBOX_BACKEND == "e2b":
        return await _run_e2b(code, language)
    else:
        return await _run_subprocess(code, language)


def _noninteractive_env() -> dict:
    """Base env + non-interactive hardening for interactive CLIs (A1)."""
    env = dict(os.environ)
    env.update(NONINTERACTIVE_ENV)
    return env


async def _run_subprocess(code: str, language: str) -> dict:
    """Fallback: run code in a subprocess with a hard timeout (A1).

    Spawns the child in its own session (``start_new_session=True``) so a
    timeout can kill the WHOLE process group, not just the leader. Shell
    commands are made non-interactive (flag/env hardening + stdin closed)
    so an interactive CLI can't block forever on a prompt.
    """
    cmd_map = {
        "python": ["python3", "-c"],
        "javascript": ["node", "-e"],
        "shell": ["sh", "-c"],
        "bash": ["bash", "-c"],
    }

    cmd_prefix = cmd_map.get(language)
    if not cmd_prefix:
        return {"error": f"Unsupported language: {language}", "exit_code": -1}

    timeout_s = _tool_timeout_s()

    # Non-interactive preprocessing for shell/bash command lines: inject
    # --yes for known CLIs and apply env hardening so prompts fail fast.
    payload = code
    env = None
    if language in _SHELL_LANGUAGES:
        payload, is_interactive = make_noninteractive(code)
        if is_interactive:
            env = _noninteractive_env()

    # Close stdin so anything that reads a TTY prompt sees EOF immediately
    # rather than blocking. /dev/null is the cheapest universal guard.
    stdin = subprocess.DEVNULL if stdin_should_be_closed() else None

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_prefix, payload,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # A1: own session/process-group so the timeout path can reap
            # the entire descendant tree (npx -> node -> worker), not just
            # the bash leader.
            start_new_session=True,
            env=env,
        )
        # Task #377 — register on the per-context inbox so canvas
        # "Stop All" (A2A tasks/cancel) can SIGTERM us mid-flight.
        register_active_subprocess(proc)

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )

        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT],
            "stderr": stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT],
            "language": language,
            "backend": "subprocess",
        }
    except asyncio.TimeoutError:
        # A1: kill the whole process group, escalating SIGTERM -> SIGKILL,
        # so no orphaned child survives. Then return a STRUCTURED result so
        # the agent loop continues instead of blocking.
        if proc is not None:
            await terminate_process_group(proc)
        return {
            "error": "tool_timeout",
            "detail": f"{language} command killed after {timeout_s}s",
            "exit_code": -1,
            "language": language,
            "backend": "subprocess",
        }
    except Exception as e:
        return {"error": str(e), "exit_code": -1}
    finally:
        # Always clear our handle so a later unrelated cancel doesn't
        # SIGTERM a recycled PID. Identity-match inside clear_* ensures
        # we don't stomp another tool's registration.
        if proc is not None:
            clear_active_subprocess(proc)


async def _run_docker(code: str, language: str) -> dict:
    """Run code in a throwaway Docker container via mounted temp file."""
    image_map = {
        "python": ("python:3.11-slim", ["python3", "/sandbox/code.py"]),
        "javascript": ("node:20-slim", ["node", "/sandbox/code.js"]),
        "shell": ("alpine:3.18", ["sh", "/sandbox/code.sh"]),
        "bash": ("alpine:3.18", ["sh", "/sandbox/code.sh"]),
    }

    entry = image_map.get(language)
    if not entry:
        return {"error": f"Unsupported language: {language}", "exit_code": -1}

    image, run_cmd = entry
    timeout_s = _tool_timeout_s()
    code_file = None
    proc = None

    try:
        # Write code to temp file — avoids shell metacharacter injection
        ext = {"python": ".py", "javascript": ".js", "shell": ".sh", "bash": ".sh"}.get(language, ".txt")
        fd, code_file = tempfile.mkstemp(suffix=ext, prefix="sandbox_")
        with os.fdopen(fd, "w") as f:
            f.write(code)

        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", SANDBOX_MEMORY_LIMIT,
            "--cpus", "0.5",
            "--read-only",
            "--tmpfs", "/tmp:size=32m",
            "-v", f"{code_file}:/sandbox/code{ext}:ro",
            image,
        ] + run_cmd

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # A1: own process-group so a timeout reaps the docker-run
            # wrapper (and, with --rm, the daemon tears down the container).
            start_new_session=True,
        )
        # Task #377 — register on the per-context inbox so canvas
        # "Stop All" propagates SIGTERM to the docker-run wrapper
        # (which docker forwards to the container with --init).
        register_active_subprocess(proc)

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )

        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT],
            "stderr": stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT],
            "language": language,
            "backend": "docker",
            "image": image,
        }
    except asyncio.TimeoutError:
        if proc is not None:
            await terminate_process_group(proc)
        return {
            "error": "tool_timeout",
            "detail": f"{language} command (docker) killed after {timeout_s}s",
            "exit_code": -1,
            "language": language,
            "backend": "docker",
        }
    except Exception as e:
        return {"error": str(e), "exit_code": -1}
    finally:
        if proc is not None:
            clear_active_subprocess(proc)
        if code_file:
            try:
                os.unlink(code_file)
            except OSError:
                pass


async def _run_e2b(code: str, language: str) -> dict:
    """Run code in an E2B cloud microVM sandbox.

    Requires the e2b-code-interpreter package and an E2B_API_KEY secret.
    Each call creates a fresh sandbox, runs the code, and destroys the sandbox.
    Sandbox lifetime is bounded by MOLECULE_TOOL_TIMEOUT_S seconds.

    Supported languages: python, javascript.
    """
    # Import lazily so the package is only required when the e2b backend is
    # actually configured — other backends work without it installed.
    try:
        from e2b_code_interpreter import Sandbox
    except ImportError:
        return {
            "error": (
                "e2b-code-interpreter is not installed. "
                "Add it to requirements.txt or switch to the docker/subprocess backend."
            ),
            "exit_code": -1,
        }

    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        return {
            "error": (
                "E2B_API_KEY is not set. "
                "Add it as a workspace secret via the canvas Secrets panel or platform API."
            ),
            "exit_code": -1,
        }

    kernel = _E2B_KERNEL_MAP.get(language)
    if kernel is None:
        return {
            "error": (
                f"Language '{language}' is not supported by the e2b backend. "
                "Supported: python, javascript."
            ),
            "exit_code": -1,
        }

    timeout_s = _tool_timeout_s()
    sandbox = None
    try:
        # Create a fresh sandbox for this execution.
        # timeout controls the sandbox lifetime in seconds.
        sandbox = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: Sandbox(api_key=api_key, timeout=timeout_s),
            ),
            timeout=timeout_s,
        )

        # Execute code and collect results.
        execution = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: sandbox.run_code(code, language=kernel),
            ),
            timeout=timeout_s,
        )

        # E2B returns a list of Result objects; collect text/error output.
        stdout_parts = []
        stderr_parts = []

        for result in execution.results:
            # result.text is the primary output (stdout equivalent)
            if hasattr(result, "text") and result.text:
                stdout_parts.append(str(result.text))
            # Some result types expose an error attribute
            if hasattr(result, "error") and result.error:
                stderr_parts.append(str(result.error))

        # Logs are stored separately in execution.logs
        if hasattr(execution, "logs"):
            logs = execution.logs
            if hasattr(logs, "stdout") and logs.stdout:
                stdout_parts.extend(logs.stdout)
            if hasattr(logs, "stderr") and logs.stderr:
                stderr_parts.extend(logs.stderr)

        combined_stdout = "".join(stdout_parts)[:MAX_OUTPUT]
        combined_stderr = "".join(stderr_parts)[:MAX_OUTPUT]

        # Treat any stderr output as a non-zero exit code (e2b doesn't expose
        # a numeric exit code at the sandbox level).
        exit_code = 1 if combined_stderr else 0

        return {
            "exit_code": exit_code,
            "stdout": combined_stdout,
            "stderr": combined_stderr,
            "language": language,
            "backend": "e2b",
        }

    except asyncio.TimeoutError:
        logger.warning("E2B sandbox timed out after %ds", timeout_s)
        return {
            "error": "tool_timeout",
            "detail": f"{language} code (e2b) killed after {timeout_s}s",
            "exit_code": -1,
            "language": language,
            "backend": "e2b",
        }
    except Exception as e:
        logger.exception("E2B sandbox error: %s", e)
        return {"error": str(e), "exit_code": -1}
    finally:
        # Always destroy the sandbox to avoid leaking E2B credits.
        if sandbox is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, sandbox.kill
                )
            except Exception:
                pass  # Best-effort cleanup
