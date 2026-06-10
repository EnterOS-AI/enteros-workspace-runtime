"""Process-group-aware subprocess termination for bounded tool execution.

Agent-Liveness RFC, Layer 1 (A1).

The problem with ``proc.kill()`` / ``proc.terminate()`` on an asyncio
subprocess is that it signals ONLY the process leader. A shell tool runs
``bash -c '<cmd>'``, and ``<cmd>`` routinely fork-execs grandchildren
(``npx vercel`` -> node -> a deploy worker; ``npm`` -> a build script).
Killing only the ``bash`` leader leaves those grandchildren ORPHANED and
still holding the pipe — so ``proc.communicate()`` can still hang, and we
leak runaway processes.

The fix is POSIX process groups:

  * Spawn the child in its OWN session/group via ``start_new_session=True``
    (which does ``setsid()`` in the child, making it a process-group
    leader whose pgid == pid).
  * On timeout / cancel, signal the WHOLE group with ``os.killpg(pgid, …)``
    so every descendant gets it.
  * Escalate: SIGTERM first (graceful — lets the child flush buffers and
    run cleanup), then after a short grace SIGKILL the group for anything
    that ignored SIGTERM.

This module is the single home for that escalation so both the timeout
path (sandbox tool) and any future cancel path can share identical,
well-tested semantics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

# Seconds to wait after SIGTERM before escalating to SIGKILL. Kept short:
# this only runs AFTER a tool already blew its (300s default) budget, so
# we don't want to add meaningful extra latency, but we do want to give a
# well-behaved child a chance to clean up.
SIGTERM_GRACE_S = float(os.environ.get("MOLECULE_TOOL_KILL_GRACE_S", "3"))


def _pgid_for(proc) -> int | None:
    """Best-effort process-group id for an asyncio/Popen process handle.

    Returns None if the process has no pid (never started) or already
    reaped, or if the platform doesn't support process groups.
    """
    pid = getattr(proc, "pid", None)
    if not pid:
        return None
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, OSError):
        # Process already gone, or no getpgid (non-POSIX). Fall back to the
        # pid itself — under start_new_session the pgid equals the pid, so
        # killpg(pid) targets the same group.
        return pid


def _signal_group(proc, sig: int) -> bool:
    """Send ``sig`` to the process group led by ``proc``. Best-effort.

    Returns True if a signal was delivered, False if the process/group was
    already gone. Falls back to signalling just the leader if the group
    send fails (e.g. the child never managed setsid).
    """
    pgid = _pgid_for(proc)
    if pgid is None:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.debug("proc_group: killpg(%s, %s) failed: %s; trying leader",
                     pgid, sig, exc)
        # Last resort: signal only the leader so we at least try.
        try:
            os.kill(proc.pid, sig)
            return True
        except (ProcessLookupError, OSError):
            return False


async def terminate_process_group(
    proc,
    grace_s: float | None = None,
) -> None:
    """SIGTERM the whole group, wait ``grace_s``, then SIGKILL the group.

    Robust to a child that ignores SIGTERM. After SIGKILL we ``await
    proc.wait()`` (when available) so the leader is reaped and no zombie is
    left. Never raises — termination is best-effort cleanup.

    ``grace_s`` defaults to the module-level ``SIGTERM_GRACE_S`` resolved at
    CALL time (so tests / late env changes take effect).

    Works for both ``asyncio.subprocess.Process`` (awaitable ``wait()``)
    and is tolerant of handles that have already exited.
    """
    if grace_s is None:
        grace_s = SIGTERM_GRACE_S
    # Already exited? Nothing to do.
    if getattr(proc, "returncode", None) is not None:
        return

    delivered = _signal_group(proc, signal.SIGTERM)
    if not delivered:
        # Group already gone.
        return

    # Grace period: poll for a clean SIGTERM exit before escalating.
    deadline = asyncio.get_running_loop().time() + grace_s
    while asyncio.get_running_loop().time() < deadline:
        if getattr(proc, "returncode", None) is not None:
            return
        await asyncio.sleep(0.05)

    # Still alive after grace — escalate to SIGKILL on the whole group.
    _signal_group(proc, signal.SIGKILL)

    # Reap the leader so we don't leave a zombie. Bounded wait so a wedged
    # wait() can't itself re-wedge us.
    waiter = getattr(proc, "wait", None)
    if waiter is not None and asyncio.iscoroutinefunction(waiter):
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError, Exception):
            pass
