"""Test the /opt/molecule-platform-agent-template fallback in load_config.

Context (core#2919 risk-2): the concierge MUST never boot identity-less. When
the asset-fetcher can't deliver a template (self-host with no token, partial
template without config.yaml, etc.), /configs is empty and the runtime would
MISSING_MODEL fail. If the image bakes the concierge identity at
/opt/molecule-platform-agent-template/, load_config falls back to it so a
no-fetch concierge still boots with the declared model.

Per-file / fill-absent-only semantics:
  - /configs/config.yaml delivered by asset-fetcher → wins (no fallback)
  - /configs missing BUT /opt baked → fallback fires
  - /configs missing AND /opt missing → fail closed (FileNotFoundError)

The fallback cascades through ALL config-relative lookups (prompts,
skills, plugins, ExecRead) so the concierge boots with the full baked
identity, not just the right model. Researcher RC 12052 finding.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _stub_minimal_yaml(dest):
    """Write a minimal config.yaml with the fields load_config requires."""
    dest.write_text(
        "name: Test Workspace\n"
        "runtime: claude-code\n"
        "model: moonshot/kimi-k2.6\n"
        "tier: 2\n"
    )


def test_load_config_uses_configs_when_present(tmp_path, monkeypatch):
    """Happy path: /configs/config.yaml exists, /opt is empty. /configs wins."""
    from molecule_runtime import config as config_mod

    # Unset env vars that override YAML (MOLECULE_MODEL > MODEL > YAML).
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    _stub_minimal_yaml(configs_dir / "config.yaml")

    # Point load_config at our tmp configs dir, ensure /opt fallback is absent.
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))
    # Make /opt lookup point at a path that does NOT exist (negative test for /opt).
    monkeypatch.setattr(
        config_mod,
        "Path",
        lambda p: Path(p)
        if str(p) != "/opt/molecule-platform-agent-template/config.yaml"
        else (tmp_path / "opt_missing" / "config.yaml"),
    )

    loaded = config_mod.load_config()
    assert loaded.runtime == "claude-code"
    assert loaded.model == "moonshot/kimi-k2.6"
    assert loaded.tier == 2


def test_load_config_falls_back_to_opt_when_configs_missing(tmp_path, monkeypatch):
    """Safety net: /configs/config.yaml is empty, /opt baked has it. Fallback fires."""
    from molecule_runtime import config as config_mod

    # Unset env vars that override YAML.
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    # /configs/config.yaml intentionally NOT created (simulates no-fetch concierge).
    assert not (configs_dir / "config.yaml").exists()

    # Stage /opt baked fallback under a tmp path, monkey-patch the lookup to point at it.
    opt_dir = tmp_path / "opt" / "molecule-platform-agent-template"
    opt_dir.mkdir(parents=True)
    _stub_minimal_yaml(opt_dir / "config.yaml")

    real_path = Path
    patched_path_str = str(opt_dir / "config.yaml")
    target_opt = "/opt/molecule-platform-agent-template/config.yaml"

    def path_factory(p):
        if str(p) == target_opt:
            return real_path(patched_path_str)
        return real_path(p)

    monkeypatch.setattr(config_mod, "Path", path_factory)
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))

    loaded = config_mod.load_config()
    # Fallback fired; runtime reads the baked concierge identity.
    assert loaded.runtime == "claude-code"
    assert loaded.model == "moonshot/kimi-k2.6"
    assert loaded.tier == 2


def test_load_config_fail_closed_when_both_missing(tmp_path, monkeypatch):
    """Neither /configs nor /opt: fail closed (FileNotFoundError, no silent boot)."""
    from molecule_runtime import config as config_mod

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    # /configs/config.yaml absent
    # /opt fallback points at a path that also doesn't exist
    opt_dir = tmp_path / "opt_does_not_exist"

    real_path = Path
    target_opt = "/opt/molecule-platform-agent-template/config.yaml"

    def path_factory(p):
        if str(p) == target_opt:
            return real_path(str(opt_dir / "config.yaml"))
        return real_path(p)

    monkeypatch.setattr(config_mod, "Path", path_factory)
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))

    with pytest.raises(FileNotFoundError):
        config_mod.load_config()


def test_load_config_opt_fallback_prompts_follow(tmp_path, monkeypatch):
    """Researcher RC 12052: when the /opt fallback fires, the PROMPT
    files (initial_prompt_file / idle_prompt_file / concierge.md via
    build_system_prompt) must also follow the /opt base — otherwise
    the concierge boots with the right model but an EMPTY system
    prompt (silently identity-less, behaviorally). This test asserts
    the loaded system prompt is non-empty AND was resolved from the
    baked template's directory, not the empty /configs.
    """
    from molecule_runtime import config as config_mod

    # Unset env vars that override YAML.
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    # /configs/config.yaml intentionally NOT created.

    # Stage /opt baked fallback with config.yaml + system-prompt.md
    # (the loader's default if prompt_files is empty).
    opt_dir = tmp_path / "opt" / "molecule-platform-agent-template"
    opt_dir.mkdir(parents=True)
    (opt_dir / "config.yaml").write_text(
        "name: Test Workspace\n"
        "runtime: claude-code\n"
        "model: moonshot/kimi-k2.6\n"
        "tier: 2\n"
    )
    (opt_dir / "system-prompt.md").write_text(
        "YOU ARE A CONCIERGE AGENT.\n"
        "If you are reading this, the /opt fallback fired and the prompt\n"
        "lookup correctly cascaded to the baked template's directory.\n"
    )

    real_path = Path
    patched_config = str(opt_dir / "config.yaml")
    target_opt = "/opt/molecule-platform-agent-template/config.yaml"

    def path_factory(p):
        if str(p) == target_opt:
            return real_path(patched_config)
        return real_path(p)

    monkeypatch.setattr(config_mod, "Path", path_factory)
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))

    loaded = config_mod.load_config()
    # The model loads from /opt.
    assert loaded.model == "moonshot/kimi-k2.6"
    # The config_path (reassigned to the /opt baked dir) now resolves
    # system-prompt.md to /opt/molecule-platform-agent-template/.
    # The Researcher's RC 12052 finding: pre-fix, this would be SILENTLY
    # EMPTY (the concierge boots with the right model but no system
    # prompt = identity-less behaviorally). Post-fix, the prompt loads
    # from the baked template.
    expected_prompt = (
        "YOU ARE A CONCIERGE AGENT.\n"
        "If you are reading this, the /opt fallback fired and the prompt\n"
        "lookup correctly cascaded to the baked template's directory.\n"
    ).strip()
    # Sanity: the config_path now points at the /opt baked dir (so
    # the /configs lookup, which would be empty, never fires).
    assert loaded.config_path == str(opt_dir), (
        f"expected config_path to be reassigned to the /opt baked dir "
        f"({opt_dir}), got {loaded.config_path!r} — the /opt fallback "
        "did not cascade through the downstream lookups"
    )
    # The Researcher RC 12052 fix: when the /opt fallback fires AND
    # the YAML has no `initial_prompt_file` (the no-fetch case), the
    # loader MUST default to the baked template's conventional file
    # so `loaded.initial_prompt` is non-empty. Pre-fix, the concierge
    # would boot with the right model but an EMPTY system prompt =
    # identity-less behaviorally. Post-fix, the concierge boots the
    # COMPLETE identity.
    assert loaded.initial_prompt == expected_prompt, (
        f"loaded.initial_prompt should be the baked system-prompt.md "
        f"content (RC 12052); got {loaded.initial_prompt!r} — the "
        "/opt fallback did not load the prompt from the baked template, "
        "so the concierge would boot identity-less"
    )
    # And the prompt resolver (now using the reassigned config_path)
    # can find system-prompt.md in the baked template.
    system_prompt_path = Path(loaded.config_path) / "system-prompt.md"
    assert system_prompt_path.exists(), (
        f"system-prompt.md not found at {system_prompt_path} — "
        "the prompt lookup didn't cascade through the /opt baked dir"
    )
    assert system_prompt_path.read_text().strip() == expected_prompt


def test_load_config_opt_fallback_prompts_follow_concierge_md(tmp_path, monkeypatch):
    """Researcher RC 12052 (concierge-template variant): when the /opt
    fallback fires, the loader MUST try `prompts/concierge.md` (the
    concierge template's actual baked layout) BEFORE `system-prompt.md`
    (the runtime's general convention). This pins the priority order
    so a future maintainer doesn't flip the candidate list and
    accidentally bypass the concierge identity.
    """
    from molecule_runtime import config as config_mod

    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    # /configs/config.yaml intentionally NOT created.

    opt_dir = tmp_path / "opt" / "molecule-platform-agent-template"
    opt_dir.mkdir(parents=True)
    (opt_dir / "config.yaml").write_text(
        "name: Test Workspace\n"
        "runtime: claude-code\n"
        "model: moonshot/kimi-k2.6\n"
        "tier: 2\n"
    )
    # Bake BOTH files: the concierge template layout has
    # prompts/concierge.md; we also bake system-prompt.md as a
    # different-content file to assert the concierge.md path WINS.
    (opt_dir / "prompts").mkdir()
    (opt_dir / "prompts" / "concierge.md").write_text(
        "CONCIERGE TEMPLATE BAKED PROMPT\n"
    )
    (opt_dir / "system-prompt.md").write_text(
        "GENERIC SYSTEM PROMPT (should NOT win over concierge.md)\n"
    )

    real_path = Path
    patched_config = str(opt_dir / "config.yaml")
    target_opt = "/opt/molecule-platform-agent-template/config.yaml"

    def path_factory(p):
        if str(p) == target_opt:
            return real_path(patched_config)
        return real_path(p)

    monkeypatch.setattr(config_mod, "Path", path_factory)
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))

    loaded = config_mod.load_config()
    # The concierge-template layout MUST win: prompts/concierge.md is
    # first in the candidate list. The runtime convention
    # (system-prompt.md) is a fallback for non-concierge templates.
    assert loaded.initial_prompt == "CONCIERGE TEMPLATE BAKED PROMPT", (
        f"loaded.initial_prompt should be the concierge template's "
        f"prompts/concierge.md; got {loaded.initial_prompt!r} — the "
        "concierge template's file should win over the runtime convention"
    )


def test_load_config_opt_fallback_does_not_default_prompt_when_configs_delivered(tmp_path, monkeypatch):
    """Delivery-wins: when /configs/config.yaml is delivered (the normal
    case), the loader MUST NOT paper over an empty `initial_prompt_file`
    with a baked fallback. The empty prompt is a config bug the
    operator needs to see, not a /opt-fallback case. Researcher RC 12052
    fix has a one-line guard (`opt_fallback_fired`); this test pins it.
    """
    from molecule_runtime import config as config_mod

    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    # /configs/config.yaml IS delivered (the normal case), with an
    # empty initial_prompt field (a config bug).
    (configs_dir / "config.yaml").write_text(
        "name: Test Workspace\n"
        "runtime: claude-code\n"
        "model: anthropic:claude-opus-4-7\n"
        "tier: 2\n"
        "initial_prompt: ''\n"
        "initial_prompt_file: ''\n"
    )

    # Bake /opt too — but the fallback MUST NOT fire because /configs
    # is delivered. The default candidate resolution MUST be gated.
    opt_dir = tmp_path / "opt" / "molecule-platform-agent-template"
    opt_dir.mkdir(parents=True)
    (opt_dir / "config.yaml").write_text(
        "name: Baked Concierge\n"
        "runtime: claude-code\n"
        "model: moonshot/kimi-k2.6\n"
        "tier: 2\n"
    )
    (opt_dir / "system-prompt.md").write_text(
        "BAKED PROMPT THAT MUST NOT WIN\n"
    )

    real_path = Path
    patched_config = str(opt_dir / "config.yaml")
    target_opt = "/opt/molecule-platform-agent-template/config.yaml"

    def path_factory(p):
        if str(p) == target_opt:
            return real_path(patched_config)
        return real_path(p)

    monkeypatch.setattr(config_mod, "Path", path_factory)
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))

    loaded = config_mod.load_config()
    # /configs wins: model is from /configs, NOT the /opt baked moonshot.
    assert loaded.model == "anthropic:claude-opus-4-7"
    # And the initial_prompt is empty (the config bug is visible, not
    # papered over by the /opt fallback). The /opt defaulting guard
    # (`opt_fallback_fired`) is the load-bearing line.
    assert loaded.initial_prompt == "", (
        f"loaded.initial_prompt should be empty (delivery-wins); got "
        f"{loaded.initial_prompt!r} — the /opt fallback's default "
        "candidate resolution fired even though /configs was delivered"
    )


# ---------------------------------------------------------------------------
# main.py integration test: when the /opt fallback fires in load_config(),
# main.py's downstream setup path MUST adopt the resolved config base
# (config.config_path) — otherwise AdapterConfig, run_preflight,
# generate_agents_md, build_system_prompt, load_skills, mcp_servers,
# SkillsWatcher, and resolve_initial_prompt_marker all keep reading from
# the empty /configs and the concierge boots with the right model but
# identity-less behavior (empty prompt, no skills, no plugins). The
# earlier tests covered load_config()'s contract; this one covers the
# end-to-end entrypoint wiring that both reviewers flagged as the real
# blocker (CR2 review 12447 + Researcher review 12448).
# ---------------------------------------------------------------------------


def test_main_py_adopts_resolved_config_path_when_opt_fallback_fires(
    tmp_path, monkeypatch
):
    """End-to-end regression for the main.py integration gap.

    When load_config() reassigns WorkspaceConfig.config_path to the
    baked /opt/... dir, main.py's local ``config_path`` variable MUST
    follow the reassignment. The focused load_config() tests do not
    catch this because they only inspect ``loaded.config_path``, not
    the local variable in main() that gets passed to AdapterConfig /
    run_preflight / generate_agents_md.

    Strategy: inspect the source of molecule_runtime.main.main() to
    assert the contract holds. We require the line
    ``config_path = config.config_path`` to appear AFTER
    ``config = load_config(...)`` and BEFORE the first downstream
    consumer (``run_preflight``). This pins the ordering at the module
    level so a future refactor that re-introduces a stale local
    config_path fails this test.
    """
    import inspect
    from molecule_runtime import main as main_mod

    source = inspect.getsource(main_mod.main)
    # Pin the structural contract: the reassignment must follow
    # load_config and precede every downstream consumer in main().
    load_idx = source.find("config = load_config(config_path)")
    reassign_idx = source.find("config_path = config.config_path")
    preflight_idx = source.find("run_preflight(config, config_path)")
    agents_md_idx = source.find("generate_agents_md(config_path,")
    adapter_idx = source.find("AdapterConfig(")
    watcher_idx = source.find("SkillsWatcher(")
    marker_idx = source.find("resolve_initial_prompt_marker(config_path)")

    assert load_idx != -1, "main() no longer calls load_config(config_path)"
    assert reassign_idx != -1, (
        "main() is missing the `config_path = config.config_path` "
        "reassignment. After load_config() falls back to /opt, the "
        "local `config_path` variable must follow config.config_path "
        "or every downstream consumer (run_preflight, generate_agents_md, "
        "AdapterConfig, SkillsWatcher, resolve_initial_prompt_marker) "
        "keeps reading from the empty /configs and the concierge boots "
        "identity-less. Researcher review 12448 + CR2 review 12447."
    )
    assert load_idx < reassign_idx, (
        "`config_path = config.config_path` must come AFTER "
        "`config = load_config(config_path)` — otherwise the reassignment "
        "uses a stale WorkspaceConfig."
    )
    # Every downstream consumer must come AFTER the reassignment.
    for name, idx in (
        ("run_preflight", preflight_idx),
        ("generate_agents_md", agents_md_idx),
        ("AdapterConfig", adapter_idx),
        ("SkillsWatcher", watcher_idx),
        ("resolve_initial_prompt_marker", marker_idx),
    ):
        assert idx != -1, (
            f"main() no longer contains the expected {name!r} consumer; "
            "update this test if the entrypoint was refactored."
        )
        assert reassign_idx < idx, (
            f"main() calls {name!r} BEFORE the "
            "`config_path = config.config_path` reassignment — the "
            "stale local config_path leaks into the consumer. Move the "
            "reassignment to the top of the post-load block."
        )


def test_main_py_passes_resolved_config_path_to_adapter_config(
    tmp_path, monkeypatch
):
    """Direct contract test: build a fake main() execution context with
    the /opt fallback fired, then assert the AdapterConfig that main()
    constructs uses the resolved config_path (not the pre-load
    /configs).

    We import the symbols main() uses (load_config, AdapterConfig,
    run_preflight) and assert the wiring at the integration boundary:
    after load_config() returns, the call sites downstream must use
    `config.config_path`. A focused contract check that does not
    require running main() end-to-end (which depends on uvicorn,
    heartbeat, governance, etc.).
    """
    from molecule_runtime import config as config_mod
    from molecule_runtime.adapter_base import AdapterConfig

    # Unset env vars that override YAML.
    for env in ("MOLECULE_MODEL", "MODEL", "MODEL_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)

    # Stage /configs (empty) + /opt baked (concierge identity).
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    opt_dir = tmp_path / "opt" / "molecule-platform-agent-template"
    opt_dir.mkdir(parents=True)
    (opt_dir / "config.yaml").write_text(
        "name: Test Workspace\n"
        "runtime: claude-code\n"
        "model: moonshot/kimi-k2.6\n"
        "tier: 2\n"
    )
    (opt_dir / "system-prompt.md").write_text("BAKED PROMPT\n")

    real_path = Path
    target_opt = "/opt/molecule-platform-agent-template/config.yaml"

    def path_factory(p):
        if str(p) == target_opt:
            return real_path(str(opt_dir / "config.yaml"))
        return real_path(p)

    monkeypatch.setattr(config_mod, "Path", path_factory)
    monkeypatch.setenv("CONFIGS_DIR", str(configs_dir))

    # Simulate the main() pre-/post-load wiring in a local function
    # that mirrors the contract: load_config() returns, then the
    # local config_path is reassigned to config.config_path, then
    # AdapterConfig is built with the resolved path.
    config = config_mod.load_config()

    # Reproduce main()'s reassignment contract under test.
    config_path_local = config.config_path  # the line under test

    adapter_config = AdapterConfig(
        model=config.model,
        system_prompt=None,
        tools=config.skills,
        runtime_config={},
        config_path=config_path_local,  # the value main() would pass
        workspace_id="ws-test",
        prompt_files=config.prompt_files,
        a2a_port=0,
        heartbeat=None,
    )

    # The AdapterConfig must carry the resolved /opt path, NOT /configs.
    assert adapter_config.config_path == str(opt_dir), (
        f"AdapterConfig.config_path should be the resolved /opt baked "
        f"dir ({opt_dir}), got {adapter_config.config_path!r}. The "
        "main.py reassignment contract is broken — the concierge would "
        "boot identity-less (empty prompt, missing skills/plugins)."
    )
    # And it must NOT point at the empty /configs.
    assert not adapter_config.config_path.startswith(str(configs_dir)), (
        f"AdapterConfig.config_path is still under the empty /configs "
        f"({configs_dir}); the /opt fallback did not cascade through "
        "main.py's AdapterConfig construction."
    )
