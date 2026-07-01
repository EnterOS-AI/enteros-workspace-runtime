"""Mailbox kernel coordinator (MUST-FIX 2) — runaway-guard + source stamping.

Pins:
  1. enabled() tracks the flag;
  2. should_inject_autonomous_turn() consults the autonomous-loop circuit
     breaker (should_halt) BEFORE injection — the runaway guard is NOT broken;
  3. autonomous_metadata() stamps a routine self-source type so the executor's
     _is_routine_self_message returns True and the (unchanged)
     evaluate_autonomous_output gate governs the output;
  4. install() arms the turn lease only when the kernel is on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime import autonomous_loop_guard as guard  # noqa: E402
from molecule_runtime import kernel  # noqa: E402
from molecule_runtime import turn_lease as tl  # noqa: E402
from molecule_runtime.a2a_executor import (  # noqa: E402
    A2A_MESSAGE_SOURCE_TYPE,
    A2A_SOURCE_SELF_DELEGATION,
    A2A_SOURCE_SELF_GOAL_NUDGE,
    A2A_SOURCE_SELF_IDLE,
    A2A_SOURCE_SELF_SCHEDULER,
    _ROUTINE_SELF_SOURCE_TYPES,
    _is_routine_self_message,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    tl.install(None)
    # Reset the process-global circuit breaker between cases.
    guard._DEFAULT.__init__()
    yield
    tl.install(None)
    guard._DEFAULT.__init__()


def test_enabled_tracks_flag(monkeypatch):
    assert kernel.enabled() is False
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    assert kernel.enabled() is True


def test_new_source_types_are_registered_as_routine_self():
    # MUST-FIX 2: the new autonomous kinds must be governed exactly like the
    # legacy self-pings — registered in _ROUTINE_SELF_SOURCE_TYPES.
    for st in (A2A_SOURCE_SELF_SCHEDULER, A2A_SOURCE_SELF_GOAL_NUDGE, A2A_SOURCE_SELF_DELEGATION):
        assert st in _ROUTINE_SELF_SOURCE_TYPES


def test_source_type_for_maps_each_kind():
    assert kernel.source_type_for(kernel.KIND_IDLE) == A2A_SOURCE_SELF_IDLE
    assert kernel.source_type_for(kernel.KIND_SCHEDULER) == A2A_SOURCE_SELF_SCHEDULER
    assert kernel.source_type_for(kernel.KIND_GOAL_NUDGE) == A2A_SOURCE_SELF_GOAL_NUDGE
    assert kernel.source_type_for(kernel.KIND_DELEGATION_RESULT) == A2A_SOURCE_SELF_DELEGATION
    # An unknown kind still maps to a routine self type (never un-governed).
    assert kernel.source_type_for("brand-new-kind") in _ROUTINE_SELF_SOURCE_TYPES


def test_autonomous_metadata_makes_turn_routine_self():
    """A message stamped with kernel.autonomous_metadata is classified as a
    routine self-ping, so the executor subjects it to the replay guard."""
    meta = kernel.autonomous_metadata(kernel.KIND_SCHEDULER)
    assert meta[A2A_MESSAGE_SOURCE_TYPE] == A2A_SOURCE_SELF_SCHEDULER

    class _Ctx:
        def __init__(self, metadata):
            self.message = type("M", (), {"metadata": metadata})()
            self.metadata = None

    assert _is_routine_self_message(_Ctx(meta), "anything") is True


def test_autonomous_metadata_preserves_extra():
    meta = kernel.autonomous_metadata(kernel.KIND_GOAL_NUDGE, extra={"k": "v"})
    assert meta["k"] == "v"
    assert meta[A2A_MESSAGE_SOURCE_TYPE] == A2A_SOURCE_SELF_GOAL_NUDGE


def test_should_inject_allows_when_breaker_closed():
    assert kernel.should_inject_autonomous_turn(kernel.KIND_IDLE) is True


def test_should_inject_drops_when_breaker_open(monkeypatch):
    # Force the circuit breaker OPEN and assert the kernel refuses injection.
    monkeypatch.setattr(guard, "should_halt", lambda: True)
    assert kernel.should_inject_autonomous_turn(kernel.KIND_SCHEDULER) is False


def test_should_inject_fails_open_on_guard_error(monkeypatch):
    def _boom():
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(guard, "should_halt", _boom)
    # A guard hiccup must never crash the caller's loop — fail OPEN (allow).
    assert kernel.should_inject_autonomous_turn() is True


def test_install_noop_when_disabled():
    kernel.install()
    assert tl.current() is None


def test_install_arms_lease_when_enabled(monkeypatch):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    kernel.install()
    assert tl.current() is not None
    kernel.uninstall()
    assert tl.current() is None
