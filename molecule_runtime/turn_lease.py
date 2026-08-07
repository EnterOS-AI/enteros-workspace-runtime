"""Turn lease — liveness signal for an in-flight autonomous turn.

MUST-FIX 1 of the mailbox-kernel design. A long turn that is *genuinely
working* (a subprocess tool churning for minutes, a claude-code session
streaming tool calls) must NOT be mistaken for a stalled one, and a turn that
has gone quiet with no tool activity should be declared stalled promptly. The
:class:`TurnLease` is that liveness signal: it is *touched* on ANY tool
activity and :meth:`expired` once the TTL elapses with no touch.

Relationship to the idle-cap
----------------------------
The executor already fails a turn when ``astream_events`` produces NO runtime
event for ``A2A_COMPLETION_IDLE_TIMEOUT_SECONDS`` (default 900s,
``a2a_executor.py``). The lease TTL is PINNED to that same cap by default so
the two COMPLEMENT rather than fight: the idle-cap watches the event stream,
the lease watches *tool* activity, and neither declares a stall earlier than
the other. Override with ``MOLECULE_TURN_LEASE_TTL_SECONDS`` for tests.

Feeders ("resets on ANY tool call")
-----------------------------------
A. Native runtime — ``a2a_executor`` calls :func:`touch_current` from
   ``on_tool_start`` / ``on_tool_end`` (next to ``AgencyTracker.on_tool_call``).
   LIVE.
B. Claude Code — its adapter exposes ``transcript_lines()``; a poller was
   designed to touch the lease when the tail advances (new jsonl lines under
   ``~/.claude``). **NEVER BUILT.** No poller exists, and ``ClaudeSDKExecutor``
   touches the lease by no other route either, so claude-code has NO feed. That
   is precisely why :func:`arm_turn_if_fed` refuses to arm it — see runtime#408
   and runtime#410, which tracks the one-line ping at its ``_report_tool_use``
   site, all that is needed to bring it up to codex/hermes parity.
C. codex / hermes — subprocess runtimes bump ``MOLECULE_TOOL_ACTIVITY_FILE``
   (see ``executor_helpers``) on every tool call and
   :func:`feed_from_activity_file` touches the lease when its mtime advances.
   Same pattern as ``DELEGATION_RESULTS_FILE``. LIVE, but ONLY since
   runtime#408: both writers no-op when that env var is unset, and it was
   exported exclusively by ``a2a_executor`` — which never runs on a subprocess
   flavour. The feed was therefore dead on exactly the runtimes it was written
   for. :func:`turn_liveness_scope` now exports it on every executor.
D. openclaw — subprocess-output liveness: any bytes on the child's stdout/err
   is a tool-activity proxy and touches the lease (its adapter's
   ``_communicate_touching_lease``). LIVE.

Arming, and why it is conditional
---------------------------------
A lease is only meaningful once :meth:`TurnLease.reset` has dated it to the
turn in flight. A lease that is armed but never fed is not a better signal than
an unarmed one — it is a worse one, because arming is what persuades a consumer
to believe it. :func:`arm_turn_if_fed` therefore arms only where a feed above
has actually been observed.

Process-scoped
--------------
The executor is constructed in the template adapter (outside this wheel), so
the lease is a PROCESS-GLOBAL singleton installed by the kernel wiring
(:mod:`molecule_runtime.kernel`) only when ``MOLECULE_MAILBOX_KERNEL`` is on.
When the kernel is off the global is ``None`` and every :func:`touch_current` /
:func:`feed_from_activity_file` call is a cheap no-op — so the touches added to
``a2a_executor`` change NOTHING in the proven default flow.

.. _turn-lease-concurrency-limit:

CONCURRENCY LIMIT — the lease describes ONE turn, and cannot say which
----------------------------------------------------------------------
Read this before adding a caller. The paragraph above is about INSTALLATION —
one lease object per process, created by the kernel wiring. This one is about
OCCUPANCY, which is a different and weaker guarantee:

* The lease is SINGLE-VALUED. It carries one ``_turn_start`` and one
  ``_last_touch``, and it carries no turn identity. Nothing in a snapshot says
  WHICH turn it describes.
* Touches are UNATTRIBUTED. Every feeder (native tool hooks, the activity-file
  watcher, openclaw's subprocess output) touches the same lease, so if two
  turns are in flight the activity of either keeps the lease of "the" turn
  fresh. A wedged turn overlapping a working one therefore reads alive.
* :func:`turn_liveness_scope` arms only on the OUTERMOST active scope
  (runtime#409), so an overlapping turn does NOT re-date the lease. That is the
  conservative choice, deliberately: ``reset()`` only ever moves the clocks
  FORWARD, so re-arming on a later turn erases the older turn's idle clock and
  its absolute-cap origin — which is precisely how a periodically-pinged
  workspace ends up with a wedged turn that is never reaped. Declining to
  re-arm cannot create that hole.
* What that costs, stated plainly: while two turns overlap, the lease stays
  dated to the OLDER one. A consumer reading it gets a conservative (older,
  more likely to be judged stalled) age for the newer turn. It does NOT get a
  falsely-fresh one. So the failure mode under overlap is an early verdict on
  the younger turn, never an unreapable one — the opposite direction from the
  bug, and the safe direction of the two.
* NOT guaranteed, and do not infer it: that a snapshot corresponds to the turn
  the reader dispatched. The scheduler's ``lease_is_attributable`` gate
  (``turn_age_seconds < elapsed``) is a NECESSARY condition, not a sufficient
  one — it rejects a never-armed lease, but it cannot distinguish two
  concurrent turns.

The real fix is a ``turn_id`` in the snapshot so a consumer can check the lease
describes ITS turn and treat a mismatch as "no signal". That is issue #408
point 3 / sdk#208: the snapshot field list is declared in the SDK
(``molecule_plugin/channel.py``) and mirrored into ``channel_sdk.py``, so
adding a field is an SDK-first chain that also bumps the scheduler plugin. It
is deliberately not done here. Until it lands, ONE in-flight turn per runtime
process is the assumption this module is honest under.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

#: Per-turn lease TTL override (seconds). Falls back to the idle-cap.
LEASE_TTL_ENV = "MOLECULE_TURN_LEASE_TTL_SECONDS"
#: The idle-cap the lease TTL is pinned to by default.
IDLE_CAP_ENV = "A2A_COMPLETION_IDLE_TIMEOUT_SECONDS"
_DEFAULT_IDLE_CAP_SECONDS = 900.0
#: Absolute per-turn wall-clock cap override (seconds). The UN-BYPASSABLE
#: backstop: a turn is ended once this elapses since turn-start EVEN IF the
#: activity file is being continuously touched. Activity touches extend
#: liveness up to the idle-cap TTL but NEVER past this cap. Defaults to a small
#: multiple of the lease TTL (which is itself pinned to the idle-cap) so a
#: genuinely-working long turn still gets a generous window, but a process that
#: keeps touching MOLECULE_TOOL_ACTIVITY_FILE can no longer keep a turn alive
#: forever (activity-file liveness bypass).
MAX_TURN_ENV = "MOLECULE_MAX_TURN_SECONDS"
#: Absolute cap default = this many idle-caps. Big enough that a real
#: multi-phase tool-running turn is never truncated, small enough that a runaway
#: is bounded.
_ABSOLUTE_CAP_IDLE_MULTIPLE = 4.0
#: Background activity-file watcher poll interval (seconds). Kept well under the
#: lease TTL (== idle-cap) so a live subprocess refreshes the lease many times
#: before the idle-cap boundary. Override for tests via the env below.
ACTIVITY_POLL_ENV = "MOLECULE_TOOL_ACTIVITY_POLL_SECONDS"
_DEFAULT_ACTIVITY_POLL_SECONDS = 5.0


def activity_poll_seconds() -> float:
    """Resolve the background activity-file watcher poll interval (seconds)."""
    raw = os.environ.get(ACTIVITY_POLL_ENV, "").strip()
    if raw:
        try:
            return max(1e-3, float(raw))
        except ValueError:
            pass
    return _DEFAULT_ACTIVITY_POLL_SECONDS


def default_ttl_seconds() -> float:
    """Resolve the lease TTL, pinned to the executor idle-cap by default.

    Precedence: ``MOLECULE_TURN_LEASE_TTL_SECONDS`` (explicit) ->
    ``A2A_COMPLETION_IDLE_TIMEOUT_SECONDS`` (the idle-cap it complements) ->
    900s. Always at least 1s so a mis-set 0 can't make every turn instantly
    "expired".
    """
    explicit = os.environ.get(LEASE_TTL_ENV, "").strip()
    if explicit:
        try:
            return max(1.0, float(explicit))
        except ValueError:
            pass
    cap_raw = os.environ.get(IDLE_CAP_ENV, "").strip()
    try:
        cap = float(cap_raw) if cap_raw else _DEFAULT_IDLE_CAP_SECONDS
    except ValueError:
        cap = _DEFAULT_IDLE_CAP_SECONDS
    return max(1.0, cap)


def _resolve_absolute_cap(ttl_seconds: float) -> float:
    """Resolve the absolute wall-clock cap for a lease whose TTL is ``ttl_seconds``.

    Precedence: ``MOLECULE_MAX_TURN_SECONDS`` (explicit) ->
    ``_ABSOLUTE_CAP_IDLE_MULTIPLE`` * ``ttl_seconds``. The explicit override is
    honored as-given (floored only at a tiny epsilon so a mis-set 0 can't make
    every turn instantly capped); tests set a SMALL cap to prove the bypass is
    closed quickly.
    """
    explicit = os.environ.get(MAX_TURN_ENV, "").strip()
    if explicit:
        try:
            return max(1e-6, float(explicit))
        except ValueError:
            pass
    return max(1e-6, _ABSOLUTE_CAP_IDLE_MULTIPLE * float(ttl_seconds))


def default_absolute_cap_seconds() -> float:
    """Resolve the absolute per-turn cap for the env-derived default TTL.

    ``MOLECULE_MAX_TURN_SECONDS`` wins; otherwise a small multiple of the
    idle-cap-pinned lease TTL. Exposed for the kernel wiring / tests.
    """
    return _resolve_absolute_cap(default_ttl_seconds())


class TurnLease:
    """Monotonic-clock liveness lease for one in-flight turn.

    Thread-safe (touched from the executor event loop AND from a background
    liveness poller). Uses ``time.monotonic`` so a wall-clock adjustment can't
    make a live turn look expired.
    """

    def __init__(
        self,
        ttl_seconds: float | None = None,
        *,
        absolute_cap_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds is None:
            # Env-derived default is already floored at 1.0s.
            self._ttl = default_ttl_seconds()
        else:
            # Explicit TTL (tests / callers): honor it, but never <= 0 which
            # would make every turn instantly "expired".
            self._ttl = max(1e-6, float(ttl_seconds))
        if absolute_cap_seconds is None:
            # Small multiple of THIS lease's TTL (env MOLECULE_MAX_TURN_SECONDS
            # overrides) so the two track together when the TTL is overridden.
            self._absolute_cap = _resolve_absolute_cap(self._ttl)
        else:
            self._absolute_cap = max(1e-6, float(absolute_cap_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        # Start "alive": a freshly-created lease is not immediately expired.
        self._last_touch = self._clock()
        # Turn-start origin for the ABSOLUTE cap. Distinct from _last_touch:
        # touch() moves _last_touch (idle clock) but NEVER _turn_start, so
        # activity touches cannot reset the un-bypassable wall-clock cap. Only
        # reset() (turn arming) moves it.
        self._turn_start = self._last_touch
        # mtime high-watermark for the activity-file feeder (source C).
        self._activity_file_mtime = 0.0
        # Count of REAL activity touches (sources A/C/D) this process has ever
        # recorded. Deliberately NOT reset by reset(): it is a property of the
        # RUNTIME FLAVOUR ("does anything here feed the lease?"), not of a turn.
        # See :meth:`observed_activity` / :func:`arm_turn_if_fed`.
        self._activity_touches = 0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def absolute_cap_seconds(self) -> float:
        return self._absolute_cap

    def touch(self) -> None:
        """Record tool activity now — resets the idle-expiry clock ONLY.

        Deliberately does NOT move ``_turn_start``: a continuously-touched
        activity file can keep the idle clock fresh, but the absolute wall-clock
        cap (see :meth:`absolute_cap_exceeded`) is measured from turn-start and
        is therefore un-bypassable by touches.
        """
        with self._lock:
            self._last_touch = self._clock()
            self._activity_touches += 1

    @property
    def observed_activity(self) -> bool:
        """True once ANY real activity touch (source A/C/D) has been recorded.

        This is the "has this runtime flavour got a working liveness feed?"
        question, and it is answered EMPIRICALLY rather than by a static
        declaration a flavour could get wrong. It never resets, because it
        describes the flavour, not the turn.

        :func:`arm_turn_if_fed` uses it to decide whether arming the lease for a
        turn would produce an HONEST signal or merely a confident-looking one —
        see that function for why arming a flavour with no feed is worse than
        not arming it at all.
        """
        with self._lock:
            return self._activity_touches > 0

    def reset(self) -> None:
        """Arm the lease at turn start: reset BOTH the idle clock and the
        absolute-cap origin. This is the ONLY place ``_turn_start`` moves."""
        with self._lock:
            now = self._clock()
            self._last_touch = now
            self._turn_start = now

    def age(self) -> float:
        """Seconds since the last touch."""
        with self._lock:
            return self._clock() - self._last_touch

    def turn_age(self) -> float:
        """Seconds since the turn was armed (:meth:`reset`) — for the abs cap."""
        with self._lock:
            return self._clock() - self._turn_start

    def expired(self) -> bool:
        """True once ``age`` exceeds the TTL with no intervening touch."""
        return self.age() > self._ttl

    def absolute_cap_exceeded(self) -> bool:
        """True once the wall-clock since turn-start exceeds the absolute cap.

        Enforced INDEPENDENT of activity touches — the un-bypassable backstop
        that closes the activity-file liveness bypass. A turn is ended once this
        is True even if ``expired()`` is False (file still being touched).
        """
        return self.turn_age() > self._absolute_cap

    def baseline_activity_file(self, path: str | os.PathLike[str]) -> None:
        """Adopt ``path``'s CURRENT mtime as the watermark, WITHOUT touching.

        Called once at turn setup, right after the activity file is
        materialized. Without it the very first
        :meth:`feed_from_activity_file` compares a real mtime against a 0.0
        watermark and "advances" — so the mere EXISTENCE of the file would
        register as tool activity. That would be the same vacuous signal this
        whole change exists to remove: a liveness feed that reports work
        because a file is there, not because anything happened. Worse, it would
        silently satisfy :meth:`observed_activity` for every flavour, including
        the ones that never write the file at all, and so defeat the check in
        :func:`arm_turn_if_fed`.

        Missing / unstat-able file is a no-op: there is nothing to baseline
        against, and the 0.0 watermark is then correct.
        """
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return
        with self._lock:
            if mtime > self._activity_file_mtime:
                self._activity_file_mtime = mtime

    def feed_from_activity_file(self, path: str | os.PathLike[str]) -> bool:
        """Touch iff ``path``'s mtime advanced since the last check.

        Source C: subprocess runtimes bump ``MOLECULE_TOOL_ACTIVITY_FILE`` on
        every tool call. Returns True when a touch happened. Missing / unstat-
        able file is a no-op (returns False) — the file only exists once a
        subprocess runtime has recorded activity.
        """
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return False
        with self._lock:
            advanced = mtime > self._activity_file_mtime
            if advanced:
                self._activity_file_mtime = mtime
                self._last_touch = self._clock()
                self._activity_touches += 1
        return advanced


# --------------------------------------------------------------------------
# Process-global lease — installed by the kernel wiring only when the mailbox
# kernel is enabled. None (and every helper a no-op) in the default flow.
# --------------------------------------------------------------------------
_CURRENT: TurnLease | None = None
_CURRENT_LOCK = threading.Lock()


def install(lease: TurnLease | None) -> None:
    """Install (or clear, with ``None``) the process-global turn lease."""
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = lease


def current() -> TurnLease | None:
    """Return the installed process-global lease, or ``None``."""
    with _CURRENT_LOCK:
        return _CURRENT


def touch_current() -> None:
    """Touch the global lease if one is installed; no-op otherwise.

    This is what ``a2a_executor`` calls on every tool start/end. When the
    mailbox kernel is off no lease is installed, so this returns immediately —
    the proven default flow is unaffected.
    """
    lease = current()
    if lease is not None:
        lease.touch()


def reset_current() -> None:
    """Arm the global lease at the start of a turn; no-op if none installed."""
    lease = current()
    if lease is not None:
        lease.reset()


def observed_activity_current() -> bool:
    """Has the global lease ever recorded a real activity touch? False if none."""
    lease = current()
    return lease is not None and lease.observed_activity


def arm_turn_if_fed() -> bool:
    """Arm the global lease for a turn starting NOW — but ONLY if this runtime
    has been observed to feed it. Returns True iff the lease was armed.

    Why this is conditional (runtime#408)
    -------------------------------------
    Arming is what makes the lease *believed*. A trigger daemon gates on
    attributability — ``turn_age_seconds < elapsed`` — so an unarmed lease
    (``turn_age`` == container uptime) is discarded as "no signal" and the
    daemon falls back to its own absolute ceiling. The moment a runtime arms
    per turn, that gate starts passing and every field of the snapshot is taken
    at face value.

    So arming without a working activity feed is strictly WORSE than not arming:

      * unarmed + no feed  -> "no signal", daemon waits to its ceiling. A hung
        turn is reaped late; a working turn is never killed. Safe, blunt.
      * armed + no feed    -> the lease is trusted AND reports the turn as idle
        from the moment it starts, because nothing ever touches it. A
        legitimately long turn (a claude-code session doing twenty minutes of
        tool work) is cancelled at the TTL. That is a NEW false kill, invented
        by the fix — the same lie as before, pointed the other way.

    Hence: arm only when the lease has demonstrably been fed at least once in
    this process. The condition is EMPIRICAL, not a list of flavour names that
    would silently rot as runtimes are added, and it is self-healing — a flavour
    that gains a tool-activity ping starts being armed with no runtime change.

    The cost is bounded and one-directional: on a fresh container the FIRST turn
    that ever feeds the lease is itself unarmed (it is what proves the feed
    exists), so it keeps today's safe "no signal" behaviour. Every subsequent
    turn is armed and honest.
    """
    lease = current()
    if lease is None:
        return False
    if not lease.observed_activity:
        # No feed has ever been seen here — leave the lease UNARMED so it stays
        # visibly unattributable rather than confidently wrong.
        return False
    lease.reset()
    return True


def feed_from_activity_file(path: str | os.PathLike[str]) -> bool:
    """Feed the global lease from ``MOLECULE_TOOL_ACTIVITY_FILE``; no-op if none."""
    lease = current()
    if lease is not None:
        return lease.feed_from_activity_file(path)
    return False


def turn_is_alive_despite_idle(path: str | os.PathLike[str]) -> bool:
    """MUST-FIX 1 stall gate. On an idle-cap timeout, is the turn STILL alive?

    The executor's per-event ``asyncio.wait_for`` idle-cap fires when
    ``astream_events`` produces NO runtime event for the cap. For a subprocess
    runtime (codex / openclaw / hermes) that surfaces no native event
    while its child churns tools, that is NOT a stall: the child bumps
    ``MOLECULE_TOOL_ACTIVITY_FILE`` on every tool call. This refreshes the lease
    from that file (source C) and reports whether it is still within TTL, so the
    executor keeps a genuinely-working subprocess going PAST the idle-cap
    instead of killing it — the lease complements the idle-cap, it does not
    fight it.

    Kernel OFF (no lease installed) -> ``current()`` is ``None`` -> returns
    ``False`` so the executor raises the stall EXACTLY as it does today
    (byte-identical). A stalled kernel-on turn (file not advancing) likewise
    expires and returns ``False`` at the idle-cap boundary.

    ABSOLUTE cap (activity-file liveness bypass fix): the source-C feed lets a
    WRITABLE activity file keep the lease un-expired, so on its own it would let
    any process that repeatedly touches that path keep a turn alive forever.
    This gate therefore ALSO consults :meth:`TurnLease.absolute_cap_exceeded`
    (env ``MOLECULE_MAX_TURN_SECONDS``, default a small multiple of the
    idle-cap) FIRST and returns ``False`` once the wall-clock since turn-start
    passes the cap — regardless of how fresh the activity file is.
    """
    lease = current()
    if lease is None:
        return False
    # Source C refresh, then judge — belt-and-braces with the background
    # watcher so the decision never depends on the watcher's last poll timing.
    feed_from_activity_file(path)
    # ABSOLUTE backstop FIRST (activity-file liveness bypass): even if the
    # activity file is being continuously touched (feed above kept the lease
    # un-expired), the turn is NOT alive once the wall-clock since turn-start
    # exceeds the absolute cap. This is what makes a WRITABLE activity file
    # unable to keep a turn alive indefinitely — the touch extends liveness up
    # to the TTL but never past the cap.
    if lease.absolute_cap_exceeded():
        return False
    return not lease.expired()


# --------------------------------------------------------------------------
# Scope occupancy registry — the re-entrancy guard for turn_liveness_scope.
#
# A DELIVERY IS NOT A TURN (runtime#409). On the native executor ``execute()``
# delegates to ``_core_execute()``, which is RE-ENTERED for every delivery that
# arrives while a turn is in flight — ``MOLECULE_A2A_NONBLOCKING`` is
# default-ON fleet-wide, so this is the live path. A routine self-ping is
# fast-acked and dropped; a user message is deferred at the same point. NEITHER
# becomes a turn: both return before ``_core_execute`` ever reaches its own
# ``reset_current()``, which is why the pre-#408 arm site was safe.
#
# A scope that armed unconditionally on entry re-dates the lease from such a
# delivery, and ``reset()`` moves BOTH the idle clock and the absolute-cap
# origin. On a workspace taking periodic self-pings (cron tick, delegation
# harvester) both the TTL and the cap then reset indefinitely, so a wedged turn
# is NEVER reaped — and because the lease *is* armed, the scheduler's
# ``lease_is_attributable`` gate passes and the daemon TRUSTS the "alive"
# snapshot instead of falling back to its own ceiling. That is strictly worse
# than the unarmed state #408 set out to fix.
#
# The scope is also legitimately NESTED: for openclaw (and any future
# ``SubprocessA2AExecutor`` subclass) both the ``main.py`` wrapper and the base
# enter it, so without a guard one turn arms twice and runs two watcher tasks.
#
# The guard: a registry of the scopes currently active in this process, keyed
# on the asyncio Task that entered. An entry made while the registry is EMPTY
# is the outermost scope and does the work; every other entry is a
# pass-through that arms nothing and starts no watcher.
#
# Why keyed on the task and not a bare depth counter
# --------------------------------------------------
# A bare integer counter would be sound for MUTUAL EXCLUSION. asyncio does not
# preempt between statements, so ``n += 1; outermost = (n == 1)`` cannot
# interleave with another coroutine on the same loop; the lock below covers
# only the (not currently exercised, but free to allow for) case of a second
# event loop on another thread.
#
# It is NOT sound for RECOVERY, and that is why it is not what is used here. A
# counter cannot distinguish a scope that is still running from one whose
# decrement was lost, and a lost decrement is a real possibility: this is an
# ``@asynccontextmanager``, and an async generator abandoned without
# ``__aexit__`` — a task cancelled at a bad moment, an ``aclose`` that never
# runs — is finalized by the GC, whose call to ``athrow`` can be dropped
# entirely when there is no running loop. One leaked increment leaves the
# counter permanently above zero, and the guard then silently declines to arm
# EVERY subsequent turn for the life of the container: a liveness mechanism
# failing to a permanent no-signal state, which is the exact defect this module
# exists to remove, reintroduced by its own guard. Keying on
# ``asyncio.current_task()`` makes that self-healing — an owner whose task is
# ``done()`` is provably no longer inside a scope, so it is pruned on the next
# entry and the registry drains itself.
#
# Keying on the task is also the right EQUIVALENCE CLASS. Nesting (wrapper ->
# base) happens within ONE task and is re-entrancy; an overlapping delivery is
# a DIFFERENT task and is concurrency. Both must decline to re-arm, but they
# are different situations — only the task key can tell them apart, and only
# the second one is the concurrency limit documented at the top of this module.
# --------------------------------------------------------------------------
#: owner token -> nesting depth for that owner. Guarded by _SCOPES_LOCK.
_ACTIVE_SCOPES: dict[object, int] = {}
_SCOPES_LOCK = threading.Lock()


def _prune_finished_scope_owners_locked() -> None:
    """Drop registry entries whose owning task has finished. Caller holds the lock.

    A finished task is provably not inside a scope, so its entry can only be a
    leak (see the note above on abandoned async generators). Pruning here — on
    entry, before the outermost decision — is what keeps a single lost
    ``__aexit__`` from disabling arming for the life of the process.
    """
    dead = [
        owner
        for owner in _ACTIVE_SCOPES
        if isinstance(owner, asyncio.Task) and owner.done()
    ]
    for owner in dead:
        del _ACTIVE_SCOPES[owner]


def _enter_liveness_scope() -> tuple[object, bool, bool]:
    """Register an active liveness scope.

    Returns ``(token, is_outermost, is_nested)``. ``is_nested`` distinguishes
    re-entrancy (same task) from concurrency (another task) — same verdict,
    different diagnosis; it is logged, not acted on.
    """
    try:
        owner: object | None = asyncio.current_task()
    except RuntimeError:  # pragma: no cover - no running loop
        owner = None
    # No task identity available: mint a unique token so this scope can neither
    # be mistaken for another's nor suppress a later one by key collision. Such
    # a token is not prunable, but it is only reachable off the asyncio path.
    token: object = owner if owner is not None else object()
    with _SCOPES_LOCK:
        _prune_finished_scope_owners_locked()
        outermost = not _ACTIVE_SCOPES
        nested = token in _ACTIVE_SCOPES
        _ACTIVE_SCOPES[token] = _ACTIVE_SCOPES.get(token, 0) + 1
    return token, outermost, nested


def _exit_liveness_scope(token: object) -> None:
    """Deregister a scope registered by :func:`_enter_liveness_scope`."""
    with _SCOPES_LOCK:
        remaining = _ACTIVE_SCOPES.get(token, 0) - 1
        if remaining > 0:
            _ACTIVE_SCOPES[token] = remaining
        else:
            _ACTIVE_SCOPES.pop(token, None)


def active_liveness_scopes() -> int:
    """Number of liveness scopes currently active in this process.

    Diagnostics / tests only. ``> 1`` means either a nested scope (wrapper plus
    a ``SubprocessA2AExecutor`` base) or overlapping deliveries — see the
    concurrency limit at the top of this module.
    """
    with _SCOPES_LOCK:
        return sum(_ACTIVE_SCOPES.values())


@contextlib.asynccontextmanager
async def turn_liveness_scope():
    """Everything one turn needs from the lease, for ANY executor. Async CM.

    Three things happen on entry, in this order, and the ORDER MATTERS:

    1. ``ensure_tool_activity_file()`` — materialize the private per-turn
       activity file and EXPORT ``MOLECULE_TOOL_ACTIVITY_FILE``. Until this
       runs, a subprocess runtime's tool-activity ping (codex / hermes both
       gate theirs on that env var) is a silent no-op, so the feed the lease
       depends on does not exist. Export BEFORE arming, and before the child is
       spawned, or the first tool calls of the turn are lost.
    2. ``arm_turn_if_fed()`` — arm the lease for THIS turn, if this runtime has
       a demonstrated feed. Strictly after the delivery has been accepted (we
       are inside ``execute``), which is what keeps ``turn_age < elapsed`` for
       the daemon that dispatched it: a runtime that armed BEFORE the delivery
       was accepted would report ``turn_age >= elapsed`` and silently fail the
       daemon's attributability gate open.
    3. Start the background activity-file watcher so touches land continuously
       rather than only when someone happens to read the snapshot.

    NON-RE-ENTRANT — a delivery is not a turn (runtime#409)
    -------------------------------------------------------
    All three steps run ONLY on the outermost active scope in this process. A
    scope entered while another is already open — whether nested inside it
    (``main.py``'s wrapper around a ``SubprocessA2AExecutor``, which enters the
    scope itself) or overlapping it (a delivery arriving mid-turn on the native
    executor, which is fast-acked and dropped WITHOUT becoming a turn) — is a
    pass-through: it arms nothing, spawns no second watcher, and yields
    ``False``.

    This is load-bearing, not tidiness. ``reset()`` moves the absolute-cap
    origin as well as the idle clock, so arming from a dropped self-ping resets
    both; on a workspace taking periodic self-pings that repeats forever and a
    wedged turn is never reaped, while the armed lease makes the daemon *trust*
    the "alive" reading. See the registry comment above for the full mechanism
    and for why the guard is keyed on the asyncio task rather than a counter.

    It does NOT make the lease multi-turn safe: see
    :ref:`the concurrency limit <turn-lease-concurrency-limit>` at the top of
    this module. While two turns overlap the lease stays dated to the older —
    conservative, and the only direction that cannot manufacture an unreapable
    turn.

    Kernel OFF (no lease installed) is a pure pass-through: no file is
    materialized, no env var is exported, no task is spawned — byte-identical
    to the pre-kernel flow. It is also checked BEFORE the scope registry, so
    the kernel-off path does not so much as take the lock.

    Never raises. A liveness scope must not be able to fail a turn.
    """
    if current() is None:
        yield False
        return
    token, outermost, nested = _enter_liveness_scope()
    if not outermost:
        try:
            logger.debug(
                "turn-lease: %s liveness scope entered while one is already "
                "active — not re-arming (runtime#409)",
                "nested" if nested else "overlapping",
            )
            yield False
        finally:
            _exit_liveness_scope(token)
        return
    armed = False
    stop_event: asyncio.Event | None = None
    watcher: asyncio.Task | None = None
    try:
        from molecule_runtime.executor_helpers import ensure_tool_activity_file

        path = ensure_tool_activity_file()
        # Adopt the file's current mtime BEFORE arming, so that creating it
        # cannot itself read as a tool call. See baseline_activity_file().
        lease = current()
        if lease is not None:
            lease.baseline_activity_file(path)
        armed = arm_turn_if_fed()
        try:
            stop_event = asyncio.Event()
            watcher = asyncio.create_task(
                watch_activity_file(path, stop_event=stop_event)
            )
        except RuntimeError:
            # No running loop — the snapshot's own read-time feed still covers
            # source C, just less promptly.
            stop_event = None
            watcher = None
    except Exception:  # noqa: BLE001 — liveness setup must never break a turn
        pass
    try:
        yield armed
    finally:
        # Deregister FIRST: the registry must drain even if awaiting the
        # watcher below hangs or raises, or this process stops arming turns.
        _exit_liveness_scope(token)
        if stop_event is not None:
            stop_event.set()
        if watcher is not None:
            try:
                await watcher
            except Exception:  # noqa: BLE001
                pass


async def watch_activity_file(
    path: str | os.PathLike[str],
    *,
    interval: float | None = None,
    stop_event: "asyncio.Event | None" = None,
) -> None:
    """Background watcher (MUST-FIX 1/3): refresh the global lease from the
    subprocess tool-activity file until ``stop_event`` is set.

    Installed by the executor for the duration of a turn WHEN the mailbox kernel
    is on. When no lease is installed (kernel OFF) it returns immediately, so
    the default flow never runs a watcher — byte-identical. Never raises: a
    liveness refresh must not perturb the turn.
    """
    if current() is None:
        # Kernel OFF — nothing to refresh; do not spin.
        return
    if interval is None:
        interval = activity_poll_seconds()
    while stop_event is None or not stop_event.is_set():
        try:
            feed_from_activity_file(path)
        except Exception:  # noqa: BLE001 — liveness refresh must never crash the turn
            pass
        if stop_event is None:
            await asyncio.sleep(interval)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), interval)
            except asyncio.TimeoutError:
                continue  # interval elapsed — poll again
            else:
                break  # stop requested
