"""Mailbox dir resolver (MUST-FIX 5 gate) — the single gating point.

Pins the NATIVE-DEFAULT contract (task #219 operator ruling 2026-07-13): with
MOLECULE_MAILBOX_KERNEL unset the kernel is ON and ``resolve()`` returns
``/workspace/.molecule`` (overridable). Setting the flag explicitly falsy
("0"/"false"/"no"/"off") is the emergency opt-out back to the LEGACY
``configs_dir.resolve()`` byte-identical behavior. The §7.2 migrator carries
legacy durable state (inbox cursor, delegation tombstones, results queue)
into the mailbox root exactly once, copy-not-move, never clobbering.
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


def test_kernel_enabled_by_default():
    # NATIVE default: unset flag == kernel ON (operator ruling 2026-07-13).
    assert mailbox_dir.kernel_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  True ", "", "nope"])
def test_kernel_enabled_values(monkeypatch, val):
    # Anything that is not an explicit opt-out keeps the kernel on — a typo'd
    # value must never silently drop a workspace back to legacy paths.
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, val)
    assert mailbox_dir.kernel_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", " Off "])
def test_kernel_optout_values(monkeypatch, val):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, val)
    assert mailbox_dir.kernel_enabled() is False


def test_resolve_optout_equals_configs_dir(monkeypatch, tmp_path):
    # Explicit opt-out => resolve() is byte-identical to configs_dir.resolve().
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
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


# ── §7.2 legacy-state migrator — replay safety on the native-default flip ──


@pytest.fixture()
def _legacy_state(monkeypatch, tmp_path):
    """A workspace that ran kernel-off: cursor + tombstones under the legacy
    configs dir, a queued delegation result on the legacy /tmp default."""
    legacy = tmp_path / "configs"
    legacy.mkdir()
    (legacy / ".mcp_inbox_cursor").write_text("41", encoding="utf-8")
    (legacy / ".mcp_inbox_cursor_deadbeef").write_text("7", encoding="utf-8")
    (legacy / ".delegation_tombstones").write_text('["t1","completed"]\n', encoding="utf-8")
    (legacy / "MEMORY.md").write_text("remember the vercel token", encoding="utf-8")
    (legacy / "CLAUDE.md").write_text("commit as jerry", encoding="utf-8")
    queue = tmp_path / "delegation_results.jsonl"
    queue.write_text('{"id":"d1"}\n', encoding="utf-8")
    base = tmp_path / ".molecule"
    monkeypatch.setenv("CONFIGS_DIR", str(legacy))
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    monkeypatch.setenv("DELEGATION_RESULTS_FILE", str(queue))
    return legacy, base, queue


def test_migrate_carries_legacy_state_once(_legacy_state):
    legacy, base, queue = _legacy_state
    assert mailbox_dir.migrate_legacy_state() is True
    # replay safety: the cursor VALUE survives — no inbound replay
    assert (base / ".mcp_inbox_cursor").read_text() == "41"
    assert (base / ".mcp_inbox_cursor_deadbeef").read_text() == "7"
    # no delegation re-harvest: tombstones survive
    assert (base / ".delegation_tombstones").read_text().startswith('["t1"')
    # queued-but-unconsumed results survive
    assert (base / "delegation_results.jsonl").read_text() == '{"id":"d1"}\n'
    # memory snapshots land in the kernel memory dir — the only place
    # kernel-on prompt assembly reads them from (no silent memory loss)
    assert (base / "memory" / "MEMORY.md").read_text() == "remember the vercel token"
    assert (base / "memory" / "CLAUDE.md").read_text() == "commit as jerry"
    # copy, not move — the legacy files stay for the emergency opt-out path
    assert (legacy / ".mcp_inbox_cursor").exists()
    # idempotent: the second boot is a marker-gated no-op
    assert mailbox_dir.migrate_legacy_state() is False


def test_migrate_never_clobbers_kernel_state(_legacy_state):
    legacy, base, queue = _legacy_state
    base.mkdir(parents=True)
    (base / ".mcp_inbox_cursor").write_text("99", encoding="utf-8")
    mailbox_dir.migrate_legacy_state()
    # kernel-authored state is NEWER than legacy state — it always wins
    assert (base / ".mcp_inbox_cursor").read_text() == "99"
    # files absent at the base still migrate
    assert (base / ".delegation_tombstones").exists()


def test_migrate_noop_on_optout(_legacy_state, monkeypatch):
    legacy, base, queue = _legacy_state
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    assert mailbox_dir.migrate_legacy_state() is False
    assert not base.exists()


def test_migrator_memory_list_matches_prompt_ssot():
    # The migrator duplicates prompt.DEFAULT_MEMORY_SNAPSHOT_FILES to avoid an
    # import cycle — this pin keeps the two tuples in lockstep.
    from molecule_runtime import prompt

    assert tuple(mailbox_dir._LEGACY_MEMORY_BASENAMES) == tuple(
        prompt.DEFAULT_MEMORY_SNAPSHOT_FILES
    )
