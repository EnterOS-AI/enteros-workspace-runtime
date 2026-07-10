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
B. Claude Code — its adapter exposes ``transcript_lines()``; a poller touches
   the lease when the tail advances (new jsonl lines under ``~/.claude``).
C. codex / openclaw / hermes — subprocess runtimes export
   ``MOLECULE_TOOL_ACTIVITY_FILE`` (see ``executor_helpers``); each tool call
   bumps that file and :func:`feed_from_activity_file` touches the lease when
   its mtime advances. Same pattern as ``DELEGATION_RESULTS_FILE``.
D. Fallback — subprocess-output liveness: any bytes on the child's stdout/err
   is a tool-activity proxy and touches the lease.

Process-scoped
--------------
The executor is constructed in the template adapter (outside this wheel), so
the lease is a PROCESS-GLOBAL singleton installed by the kernel wiring
(:mod:`molecule_runtime.kernel`) only when ``MOLECULE_MAILBOX_KERNEL`` is on.
When the kernel is off the global is ``None`` and every :func:`touch_current` /
:func:`feed_from_activity_file` call is a cheap no-op — so the touches added to
``a2a_executor`` change NOTHING in the proven default flow.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable

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
