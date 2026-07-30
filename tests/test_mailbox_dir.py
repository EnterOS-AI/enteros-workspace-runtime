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

import os
import time


@pytest.fixture(autouse=True)
def _fresh_usability_cache():
    mailbox_dir._usable_cache.clear()
    mailbox_dir._degraded_logged.clear()
    yield
    mailbox_dir._usable_cache.clear()
    mailbox_dir._degraded_logged.clear()


@pytest.fixture()
def _legacy_state(monkeypatch, tmp_path):
    """A workspace that ran kernel-off: cursor + tombstones + memory under the
    legacy configs dir, queued delegation results + activity cursor on the
    legacy /tmp defaults (redirected into tmp_path for the test)."""
    legacy = tmp_path / "configs"
    legacy.mkdir()
    (legacy / ".mcp_inbox_cursor").write_text("41", encoding="utf-8")
    (legacy / ".mcp_inbox_cursor_deadbeef").write_text("7", encoding="utf-8")
    (legacy / ".mcp_inbox_cursor.tmp").write_text("junk", encoding="utf-8")
    (legacy / ".delegation_tombstones").write_text('["t1","completed"]\n', encoding="utf-8")
    (legacy / "MEMORY.md").write_text("remember the vercel token", encoding="utf-8")
    (legacy / "CLAUDE.md").write_text("commit as jerry", encoding="utf-8")
    queue = tmp_path / "delegation_results.jsonl"
    queue.write_text('{"id":"d1"}\n', encoding="utf-8")
    act_cursor = tmp_path / "delegation_activity_cursor"
    act_cursor.write_text("act-9", encoding="utf-8")
    base = tmp_path / ".molecule"
    monkeypatch.setenv("CONFIGS_DIR", str(legacy))
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    monkeypatch.delenv("DELEGATION_RESULTS_FILE", raising=False)
    monkeypatch.delenv("DELEGATION_ACTIVITY_CURSOR_FILE", raising=False)
    # Redirect the legacy /tmp defaults into the test sandbox.
    monkeypatch.setattr(mailbox_dir, "_DEFAULT_DELEGATION_RESULTS_FILE", str(queue))
    real_pairs = mailbox_dir._legacy_pairs

    def pairs(base_, legacy_):
        out = []
        for src, dst in real_pairs(base_, legacy_):
            if str(src) == "/tmp/delegation_activity_cursor":
                src = act_cursor
            out.append((src, dst))
        return out

    monkeypatch.setattr(mailbox_dir, "_legacy_pairs", pairs)
    return legacy, base, queue


def test_migrate_carries_legacy_state_once(_legacy_state):
    legacy, base, queue = _legacy_state
    assert mailbox_dir.migrate_legacy_state() is True
    # replay safety: the cursor VALUE survives — no backlog-window replay
    assert (base / ".mcp_inbox_cursor").read_text() == "41"
    assert (base / ".mcp_inbox_cursor_deadbeef").read_text() == "7"
    # crash leftovers are NOT migrated as junk
    assert not (base / ".mcp_inbox_cursor.tmp").exists()
    # no delegation re-harvest: tombstones survive
    assert (base / ".delegation_tombstones").read_text().startswith('["t1"')
    # queued-but-unconsumed results + the activity cursor survive
    assert (base / "delegation_results.jsonl").read_text() == '{"id":"d1"}\n'
    assert (base / ".delegation_activity_cursor").read_text() == "act-9"
    # memory snapshots land in the kernel memory dir — the only place
    # kernel-on prompt assembly prefers (no silent memory loss)
    assert (base / "memory" / "MEMORY.md").read_text() == "remember the vercel token"
    assert (base / "memory" / "CLAUDE.md").read_text() == "commit as jerry"
    # migrated copies are private
    assert (base / ".mcp_inbox_cursor").stat().st_mode & 0o777 == 0o600
    # copy, not move — the legacy files stay for the emergency opt-out path
    assert (legacy / ".mcp_inbox_cursor").exists()
    # idempotent: the second boot is a marker-gated reconcile no-op
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


def test_migrate_skips_queue_when_env_override_pins_it(_legacy_state, monkeypatch, tmp_path):
    # An explicit DELEGATION_RESULTS_FILE is read verbatim in BOTH kernel
    # modes — the flip does not move it, so migrating a copy would be dead data.
    legacy, base, queue = _legacy_state
    monkeypatch.setenv("DELEGATION_RESULTS_FILE", str(queue))
    mailbox_dir.migrate_legacy_state()
    assert not (base / "delegation_results.jsonl").exists()
    assert (base / ".mcp_inbox_cursor").exists()  # the rest still migrates


def test_reconcile_prefers_newer_legacy_dotfiles_after_optout_window(_legacy_state):
    # kernel on → migrate → emergency opt-out window advances the legacy
    # cursor → re-enable: the newer legacy dotfile must win or the stale
    # mailbox copy replays the whole opt-out window.
    legacy, base, queue = _legacy_state
    assert mailbox_dir.migrate_legacy_state() is True
    past = time.time() - 3600
    os.utime(base / ".mcp_inbox_cursor", (past, past))
    (legacy / ".mcp_inbox_cursor").write_text("77", encoding="utf-8")
    assert mailbox_dir.migrate_legacy_state() is True  # reconcile carried it
    assert (base / ".mcp_inbox_cursor").read_text() == "77"


def test_reconcile_never_touches_memory_snapshots(_legacy_state):
    # /configs memory copies are PARAM-RENDERED fresh on every provision — a
    # newer mtime there is provisioner authorship, not opt-out evidence.
    # Reconciling them would clobber the agent's evolved mailbox memory with
    # the template baseline on every routine reprovision (review blocker).
    legacy, base, queue = _legacy_state
    assert mailbox_dir.migrate_legacy_state() is True
    past = time.time() - 3600
    os.utime(base / "memory" / "MEMORY.md", (past, past))
    (legacy / "MEMORY.md").write_text("param-rendered template baseline", encoding="utf-8")
    mailbox_dir.migrate_legacy_state()
    assert (base / "memory" / "MEMORY.md").read_text() == "remember the vercel token", (
        "reconcile must never overwrite evolved mailbox memory with a re-render"
    )


def test_first_boot_migrates_degraded_window_memory(_legacy_state):
    # Memory written during a DEGRADED kernel window lands at <configs>/memory
    # — the first-boot pass must source that subdir too.
    legacy, base, queue = _legacy_state
    (legacy / "memory").mkdir()
    (legacy / "memory" / "USER.md").write_text("degraded-window user memory", encoding="utf-8")
    assert mailbox_dir.migrate_legacy_state() is True
    assert (base / "memory" / "USER.md").read_text() == "degraded-window user memory"


def test_migrate_defers_marker_when_legacy_dir_absent(_legacy_state):
    import shutil

    legacy, base, queue = _legacy_state
    shutil.rmtree(legacy)
    mailbox_dir.migrate_legacy_state()
    assert not (base / mailbox_dir._MIGRATED_MARKER).exists(), (
        "marker must not burn while /configs is absent (asset-fetcher window)"
    )
    # legacy appears on a later boot → the one-shot migration still happens
    legacy.mkdir(exist_ok=True)
    (legacy / ".mcp_inbox_cursor").write_text("41", encoding="utf-8")
    assert mailbox_dir.migrate_legacy_state() is True
    assert (base / ".mcp_inbox_cursor").read_text() == "41"
    assert (base / mailbox_dir._MIGRATED_MARKER).exists()


def test_migrate_rerun_heals_crash_leftover(_legacy_state):
    legacy, base, queue = _legacy_state
    base.mkdir(parents=True)
    # a crashed earlier pass left a partial .migrating temp behind
    (base / ".mcp_inbox_cursor.migrating").write_text("par", encoding="utf-8")
    assert mailbox_dir.migrate_legacy_state() is True
    assert (base / ".mcp_inbox_cursor").read_text() == "41"


def test_migrator_memory_list_matches_prompt_ssot():
    # The migrator duplicates prompt.DEFAULT_MEMORY_SNAPSHOT_FILES to avoid an
    # import cycle — this pin keeps the two tuples in lockstep.
    from molecule_runtime import prompt

    assert tuple(mailbox_dir._LEGACY_MEMORY_BASENAMES) == tuple(
        prompt.DEFAULT_MEMORY_SNAPSHOT_FILES
    )


# ── unusable-base degradation (core#4295 class) ────────────────────────────


def test_resolve_degrades_to_configs_when_base_unwritable(monkeypatch, tmp_path):
    legacy = tmp_path / "configs"
    legacy.mkdir()
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    base = locked / ".molecule"
    monkeypatch.setenv("CONFIGS_DIR", str(legacy))
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    try:
        assert mailbox_dir.resolve() == configs_dir.resolve()
        assert mailbox_dir.base_degraded() is True
        # the raw base still reports UNWRITABLE to the durability guard
        assert mailbox_dir.verify_durability() == mailbox_dir.DURABILITY_UNWRITABLE
        # migration is a no-op (base == legacy) — nothing scatters
        assert mailbox_dir.migrate_legacy_state() is False
    finally:
        locked.chmod(0o700)


def test_delegation_results_falls_back_to_tmp_legacy_when_degraded(monkeypatch, tmp_path):
    # The queue's legacy home is /tmp, NOT configs — degraded mode must return
    # the exact legacy path so delegation delivery keeps working (review R2).
    legacy = tmp_path / "configs"
    legacy.mkdir()
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    monkeypatch.setenv("CONFIGS_DIR", str(legacy))
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(locked / ".molecule"))
    monkeypatch.delenv("DELEGATION_RESULTS_FILE", raising=False)
    try:
        assert (
            mailbox_dir.delegation_results_file()
            == mailbox_dir._DEFAULT_DELEGATION_RESULTS_FILE
        )
    finally:
        locked.chmod(0o700)


def test_degraded_window_memory_beats_param_rendered_root_copy(_legacy_state):
    # Both exist at first boot: <configs>/memory/<name> (agent memory written
    # during a degraded window) AND <configs>/<name> (param-rendered template
    # baseline). The agent memory must win under no-clobber ordering.
    legacy, base, queue = _legacy_state
    (legacy / "memory").mkdir()
    (legacy / "memory" / "MEMORY.md").write_text("agent memory from degraded window", encoding="utf-8")
    # legacy/MEMORY.md (the template baseline) already exists via the fixture
    assert mailbox_dir.migrate_legacy_state() is True
    assert (base / "memory" / "MEMORY.md").read_text() == "agent memory from degraded window"


def test_first_boot_records_seed_provenance_for_root_configs_copies(_legacy_state):
    """The memory copies made from the /configs ROOT are param-rendered role
    files, not agent memory. Record their sha256+size so prompt.py can later
    tell a frozen first-boot SNAPSHOT of role file v1 apart from memory a writer
    produced — without provenance the two are indistinguishable once /configs
    has been re-rendered to v2, and the stale v1 gets injected forever.
    """
    import hashlib
    import json

    legacy, base, queue = _legacy_state
    assert mailbox_dir.migrate_legacy_state() is True

    manifest = json.loads(
        (base / "memory" / mailbox_dir._SEED_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    for name in ("MEMORY.md", "CLAUDE.md"):  # both seeded by the fixture
        raw = (legacy / name).read_bytes()
        assert manifest[name] == {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    assert mailbox_dir.seed_manifest()["MEMORY.md"]["size"] == len(
        (legacy / "MEMORY.md").read_bytes()
    )
    # 0600: the seed digests describe files that may be relay-delivered 0600.
    assert (base / "memory" / mailbox_dir._SEED_MANIFEST_NAME).stat().st_mode & 0o777 == 0o600


def test_degraded_window_memory_is_never_recorded_as_a_seed(_legacy_state):
    """A <configs>/memory/<name> source really IS agent memory. Recording it as
    a role-file seed would let prompt.py subtract it, deleting the agent's own
    memory — so only the ROOT copy is ever recorded."""
    import json

    legacy, base, queue = _legacy_state
    (legacy / "memory").mkdir()
    (legacy / "memory" / "MEMORY.md").write_text("agent memory from degraded window", encoding="utf-8")
    assert mailbox_dir.migrate_legacy_state() is True

    manifest = json.loads(
        (base / "memory" / mailbox_dir._SEED_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert "MEMORY.md" not in manifest, (
        "the degraded-window copy won the destination — it is agent memory and "
        "must carry no seed provenance"
    )
    assert "CLAUDE.md" in manifest, "the root-sourced copy is still recorded"


def test_seed_manifest_empty_without_kernel_or_file(_legacy_state, monkeypatch):
    legacy, base, queue = _legacy_state
    assert mailbox_dir.seed_manifest() == {}  # no migration has run yet
    mailbox_dir.migrate_legacy_state()
    assert mailbox_dir.seed_manifest()
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    assert mailbox_dir.seed_manifest() == {}, "kernel OFF reads no mailbox provenance"
