"""Skills-surfacing PORT tests — the per-runtime render matrix + the
live-runtime completeness guardrail.

Third sibling of ``test_mcp_render_completeness_g6`` (tools) and
``test_persona_render`` (identity): these pin that each runtime's *skills*
surface — the canonical ``<configs>/skills`` dir where the AgentskillsAdaptor
installs plugin skills — is materialized into the location THAT runtime's
native binary actually scans, and critically:

* codex/openclaw do NOT get the claude ``~/.claude/skills`` link (the skills
  analogue of the #3159 cross-runtime mis-attribution);
* an UNMAPPED runtime fails LOUD instead of silently falling back to the
  claude convention (skills have no universal base location);
* materialization is directory-level and idempotent (a post-boot plugin
  install into /configs/skills is visible without re-materializing);
* a REAL directory squatting a link target is never clobbered (the
  template-claude-code#224 guard), and the refusal is an error, not a no-op.
"""
from __future__ import annotations

import os

import pytest
import yaml

from molecule_runtime import skills_render
from molecule_runtime.skills_render import SkillsMaterializeError

# Symlink materializers need a symlink-capable filesystem; on Windows dev
# boxes os.symlink needs Developer Mode. CI (Linux) always runs these.
requires_symlink = pytest.mark.skipif(
    os.name == "nt" and not hasattr(os, "symlink"),
    reason="symlink support required",
)

# The 5 live runtimes (runtimes.yaml SSOT after the 4-runtime removal). Every
# one must have an explicit, verified stance here — concrete materializer or
# documented prompt-embedded — never an accidental fallback.
LIVE_RUNTIMES = ("claude-code", "codex", "openclaw", "google-adk", "hermes")


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # expanduser on Windows
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    return home


def _configs(tmp_path):
    configs = tmp_path / "configs"
    (configs / "skills" / "probe-skill").mkdir(parents=True, exist_ok=True)
    (configs / "skills" / "probe-skill" / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: probe\n---\n\nDo the probe.\n"
    )
    return configs


# ---------------------------------------------------------------------------
# Render matrix — each runtime materializes into ITS native surface.
# ---------------------------------------------------------------------------

@requires_symlink
def test_claude_code_links_personal_skills_dir(tmp_path, monkeypatch):
    """claude-code: ``~/.claude/skills`` becomes a symlink to /configs/skills
    — the SDK-owned generalization of template-claude-code#224."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    target = skills_render.materialize_skills_for("claude-code", configs)

    assert target == home / ".claude" / "skills"
    assert target.is_symlink()
    assert (target / "probe-skill" / "SKILL.md").is_file()


@requires_symlink
def test_codex_links_nested_group_not_the_root(tmp_path, monkeypatch):
    """codex: the link is the NESTED ``$CODEX_HOME/skills/molecule`` group —
    the skills ROOT stays a real dir so codex's self-materialized ``.system``
    skills never land inside /configs/skills (pinned vs codex 0.130.0)."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    target = skills_render.materialize_skills_for("codex", configs)

    assert target == home / ".codex" / "skills" / "molecule"
    assert target.is_symlink()
    assert not (home / ".codex" / "skills").is_symlink()
    assert (target / "probe-skill" / "SKILL.md").is_file()
    # The #3159-analogue: codex must NOT have touched the claude location.
    assert not (home / ".claude" / "skills").exists()


@requires_symlink
def test_codex_honors_codex_home_env(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    codex_home = tmp_path / "custom-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    target = skills_render.materialize_skills_for("codex", configs)

    assert target == codex_home / "skills" / "molecule"
    assert target.is_symlink()


@requires_symlink
def test_openclaw_links_workspace_skills_root(tmp_path, monkeypatch):
    """openclaw: ``<workspace>/skills`` (the gateway's highest-precedence
    native skill root, workspace resolved by openclaw's own profile rule)."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    target = skills_render.materialize_skills_for("openclaw", configs)

    assert target == home / ".openclaw" / "workspace" / "skills"
    assert target.is_symlink()
    assert (target / "probe-skill" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills").exists()


@requires_symlink
def test_openclaw_profile_partitions_workspace(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    monkeypatch.setenv("OPENCLAW_PROFILE", "dev")

    target = skills_render.materialize_skills_for("openclaw", configs)

    assert target == home / ".openclaw" / "workspace-dev" / "skills"
    # And the default-profile spelling stays untouched.
    monkeypatch.setenv("OPENCLAW_PROFILE", "default")
    assert skills_render.skills_target_for("openclaw", configs) == (
        home / ".openclaw" / "workspace" / "skills"
    )


def test_hermes_merges_external_dirs_into_config_yaml(tmp_path, monkeypatch):
    """hermes: a config pointer (``skills.external_dirs``) in
    ``$HERMES_HOME/config.yaml`` — hermes' first-class external-skills
    mechanism, re-read on every native scan. Existing config keys survive."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"inference": {"model": "hermes-4"}, "skills": {"auto_curate": True}})
    )

    target = skills_render.materialize_skills_for("hermes", configs)

    assert target == hermes_home / "config.yaml"
    data = yaml.safe_load(target.read_text())
    assert data["skills"]["external_dirs"] == [str(configs / "skills")]
    # Pre-existing keys preserved — the merge is additive.
    assert data["inference"] == {"model": "hermes-4"}
    assert data["skills"]["auto_curate"] is True
    # hermes only honors dirs that EXIST — the materializer created the source.
    assert (configs / "skills").is_dir()


def test_hermes_creates_config_when_absent(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    target = skills_render.materialize_skills_for("hermes", configs)

    data = yaml.safe_load(target.read_text())
    assert data["skills"]["external_dirs"] == [str(configs / "skills")]
    assert target == home / ".hermes" / "config.yaml"


def test_hermes_honors_hermes_home_env(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    hermes_home = tmp_path / "custom-hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    target = skills_render.materialize_skills_for("hermes", configs)
    assert target == hermes_home / "config.yaml"


def test_hermes_refuses_unparseable_config(tmp_path, monkeypatch):
    """A corrupt hermes config is a loud SkillsMaterializeError, never an
    overwrite — hermes may still partially honor the file."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text("inference: [unclosed\n  bad: {{{\n")

    with pytest.raises(SkillsMaterializeError):
        skills_render.materialize_skills_for("hermes", configs)


def test_google_adk_is_a_documented_prompt_embedded_skip(tmp_path):
    """google-adk: no on-disk discovery — the documented skip returns None
    (its skills ride the assembled instruction via _common_setup)."""
    configs = _configs(tmp_path)
    assert skills_render.materialize_skills_for("google-adk", configs) is None
    assert skills_render.skills_target_for("google-adk", configs) is None
    assert not skills_render.is_skills_supported("google-adk")
    assert skills_render.is_skills_satisfied("google-adk")


# ---------------------------------------------------------------------------
# Fail-loud stances — no silent no-op, no silent claude fallback.
# ---------------------------------------------------------------------------

def test_gemini_is_unverified_failloud_stub(tmp_path):
    configs = _configs(tmp_path)
    with pytest.raises(NotImplementedError):
        skills_render.materialize_skills_for("gemini", configs)
    assert not skills_render.is_skills_supported("gemini")
    assert not skills_render.is_skills_satisfied("gemini")


def test_unmapped_runtime_fails_loud_never_claude_fallback(tmp_path, monkeypatch):
    """UNLIKE the mcp/persona ports there is NO claude default: an unmapped
    runtime raises instead of silently linking ~/.claude/skills (a location
    its runtime never scans — the #3159 flaw)."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    with pytest.raises(NotImplementedError):
        skills_render.materialize_skills_for("some-future-runtime", configs)
    with pytest.raises(NotImplementedError):
        skills_render.skills_target_for("some-future-runtime", configs)
    assert not (home / ".claude" / "skills").exists()


@requires_symlink
def test_real_dir_at_link_target_is_never_clobbered(tmp_path, monkeypatch):
    """The #224 guard: a REAL ~/.claude/skills dir (agent-authored skills)
    is left alone — and the refusal is a loud error, not a silent no-op."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    real_dir = home / ".claude" / "skills"
    real_dir.mkdir(parents=True)
    (real_dir / "hand-authored.md").write_text("mine")

    with pytest.raises(SkillsMaterializeError):
        skills_render.materialize_skills_for("claude-code", configs)

    assert real_dir.is_dir() and not real_dir.is_symlink()
    assert (real_dir / "hand-authored.md").read_text() == "mine"


# ---------------------------------------------------------------------------
# Idempotency + dir-level semantics (post-boot installs need no re-run).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", ["claude-code", "codex", "openclaw"])
@requires_symlink
def test_symlink_materializers_are_idempotent(runtime, tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    first = skills_render.materialize_skills_for(runtime, configs)
    second = skills_render.materialize_skills_for(runtime, configs)

    assert first == second
    assert second.is_symlink()


def test_hermes_materializer_is_idempotent(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    skills_render.materialize_skills_for("hermes", configs)
    target = skills_render.materialize_skills_for("hermes", configs)

    data = yaml.safe_load(target.read_text())
    # Re-running never duplicates the entry.
    assert data["skills"]["external_dirs"] == [str(configs / "skills")]


@requires_symlink
def test_stale_symlink_is_repointed(tmp_path, monkeypatch):
    """A wrong/stale link (e.g. restored by a backup rsync from an older
    layout) is safely re-pointed — only the LINK is replaced."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)
    stale_src = tmp_path / "old-location"
    stale_src.mkdir()
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    os.symlink(str(stale_src), str(claude_dir / "skills"), target_is_directory=True)

    target = skills_render.materialize_skills_for("claude-code", configs)

    assert target.is_symlink()
    assert os.readlink(target) == str(configs / "skills")
    assert stale_src.is_dir()  # the old real dir itself is untouched


@requires_symlink
def test_dir_level_link_sees_post_boot_installs(tmp_path, monkeypatch):
    """The contract's whole point: a skill installed into /configs/skills
    AFTER materialization is immediately visible through the native surface."""
    home = _home(monkeypatch, tmp_path)
    configs = _configs(tmp_path)

    skills_render.materialize_skills_for("claude-code", configs)

    late = configs / "skills" / "late-skill"
    late.mkdir()
    (late / "SKILL.md").write_text("---\nname: late-skill\ndescription: d\n---\nx\n")

    assert (home / ".claude" / "skills" / "late-skill" / "SKILL.md").is_file()


@requires_symlink
def test_materializer_creates_missing_canonical_dir(tmp_path, monkeypatch):
    """A fresh workspace with no skills yet still gets a valid native surface
    (link to an existing empty dir, never a dangling link)."""
    _home(monkeypatch, tmp_path)
    configs = tmp_path / "configs"
    configs.mkdir()

    target = skills_render.materialize_skills_for("claude-code", configs)

    assert (configs / "skills").is_dir()
    assert target.is_symlink()


# ---------------------------------------------------------------------------
# Completeness guardrail — every LIVE runtime has an explicit, verified stance.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", LIVE_RUNTIMES)
def test_every_live_runtime_is_explicitly_mapped(runtime):
    """No live runtime may hit the unmapped fail-loud path by accident: each
    of the 5 has an explicit _RUNTIME_SKILLS entry (concrete or documented)."""
    from molecule_runtime.mcp_render import normalize_runtime

    assert normalize_runtime(runtime) in skills_render._RUNTIME_SKILLS


@pytest.mark.parametrize("runtime", LIVE_RUNTIMES)
def test_every_live_runtime_is_satisfied(runtime):
    """Contract-level assertion: plugin skills verifiably reach every live
    runtime's agent (on-disk materializer or documented prompt path)."""
    assert skills_render.is_skills_satisfied(runtime)


@pytest.mark.parametrize(
    "runtime,expected_tail",
    [
        ("claude-code", (".claude", "skills")),
        ("codex", (".codex", "skills", "molecule")),
        ("openclaw", (".openclaw", "workspace", "skills")),
        ("hermes", (".hermes", "config.yaml")),
    ],
)
def test_native_targets_are_runtime_specific(runtime, expected_tail, tmp_path, monkeypatch):
    """The render matrix: each runtime's target is ITS OWN native location —
    the test class that would have caught a #3159-style mis-attribution."""
    _home(monkeypatch, tmp_path)
    target = skills_render.skills_target_for(runtime, tmp_path / "configs")
    assert target.parts[-len(expected_tail):] == expected_tail
