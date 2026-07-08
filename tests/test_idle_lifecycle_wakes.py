"""Lifecycle wake-up call typed-source (task #219 §5).

Proves the boot initial_prompt + reprovision-wake self-posts are now stamped
with a typed lifecycle source that the executor classifies as routine-self —
so they are governed by the autonomous-loop replay guard, closing the
unstamped guard-bypass the design flags.
"""
from __future__ import annotations

from molecule_runtime import kernel
from molecule_runtime.a2a_executor import (
    A2A_MESSAGE_SOURCE_TYPE,
    A2A_SOURCE_SELF_LIFECYCLE,
    _ROUTINE_SELF_SOURCE_TYPES,
)


def test_lifecycle_source_is_registered_guard_governed():
    # membership here is what wires the executor's routine-self classification
    # (`if source_type in _ROUTINE_SELF_SOURCE_TYPES`), i.e. guard governance.
    assert A2A_SOURCE_SELF_LIFECYCLE in _ROUTINE_SELF_SOURCE_TYPES
    assert A2A_SOURCE_SELF_LIFECYCLE == "self-lifecycle"


def test_kernel_maps_lifecycle_kind_to_source():
    assert (
        kernel.source_type_for(kernel.KIND_LIFECYCLE_WAKE)
        == A2A_SOURCE_SELF_LIFECYCLE
    )


def test_kernel_autonomous_metadata_stamps_lifecycle_source():
    md = kernel.autonomous_metadata(kernel.KIND_LIFECYCLE_WAKE)
    assert md[A2A_MESSAGE_SOURCE_TYPE] == A2A_SOURCE_SELF_LIFECYCLE


def test_initial_prompt_and_reprovision_stamp_the_source():
    """The two boot lifecycle wakes reference the lifecycle source constant
    (structural guard against a future refactor dropping the stamp)."""
    import inspect

    from molecule_runtime import main, reprovision_wake

    main_src = inspect.getsource(main)
    repro_src = inspect.getsource(reprovision_wake)
    assert "A2A_SOURCE_SELF_LIFECYCLE" in main_src, "initial_prompt lost its lifecycle stamp"
    assert "A2A_SOURCE_SELF_LIFECYCLE" in repro_src, "reprovision-wake lost its lifecycle stamp"
