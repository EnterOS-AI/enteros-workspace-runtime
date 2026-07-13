"""Mailbox kernel — coordinator for durable, acked, guard-safe autonomous turns.

This is the seam that ties the mailbox-kernel pieces together. The kernel is
NATIVE runtime behavior (default ON — operator ruling 2026-07-13, task #219);
``MOLECULE_MAILBOX_KERNEL=0`` (:func:`molecule_runtime.mailbox_dir.kernel_enabled`)
is the emergency opt-out under which the kernel installs nothing and every
helper falls back to the exact legacy behavior (legacy state paths, static
idle loop) — byte-identical to the pre-kernel runtime.

Responsibilities
----------------
MUST-FIX 2 (runaway guard — DO NOT break):
    Every autonomous self-turn the runtime injects (idle self-wake, scheduler
    tick, goal-nudge, delegation-result harvest) MUST (a) be dropped when the
    autonomous-loop circuit breaker is OPEN — :func:`should_inject_autonomous_turn`
    consults :func:`autonomous_loop_guard.should_halt` BEFORE injection — and
    (b) carry a routine self-source ``source_type`` so the executor's
    ``_is_routine_self_message`` returns True and the UNCHANGED end-of-turn
    ``evaluate_autonomous_output`` gate governs its output. :func:`autonomous_metadata`
    stamps that marker. No new suppression logic lives here — the enforcement
    is still the existing guard.

MUST-FIX 1 (turn lease):
    :func:`install` publishes a process-global :class:`~molecule_runtime.turn_lease.TurnLease`
    (kernel-on only) so the executor's tool-activity touches have somewhere to
    land. :func:`uninstall` clears it.

The autonomous "kinds" and their stamped source types are the SSOT the
executor consumes via ``_ROUTINE_SELF_SOURCE_TYPES``.
"""
from __future__ import annotations

import logging

from molecule_runtime import mailbox_dir
from molecule_runtime.a2a_executor import (
    A2A_MESSAGE_SOURCE_TYPE,
    A2A_SOURCE_SELF_DELEGATION,
    A2A_SOURCE_SELF_GOAL_NUDGE,
    A2A_SOURCE_SELF_HARVESTER,
    A2A_SOURCE_SELF_IDLE,
    A2A_SOURCE_SELF_LIFECYCLE,
    A2A_SOURCE_SELF_SCHEDULER,
)

logger = logging.getLogger(__name__)

# Logical autonomous-turn kinds. Callers pass a KIND_*; the kernel maps it to
# the stamped source_type so there is one place that knows the mapping.
KIND_IDLE = "idle"
KIND_SCHEDULER = "scheduler"
KIND_GOAL_NUDGE = "goal_nudge"
KIND_DELEGATION_RESULT = "delegation_result"
KIND_HARVESTER = "harvester"
# Lifecycle wake-up calls (task #219 §5): first-creation greeting, session
# reset, restart/reprovision, plugin-install, idle reactivation — all stamped
# through one typed source so they are guard-governed like every other
# autonomous turn (never the unstamped guard-bypass they were before).
KIND_LIFECYCLE_WAKE = "lifecycle_wake"

_KIND_TO_SOURCE_TYPE = {
    KIND_IDLE: A2A_SOURCE_SELF_IDLE,
    KIND_SCHEDULER: A2A_SOURCE_SELF_SCHEDULER,
    KIND_GOAL_NUDGE: A2A_SOURCE_SELF_GOAL_NUDGE,
    KIND_DELEGATION_RESULT: A2A_SOURCE_SELF_DELEGATION,
    KIND_HARVESTER: A2A_SOURCE_SELF_HARVESTER,
    KIND_LIFECYCLE_WAKE: A2A_SOURCE_SELF_LIFECYCLE,
}


def enabled() -> bool:
    """True iff the mailbox kernel is switched on. Single flag, read live."""
    return mailbox_dir.kernel_enabled()


def source_type_for(kind: str) -> str:
    """Map a logical autonomous ``kind`` to its stamped ``source_type``.

    Unknown kinds fall back to the harvester marker (still a routine self type),
    so a new caller can never accidentally emit an *un-governed* autonomous turn.
    """
    return _KIND_TO_SOURCE_TYPE.get(kind, A2A_SOURCE_SELF_HARVESTER)


def autonomous_metadata(kind: str, extra: dict | None = None) -> dict:
    """Build A2A ``params.metadata`` for an autonomous self-turn of ``kind``.

    Stamps ``source_type`` so the executor classifies the turn as a routine
    self-ping and subjects its output to the (unchanged) replay guard.
    """
    meta = dict(extra or {})
    meta[A2A_MESSAGE_SOURCE_TYPE] = source_type_for(kind)
    return meta


def should_inject_autonomous_turn(kind: str = KIND_IDLE) -> bool:
    """Return False when the autonomous-loop circuit breaker is OPEN.

    MUST-FIX 2: the kernel calls :func:`autonomous_loop_guard.should_halt`
    BEFORE injecting ANY autonomous turn. Callers drop the injection on False.
    Import is lazy + defensive so a guard-module hiccup can never crash the
    caller's loop (fails OPEN — allow the turn — matching the legacy idle-loop
    ``except Exception: pass`` posture).
    """
    try:
        from molecule_runtime.autonomous_loop_guard import should_halt

        if should_halt():
            logger.info(
                "kernel: autonomous-loop circuit breaker OPEN — dropping %s self-turn",
                kind,
            )
            return False
    except Exception:  # noqa: BLE001 — guard lookup must never crash the loop
        pass
    return True


def install() -> None:
    """Migrate legacy state + publish the process-global turn lease.

    No-op under the ``MOLECULE_MAILBOX_KERNEL=0`` opt-out, so nothing is
    installed and the executor's tool-activity touches stay no-ops (legacy
    flow byte-identical).
    """
    if not enabled():
        return
    # Legacy-state migration FIRST (design §7.2): the inbox cursor and
    # delegation tombstones must be in place before the inbox poller and
    # heartbeat read them, or a previously kernel-off workspace replays its
    # whole inbound backlog / re-harvests delegations on this boot.
    mailbox_dir.migrate_legacy_state()

    from molecule_runtime import turn_lease

    lease = turn_lease.TurnLease()
    turn_lease.install(lease)
    logger.info(
        "kernel: mailbox kernel ENABLED — turn lease armed (ttl=%.0fs), durable base=%s",
        lease.ttl_seconds,
        mailbox_dir.resolve(),
    )
    # Durability guard: prove the resolved base is actually a persistent volume.
    # On a flavor where /workspace is only the ephemeral root disk this warns
    # LOUD (instead of silently losing state on the next auto-heal — cp#326).
    mailbox_dir.verify_durability()


def uninstall() -> None:
    """Clear the process-global turn lease (test teardown / shutdown)."""
    from molecule_runtime import turn_lease

    turn_lease.install(None)
