"""Ownership-aware install, reconcile, and uninstall behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from molecule_runtime.plugins_registry.builtins import AgentskillsAdaptor
from molecule_runtime.plugins_registry.protocol import InstallContext


def _context(plugin_root: Path, configs: Path) -> InstallContext:
    def append(filename: str, content: str) -> None:
        target = configs / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(content + "\n")

    return InstallContext(
        configs_dir=configs,
        workspace_id="workspace",
        runtime="claude-code",
        plugin_root=plugin_root,
        append_to_memory=append,
    )


@pytest.fixture(autouse=True)
def _memory_in_configs_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route plugin memory to the plugin's ``configs_dir`` for these tests.

    These ownership tests model memory (and owned files) under ``configs_dir``:
    the ``_context()`` append closure writes ``configs/<file>`` and the
    assertions read it. With the mailbox kernel ON (the default) memory instead
    lands in the durable mailbox memory dir, so the writer and
    ``_runtime_memory_path`` would disagree and ownership would never be
    recorded. Pin the kernel OFF so both resolve to ``configs/<file>``. The
    kernel-ON mailbox memory path is covered explicitly by
    ``test_mailbox_memory_path_is_owned_and_removed``, which re-enables it.
    """
    monkeypatch.setenv("MOLECULE_MAILBOX_KERNEL", "0")


@pytest.mark.asyncio
async def test_uninstall_preserves_preexisting_skill_directory(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "shared").mkdir(parents=True)
    (plugin / "skills" / "shared" / "SKILL.md").write_text("plugin")
    configs = tmp_path / "configs"
    existing = configs / "skills" / "shared" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("user")
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)

    await adaptor.install(ctx)
    await adaptor.uninstall(ctx)

    assert existing.read_text() == "user"


@pytest.mark.asyncio
async def test_reinstall_updates_and_retires_only_unchanged_owned_files(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    skill = plugin / "skills" / "owned"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("one")
    (skill / "retired.md").write_text("retire")
    configs = tmp_path / "configs"
    configs.mkdir()
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)
    await adaptor.install(ctx)

    installed = configs / "skills" / "owned"
    (skill / "SKILL.md").write_text("two")
    (skill / "retired.md").unlink()
    await adaptor.install(ctx)

    assert (installed / "SKILL.md").read_text() == "two"
    assert not (installed / "retired.md").exists()


@pytest.mark.asyncio
async def test_uninstall_preserves_modified_owned_file(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    skill = plugin / "skills" / "owned"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("plugin")
    configs = tmp_path / "configs"
    configs.mkdir()
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)
    await adaptor.install(ctx)
    installed = configs / "skills" / "owned" / "SKILL.md"
    installed.write_text("user modified")

    await adaptor.uninstall(ctx)

    assert installed.read_text() == "user modified"


@pytest.mark.asyncio
async def test_uninstall_removes_exact_memory_block_only(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "rules").mkdir(parents=True)
    (plugin / "rules" / "rule.md").write_text("plugin rule")
    configs = tmp_path / "configs"
    configs.mkdir()
    memory = configs / "CLAUDE.md"
    memory.write_text("before\n")
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)
    await adaptor.install(ctx)
    with memory.open("a") as handle:
        handle.write("after\n")

    await adaptor.uninstall(ctx)

    assert memory.read_text() == "before\nafter\n"


@pytest.mark.asyncio
async def test_uninstall_preserves_modified_memory_block(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "rules").mkdir(parents=True)
    (plugin / "rules" / "rule.md").write_text("plugin rule")
    configs = tmp_path / "configs"
    configs.mkdir()
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)
    await adaptor.install(ctx)
    memory = configs / "CLAUDE.md"
    memory.write_text(memory.read_text().replace("plugin rule", "user edited"))

    await adaptor.uninstall(ctx)

    assert "user edited" in memory.read_text()


@pytest.mark.asyncio
async def test_uninstall_subtracts_only_plugin_settings_and_hooks(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "owned.sh").write_text("#!/bin/sh\n")
    (plugin / "settings-fragment.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_DIR}/hooks/owned.sh",
                                }
                            ],
                        }
                    ]
                },
                "theme": "plugin",
            }
        )
    )
    configs = tmp_path / "configs"
    settings = configs / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    user_handler = {
        "matcher": "Read",
        "hooks": [{"type": "command", "command": "echo user"}],
    }
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [user_handler]}, "user": True})
    )
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)

    await adaptor.install(ctx)
    await adaptor.uninstall(ctx)

    assert json.loads(settings.read_text()) == {
        "hooks": {"PreToolUse": [user_handler]},
        "user": True,
    }
    assert not (configs / ".claude" / "hooks" / "owned.sh").exists()


@pytest.mark.asyncio
async def test_legacy_uninstall_without_record_is_safe(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "legacy").mkdir(parents=True)
    configs = tmp_path / "configs"
    installed = configs / "skills" / "legacy" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("legacy")

    await AgentskillsAdaptor("demo", "claude-code").uninstall(
        _context(plugin, configs)
    )

    assert installed.read_text() == "legacy"


@pytest.mark.asyncio
async def test_mailbox_memory_path_is_owned_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "rules").mkdir(parents=True)
    (plugin / "rules" / "rule.md").write_text("durable rule")
    configs = tmp_path / "configs"
    configs.mkdir()
    mailbox = tmp_path / "mailbox"
    monkeypatch.setenv("MOLECULE_MAILBOX_KERNEL", "1")
    monkeypatch.setenv("MOLECULE_MAILBOX_DIR", str(mailbox))

    import molecule_runtime.mailbox_dir as mailbox_dir

    def append(filename: str, content: str) -> None:
        target = mailbox_dir.memory_file(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content + "\n")

    ctx = InstallContext(
        configs_dir=configs,
        workspace_id="workspace",
        runtime="claude-code",
        plugin_root=plugin,
        append_to_memory=append,
    )
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    await adaptor.install(ctx)
    memory = mailbox_dir.memory_file("CLAUDE.md")
    assert "durable rule" in memory.read_text()

    await adaptor.uninstall(ctx)

    assert memory.read_text() == ""


@pytest.mark.asyncio
async def test_tampered_memory_ownership_cannot_escape_configs(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "rules").mkdir(parents=True)
    (plugin / "rules" / "rule.md").write_text("owned rule")
    configs = tmp_path / "configs"
    configs.mkdir()
    ctx = _context(plugin, configs)
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    await adaptor.install(ctx)
    ownership = next((configs / ".molecule" / "plugin-ownership").glob("*.json"))
    state = json.loads(ownership.read_text())
    victim = tmp_path / "victim.md"
    victim.write_text((configs / "CLAUDE.md").read_text())
    state["memory"]["filename"] = str(victim)
    state["memory"]["path"] = str(victim)
    ownership.write_text(json.dumps(state))

    await adaptor.uninstall(ctx)

    assert "owned rule" in victim.read_text()


@pytest.mark.asyncio
async def test_memory_symlink_escape_is_rejected_before_append(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "rules").mkdir(parents=True)
    (plugin / "rules" / "rule.md").write_text("owned rule")
    configs = tmp_path / "configs"
    configs.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text("user content\n")
    (configs / "CLAUDE.md").symlink_to(victim)
    ctx = _context(plugin, configs)

    with pytest.raises(ValueError, match="memory path escapes"):
        await AgentskillsAdaptor("demo", "claude-code").install(ctx)

    assert victim.read_text() == "user content\n"


@pytest.mark.asyncio
async def test_privileged_setup_failure_persists_partial_ownership(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "owned").mkdir(parents=True)
    (plugin / "skills" / "owned" / "SKILL.md").write_text("owned")
    (plugin / "setup.sh").write_text("#!/bin/sh\nexit 1\n")
    configs = tmp_path / "configs"
    configs.mkdir()
    ctx = _context(plugin, configs)
    adaptor = AgentskillsAdaptor("molecule-platform-mcp", "claude-code")

    with pytest.raises(RuntimeError, match="setup.sh exited 1"):
        await adaptor.install(ctx)

    assert list((configs / ".molecule" / "plugin-ownership").glob("*.json"))
    await adaptor.uninstall(ctx)
    assert not (configs / "skills" / "owned").exists()


@pytest.mark.asyncio
async def test_uninstall_preserves_symlinked_owned_path_target(tmp_path: Path) -> None:
    """An owned file swapped for an in-root symlink must not let uninstall delete
    the symlink's target.

    The victim shares the recorded content, so the SHA-match guard alone would
    pass if the recorded path were resolved through the symlink — the no-follow
    _owned_path rejects the symlinked recorded leaf and preserves the replacement.
    """
    shared = "shared-content\n"
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "s1").mkdir(parents=True)
    (plugin / "skills" / "s1" / "SKILL.md").write_text(shared)
    configs = tmp_path / "configs"
    configs.mkdir()
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)
    await adaptor.install(ctx)

    installed = configs / "skills" / "s1" / "SKILL.md"
    assert installed.read_text() == shared
    victim = configs / "victim.md"
    victim.write_text(shared)
    installed.unlink()
    installed.symlink_to(victim)

    await adaptor.uninstall(ctx)

    assert victim.exists(), "uninstall followed the symlink and deleted its target"
    assert victim.read_text() == shared
    assert installed.is_symlink(), "the symlink replacement was not preserved"


@pytest.mark.asyncio
async def test_update_does_not_write_through_symlinked_owned_path(tmp_path: Path) -> None:
    """A reinstall/update must not copy a new plugin file THROUGH a symlink that
    has replaced an owned destination.

    The victim shares the previously-installed content, so the previous-digest
    guard would pass if the destination symlink were followed — the no-follow
    guard preserves the symlinked destination instead of overwriting its target.
    """
    plugin = tmp_path / "plugin"
    skill = plugin / "skills" / "s1"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("v1\n")
    configs = tmp_path / "configs"
    configs.mkdir()
    adaptor = AgentskillsAdaptor("demo", "claude-code")
    ctx = _context(plugin, configs)
    await adaptor.install(ctx)

    installed = configs / "skills" / "s1" / "SKILL.md"
    victim = configs / "victim.md"
    victim.write_text("v1\n")
    installed.unlink()
    installed.symlink_to(victim)

    (skill / "SKILL.md").write_text("v2\n")
    await adaptor.install(ctx)

    assert victim.read_text() == "v1\n", "update wrote through the symlink onto the victim"
    assert installed.is_symlink(), "the symlink replacement was not preserved"
