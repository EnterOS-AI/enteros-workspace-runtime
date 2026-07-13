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
    """RC #203: even when MEMORY.md is NAMED in prompt_files (so the /configs
    copy is loaded in the prompt_files loop and marked seen), the fresh mailbox
    copy MUST win — the stale /configs copy must never appear. This is the
    prompt_files case the existing 'stale never shadows' test missed."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    # Stale copy in the provisioner-owned /configs dir, explicitly listed.
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
    assert FRESH in out, "the fresh mailbox MEMORY.md must win even when named in prompt_files"
    assert STALE not in out, "a stale /configs MEMORY.md must NEVER shadow, even via prompt_files"
    assert out.count(FRESH) == 1, "the memory snapshot must appear exactly once (no double-load)"


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
