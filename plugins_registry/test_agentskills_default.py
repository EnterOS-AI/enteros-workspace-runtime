"""RFC#2843 #32 — skill-shaped plugins resolve to AgentskillsAdaptor and
install into /configs/skills/ (where Claude Code reads), even with the
SKILL.md-at-root shape (e.g. the seo-all skill). Before this fix resolve()
fell to RawDropAdaptor and the skill was dropped-but-never-activated.
"""
import asyncio
import logging
from pathlib import Path

from molecule_runtime.plugins_registry import AdaptorSource, resolve
from molecule_runtime.plugins_registry.builtins import AgentskillsAdaptor
from molecule_runtime.plugins_registry.protocol import InstallContext


def _mk_root_skill(tmp_path: Path, name: str) -> Path:
    """A SKILL.md-at-root plugin (seo-all shape): SKILL.md + commands/ + plugin.yaml."""
    root = tmp_path / "plugins" / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: %s\n---\nDo SEO.\n" % name)
    (root / "plugin.yaml").write_text("name: %s\nskills: [%s]\n" % (name, name))
    (root / "commands").mkdir()
    (root / "commands" / "audit.md").write_text("# audit\n")
    return root


def test_root_skill_resolves_to_agentskills_not_rawdrop(tmp_path):
    root = _mk_root_skill(tmp_path, "seo-all")
    adaptor, source = resolve("seo-all", "claude_code", root)
    assert isinstance(adaptor, AgentskillsAdaptor), f"got {type(adaptor).__name__}"
    assert source == AdaptorSource.AGENTSKILLS, source


def test_root_skill_installs_into_configs_skills(tmp_path):
    root = _mk_root_skill(tmp_path, "seo-all")
    configs = tmp_path / "configs"
    configs.mkdir()
    adaptor, _ = resolve("seo-all", "claude_code", root)
    ctx = InstallContext(configs_dir=configs, runtime="claude_code", plugin_root=root, workspace_id="ws-test")
    asyncio.run(adaptor.install(ctx))
    skill = configs / "skills" / "seo-all"
    assert (skill / "SKILL.md").is_file(), "SKILL.md must land in /configs/skills/<name>/"
    assert (skill / "commands" / "audit.md").is_file(), "skill companion files must come along"
    # SKILL.md must NOT have been double-counted as a memory fragment.
    claude_md = configs / "CLAUDE.md"
    if claude_md.exists():
        assert "Do SEO." not in claude_md.read_text() or "fragment: SKILL.md" not in claude_md.read_text()


def test_plain_plugin_still_falls_to_rawdrop(tmp_path):
    # A non-skill plugin (no SKILL.md, no skills/) keeps the RawDrop fallback.
    root = tmp_path / "plugins" / "thing"
    root.mkdir(parents=True)
    (root / "data.txt").write_text("x")
    _, source = resolve("thing", "claude_code", root)
    assert source == AdaptorSource.RAW_DROP, source
