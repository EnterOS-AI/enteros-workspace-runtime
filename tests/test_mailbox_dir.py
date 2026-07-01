"""Mailbox dir resolver (MUST-FIX 5 gate) — the single gating point.

Pins the byte-identical-default contract: with MOLECULE_MAILBOX_KERNEL unset,
``resolve()`` returns the LEGACY ``configs_dir.resolve()`` so every migrated
call site behaves exactly as before. With the flag on, durable state moves to
``/workspace/.molecule`` (overridable).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.configs_dir as configs_dir  # noqa: E402
import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.delenv(mailbox_dir.MAILBOX_DIR_ENV, raising=False)
    yield


def test_kernel_disabled_by_default():
    assert mailbox_dir.kernel_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  True "])
def test_kernel_truthy_values(monkeypatch, val):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, val)
    assert mailbox_dir.kernel_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_kernel_falsey_values(monkeypatch, val):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, val)
    assert mailbox_dir.kernel_enabled() is False


def test_resolve_off_equals_configs_dir(monkeypatch, tmp_path):
    # Kernel OFF => resolve() is byte-identical to configs_dir.resolve().
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))
    assert mailbox_dir.resolve() == configs_dir.resolve()


def test_resolve_on_uses_workspace_molecule(monkeypatch, tmp_path):
    base = tmp_path / "ws" / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    resolved = mailbox_dir.resolve()
    assert resolved == base
    assert resolved.is_dir(), "kernel-on resolve() creates the durable dir"


def test_memory_and_snapshot_paths_on(monkeypatch, tmp_path):
    base = tmp_path / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "on")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    assert mailbox_dir.memory_dir() == base / "memory"
    assert mailbox_dir.memory_dir().is_dir()
    assert mailbox_dir.memory_file("MEMORY.md") == base / "memory" / "MEMORY.md"
    assert mailbox_dir.snapshot_path() == base / ".agent_snapshot.json"
