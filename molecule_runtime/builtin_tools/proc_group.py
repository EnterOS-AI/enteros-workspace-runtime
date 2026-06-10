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


def _killpg(pgid: int | None, sig: int, leader_pid: int | None = None) -> bool:
    """Send ``sig`` to ``pgid``. Best-effort.

    Returns True if a signal was delivered, False if the group was already
    gone. Falls back to signalling just the leader if the group send fails
    with a non-"no such process" OSError (e.g. the child never managed
    setsid).
    """
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
        if leader_pid is None:
            return False
        # Last resort: signal only the leader so we at least try.
        try:
            os.kill(leader_pid, sig)
            return True
        except (ProcessLookupError, OSError):
            return False


def _signal_group(proc, sig: int) -> bool:
    """Send ``sig`` to the process group led by ``proc``. Best-effort.

    Returns True if a signal was delivered, False if the process/group was
    already gone. Falls back to signalling just the leader if the group
    send fails (e.g. the child never managed setsid).
    """
    return _killpg(_pgid_for(proc), sig, getattr(proc, "pid", None))


def _group_alive(pgid: int | None) -> bool:
    """Is any process still in process-group ``pgid``?

    Uses ``killpg(pgid, 0)`` — the null-signal liveness probe. Returns False
    only once the group is fully empty (``ProcessLookupError``). A member that
    was killed but not yet *reaped* (a zombie) still keeps the group "alive"
    here, which is intentional: the reap loop in
    :func:`terminate_process_group` keeps draining via
    :func:`_reap_reparented_zombies` until the zombie is actually collected
    and the group truly empties.
    """
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM etc. — something is there, treat as alive.
        return True


def _reap_reparented_zombies(pgid: int) -> None:
    """Reap zombie children of process-group ``pgid`` re-parented to us.

    When the group leader (``bash -c ...``) is killed, the grandchildren it
    spawned are re-parented to *this* process. If this process happens to be
    the container PID 1 / a subreaper (common for agent workspaces and CI
    runners), nothing else will ``wait()`` for those grandchildren, so even
    though SIGKILL terminated them they linger as ZOMBIES — and a zombie pid
    still answers ``os.kill(pid, 0)`` without ``ProcessLookupError``, i.e. it
    looks "alive". Draining them here makes the kill actually observable and
    prevents a zombie leak.

    We scope reaping to OUR group: each exited child is PEEKED with
    ``os.waitid(..., WNOWAIT)`` (which inspects without reaping) and only
    actually collected with ``waitpid`` if its pgid matches ``pgid`` (or its
    group is already gone, meaning it was one of ours). This avoids stealing
    an unrelated concurrent subprocess's child from another asyncio task in
    the same process. Foreign exited children are remembered and skipped so we
    don't spin on them. ``WNOHANG`` means we never block; no-child / pid 0 /
    only-foreign-left ends the loop.

    NOTE: ``waitpid(-1, WNOWAIT)`` is EINVAL on Linux — the non-destructive
    peek must go through ``waitid``. ``waitid`` may be absent on some
    platforms; we degrade to a plain ``waitpid`` drain there.
    """
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        # No non-destructive peek available — reap directly. Safe here
        # because the asyncio-owned leader was already collected above.
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except (ChildProcessError, OSError):
                return
            if pid == 0:
                return
    skip: set[int] = set()
    while True:
        try:
            info = waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError):
            return  # no children left to reap
        if info is None:
            return  # children exist but none have exited yet
        pid = info.si_pid
        if pid == 0 or pid in skip:
            return  # nothing of ours left
        # Decide whether this exited child belongs to our group.
        try:
            ours = os.getpgid(pid) == pgid
        except ProcessLookupError:
            # Group/pid info already gone — it was a member of the group we
            # just killed; safe to collect.
            ours = True
        except OSError:
            ours = False
        if not ours:
            skip.add(pid)  # leave it for its owner; don't re-peek forever
            continue
        try:
            os.waitpid(pid, 0)  # actually reap it now
        except ChildProcessError:
            pass


async def terminate_process_group(
    proc,
    grace_s: float | None = None,
) -> None:
    """SIGTERM the whole group, wait ``grace_s``, then SIGKILL the group.

    Robust to a child that ignores SIGTERM AND to a fast-dying leader that
    leaves a backgrounded grandchild behind: escalation is driven against the
    GROUP, not the leader, and after SIGKILL we both ``await proc.wait()`` the
    leader and drain any re-parented grandchildren so no zombie is left (a
    zombie would otherwise still answer ``os.kill(pid, 0)`` as if alive).
    Never raises — termination is best-effort cleanup.

    ``grace_s`` defaults to the module-level ``SIGTERM_GRACE_S`` resolved at
    CALL time (so tests / late env changes take effect).

    Works for both ``asyncio.subprocess.Process`` (awaitable ``wait()``)
    and is tolerant of handles that have already exited.
    """
    if grace_s is None:
        grace_s = SIGTERM_GRACE_S

    # Resolve the process-GROUP id ONCE, up front — while the leader is still
    # alive so ``getpgid`` works. We escalate against this group for the rest
    # of the routine, NOT against the leader: the leader (``bash -c ...``) is
    # routinely the first to die on SIGTERM, but the grandchildren it spawned
    # are the orphans we actually care about. Gating escalation on the leader
    # exiting (the old behaviour) let a fast-dying leader skip the SIGKILL and
    # leave a backgrounded grandchild running forever.
    pgid = _pgid_for(proc)
    loop = asyncio.get_running_loop()
    leader_pid = getattr(proc, "pid", None)

    if pgid is None:
        # Never started / no pid — nothing to signal.
        return

    # SIGTERM the whole group (graceful). Even if the group is already gone
    # this is a harmless no-op.
    _killpg(pgid, signal.SIGTERM, leader_pid)

    # Grace period: poll the GROUP (not just the leader) for a clean exit.
    # We do NOT reap here — the leader is owned by asyncio's child watcher,
    # so reaping is deferred until after ``proc.wait()`` below. Escalate as
    # soon as the whole group is gone.
    deadline = loop.time() + grace_s
    while loop.time() < deadline:
        if not _group_alive(pgid):
            break
        await asyncio.sleep(0.05)

    # Escalate to SIGKILL on the whole group unconditionally — anything that
    # ignored or out-raced the SIGTERM dies here. SIGKILL is uncatchable, so
    # after this the only thing left to do is reap.
    _killpg(pgid, signal.SIGKILL, leader_pid)

    # Reap the leader so we don't leave a zombie. Bounded wait so a wedged
    # wait() can't itself re-wedge us. This MUST happen before we drain the
    # group with waitpid(-1), so we don't steal the leader from its watcher.
    waiter = getattr(proc, "wait", None)
    if waiter is not None and asyncio.iscoroutinefunction(waiter):
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError, Exception):
            pass

    # Drain re-parented grandchildren that SIGKILL just terminated. Without
    # this they linger as zombies (when we are the container PID 1 / a
    # subreaper) and still answer ``os.kill(pid, 0)`` as if alive. Poll until
    # the group is truly empty, bounded so a stuck reap can't re-wedge us.
    reap_deadline = loop.time() + 2.0
    while loop.time() < reap_deadline:
        _reap_reparented_zombies(pgid)
        if not _group_alive(pgid):
            break
        await asyncio.sleep(0.02)
