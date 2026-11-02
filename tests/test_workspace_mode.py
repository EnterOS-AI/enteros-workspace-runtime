"""MOLECULE_CONTINUOUS — the "24/7" switch for a workspace's idle self-wake.

Off means the runtime arms neither idle loop; the workspace runs only for the
messages it receives and the schedules its trigger daemon fires.
"""
from __future__ import annotations

import inspect

import pytest

from molecule_runtime import workspace_mode
from molecule_runtime.workspace_mode import CONTINUOUS_ENV, continuous


def test_default_is_continuous(monkeypatch):
    monkeypatch.delenv(CONTINUOUS_ENV, raising=False)
    assert continuous() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", " Off "])
def test_explicit_falsy_turns_it_off(monkeypatch, value):
    monkeypatch.setenv(CONTINUOUS_ENV, value)
    assert continuous() is False


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "on", "anything"])
def test_everything_else_stays_on(monkeypatch, value):
    monkeypatch.setenv(CONTINUOUS_ENV, value)
    assert continuous() is True


def test_read_live_not_cached(monkeypatch):
    monkeypatch.setenv(CONTINUOUS_ENV, "false")
    assert continuous() is False
    monkeypatch.setenv(CONTINUOUS_ENV, "true")
    assert continuous() is True


def test_main_gates_both_idle_loops_on_the_switch():
    """Structural: the digest loop AND the legacy static loop are both behind it.

    Gating only the digest would let the legacy loop arm instead (its condition
    is `not _idle_digest_enabled`), which is the same 300 s self-wake.
    """
    from molecule_runtime import main

    src = inspect.getsource(main)
    assert "_wakes_itself = _continuous()" in src
    assert "if adapter_ready and _wakes_itself:" in src, "idle digest not gated"
    assert (
        "if adapter_ready and _wakes_itself and config.idle_prompt and not _idle_digest_enabled:"
        in src
    ), "legacy idle loop not gated"


def test_shares_the_kernel_flag_falsy_set():
    """MOLECULE_CONTINUOUS reads "explicitly falsy" exactly as MOLECULE_MAILBOX_KERNEL does."""
    from molecule_runtime import mailbox_dir

    assert workspace_mode._FALSY is mailbox_dir._FALSY


def test_on_demand_prompt_only_when_the_switch_is_off(tmp_path, monkeypatch):
    """Off, the idle digest that re-surfaces task_add/goal_set work is not armed;
    the prompt says so instead of leaving the tools' "idle brings it back" promise."""
    from molecule_runtime.prompt import ON_DEMAND_PROMPT, build_system_prompt

    def _prompt():
        return build_system_prompt(
            config_path=str(tmp_path), workspace_id="w", loaded_skills=[], peers=[]
        )

    monkeypatch.setenv(CONTINUOUS_ENV, "false")
    off = _prompt()
    monkeypatch.delenv(CONTINUOUS_ENV)
    on = _prompt()

    assert ON_DEMAND_PROMPT in off
    assert ON_DEMAND_PROMPT not in on
    assert "task_add" in ON_DEMAND_PROMPT and "goal_set" in ON_DEMAND_PROMPT
