"""Memory WRITE-path reconciliation (REMAINING MUST-FIX).

The bug: ``prompt.py`` reads durable memory, but the WRITERS pointed elsewhere
(``/configs`` / ``/workspace/AGENTS.md``), so a stale ``/configs`` copy could
SHADOW fresh agent memory. The fix reconciles every writer (agents_md,
append-to-memory hook, consolidation) onto the durable mailbox memory dir and
makes ``prompt.py`` read memory snapshots from there when the kernel is on.

The load-bearing test: **a stale /configs copy must NEVER shadow fresh
/workspace/.molecule/memory.** Plus the byte-identical-when-off guarantee.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime.prompt import build_system_prompt  # noqa: E402


STALE = "STALE-CONFIGS-MEMORY-DO-NOT-SHOW"
FRESH = "FRESH-MAILBOX-MEMORY-SHOW-THIS"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.delenv(mailbox_dir.MAILBOX_DIR_ENV, raising=False)
    yield


def _enable_kernel(monkeypatch, tmp_path):
    base = tmp_path / "ws" / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    return base


def test_stale_configs_never_shadows_fresh_mailbox_memory(tmp_path, monkeypatch):
    """THE test: with the kernel ON, a stale /configs/MEMORY.md must NOT appear
    in the prompt, and the fresh mailbox-memory MEMORY.md MUST."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    # Stale copy in the provisioner-owned /configs dir.
    (configs / "MEMORY.md").write_text(STALE, encoding="utf-8")

    base = _enable_kernel(monkeypatch, tmp_path)
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True)
    # Fresh copy on the durable mailbox volume.
    (mem_dir / "MEMORY.md").write_text(FRESH, encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="w",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md"],
    )

    assert "BASE-ROLE" in out, "param-rendered system prompt still loads from /configs"
    assert FRESH in out, "fresh mailbox memory must be injected"
    assert STALE not in out, "a stale /configs copy must NEVER shadow fresh mailbox memory"


def test_prompt_files_memory_snapshot_uses_mailbox_not_configs(tmp_path, monkeypatch):
    """RC #203, AMENDED for durable /workspace (cp#672 / controlplane #2777).

    RC #203 originally REDIRECTED a memory basename named in prompt_files to its
    mailbox copy and asserted the /configs copy never appears. That assumed a
    memory basename in prompt_files is always durable memory — false for
    openclaw, whose ROLE files are literally named SOUL.md / AGENTS.md / USER.md.
    Once /workspace is durable, the never-refreshed mailbox copy
    (``_copy_0600`` is skip-if-exists; reconcile skips the memory dir) FROZE the
    persona at first boot forever. See
    tests/test_declared_role_file_not_frozen_by_mailbox_memory.py.

    RC #203's LOAD-BEARING property is unchanged and still pinned here: fresh
    mailbox memory must ALWAYS reach the prompt and can never be suppressed by
    the presence of a /configs copy. What changed is that the declared /configs
    copy is now ALSO injected, in the ROLE slot ahead of it — the two are
    different documents that merely share a basename, so neither is dropped and
    the durable memory still layers last (wins on recency).
    """
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    # Provisioner-authored copy in /configs, explicitly DECLARED in prompt_files.
    (configs / "MEMORY.md").write_text(STALE, encoding="utf-8")

    base = _enable_kernel(monkeypatch, tmp_path)
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text(FRESH, encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="w",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md", "MEMORY.md"],  # MEMORY.md IN prompt_files
    )

    assert "BASE-ROLE" in out, "non-memory prompt files still load from /configs"
    assert FRESH in out, (
        "RC #203 core property: fresh mailbox memory must ALWAYS reach the "
        "prompt, even when the basename is declared in prompt_files"
    )
    assert out.count(FRESH) == 1, "the memory snapshot must appear exactly once (no double-load)"
    # The declared /configs copy is the ROLE slot and is re-rendered every
    # provision, so it must land AND must be positioned before durable memory.
    assert STALE in out, (
        "the DECLARED /configs copy is a provisioner-authored role file and must "
        "still land — suppressing it is what froze the openclaw persona"
    )
    assert out.index(STALE) < out.index(FRESH), (
        "durable memory layers AFTER the role slot so it still wins on recency"
    )


def test_prompt_files_memory_falls_back_to_configs_when_no_mailbox_copy(tmp_path, monkeypatch):
    """RC #203: MEMORY.md named in prompt_files with the kernel ON but NO mailbox
    copy yet still loads the /configs copy (graceful fallback, no crash)."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    (configs / "MEMORY.md").write_text("CONFIGS-ONLY-MEMORY", encoding="utf-8")

    base = _enable_kernel(monkeypatch, tmp_path)
    (base / "memory").mkdir(parents=True)  # empty — no mailbox MEMORY.md

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="w",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md", "MEMORY.md"],
    )
    assert "CONFIGS-ONLY-MEMORY" in out, "with no mailbox copy the /configs copy is the fallback"


def test_prompt_files_memory_kernel_off_byte_identical(tmp_path, monkeypatch):
    """Kernel OFF with MEMORY.md in prompt_files: loads the /configs copy exactly
    as before (byte-identical — memory_source IS config_path)."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    (configs / "MEMORY.md").write_text(STALE, encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="w",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md", "MEMORY.md"],
    )
    assert STALE in out, "kernel off keeps the legacy /configs MEMORY.md source"


def test_kernel_off_reads_configs_byte_identical(tmp_path, monkeypatch):
    """Kernel opt-out: memory snapshots load from /configs exactly as before."""
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    (configs / "MEMORY.md").write_text(STALE, encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="w",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md"],
    )
    # With the kernel off the /configs MEMORY.md is the legacy source — loaded.
    assert STALE in out


def test_append_to_memory_hook_writes_mailbox_when_on(tmp_path, monkeypatch):
    from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter

    base = _enable_kernel(monkeypatch, tmp_path)
    configs = tmp_path / "configs"
    configs.mkdir()
    cfg = AdapterConfig(model="test", config_path=str(configs), workspace_id="w")

    # Call the concrete hook off the base class (it doesn't touch self).
    BaseAdapter.append_to_memory_hook(
        types.SimpleNamespace(), cfg, "CLAUDE.md", "# Plugin: demo\nhello\n"
    )
    mailbox_copy = base / "memory" / "CLAUDE.md"
    assert mailbox_copy.exists(), "append-to-memory must write the durable mailbox copy"
    assert "hello" in mailbox_copy.read_text(encoding="utf-8")
    # And NOT the legacy /configs path.
    assert not (configs / "CLAUDE.md").exists()


def test_append_to_memory_hook_writes_configs_when_off(tmp_path, monkeypatch):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter

    configs = tmp_path / "configs"
    configs.mkdir()
    cfg = AdapterConfig(model="test", config_path=str(configs), workspace_id="w")
    BaseAdapter.append_to_memory_hook(
        types.SimpleNamespace(), cfg, "CLAUDE.md", "# Plugin: demo\nhello\n"
    )
    assert (configs / "CLAUDE.md").exists(), "kernel-off keeps the legacy /configs target"


def test_consolidation_mirror_appends_to_mailbox(tmp_path, monkeypatch):
    # consolidation validates WORKSPACE_ID at import — set it before importing.
    monkeypatch.setenv("WORKSPACE_ID", "00000000-0000-0000-0000-0000deadbeef")
    import molecule_runtime.consolidation as consolidation

    base = _enable_kernel(monkeypatch, tmp_path)
    (base / "memory").mkdir(parents=True)
    consolidation._mirror_consolidated_to_mailbox("a distilled fact")
    mem = (base / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "a distilled fact" in mem
    assert "[Consolidated]" in mem


def test_consolidation_mirror_noop_when_off(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ID", "00000000-0000-0000-0000-0000deadbeef")
    import molecule_runtime.consolidation as consolidation

    # No exception, no file written when kernel off (explicit opt-out).
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    consolidation._mirror_consolidated_to_mailbox("ignored")  # must not raise


def test_kernel_on_falls_back_to_configs_when_mailbox_copy_absent(tmp_path, monkeypatch):
    """Review R3 (core#4295 class): with the kernel ON but NO mailbox memory
    copy (first boot pre-migration, or an unwritable volume where the migrator
    could not run), the auto-loaded snapshots must fall back to the legacy
    /configs copy instead of silently dropping accumulated memory from the
    prompt. A PRESENT mailbox copy still wins (stale-shadow rule unchanged —
    pinned by test_stale_configs_never_shadows_fresh_mailbox_memory)."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    (configs / "MEMORY.md").write_text("LEGACY-ONLY-MEMORY", encoding="utf-8")
    base = _enable_kernel(monkeypatch, tmp_path)  # mailbox memory dir stays EMPTY

    out = build_system_prompt(
        config_path=str(configs), workspace_id="w", loaded_skills=[], peers=[]
    )
    assert "LEGACY-ONLY-MEMORY" in out, (
        "kernel-on with no mailbox copy must not lobotomize the agent — "
        "the legacy /configs snapshot is the only memory there is"
    )
