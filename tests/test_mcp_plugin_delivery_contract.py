"""SSOT gate — platform_agent_identity literals and delivery path MUST match
the canonical mcp-plugin-delivery contract.

This is the check that would have caught the RCA#2970 concierge-online bug:
``mcp_server_present()`` looked for a path/name that had drifted from how the
management MCP is actually delivered (plugin -> settings.json), so a healthy
concierge self-reported false and the server gate refused to mark it online.

Scope (honest): this is the RUNTIME-LOCAL gate. It pins this repo's literals to
this repo's vendored copy of ``contracts/mcp-plugin-delivery.contract.json`` —
so an in-repo edit that changes a literal without the contract (or vice versa)
fails ``unit-tests`` before any image ships. It also exercises the real delivery
path by materializing a delivered ``settings.json`` and asserting that
``_settings_has_management_mcp()``, ``mcp_server_present()``, and
``identity_gate_payload()`` report correctly. The BYTE-IDENTICAL guarantee — that
this repo's vendored copy stays equal to the SDK SSOT
(``molecule-ai-sdk:contracts/mcp/mcp-plugin-delivery.contract.json``) — is now
enforced by this repo's own ``schema-sync`` workflow
(``scripts/check-schemas-in-sync.sh`` fetches SDK main and diffs), alongside the
core/template copies guarded by ``mcp-plugin-delivery-contract-drift`` in
molecule-core.
"""

import json
from pathlib import Path
from unittest.mock import patch

from molecule_runtime import platform_agent_identity as pai

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "mcp-plugin-delivery.contract.json"
    ).read_text()
)


def test_settings_path_matches_contract():
    # The claude_code runtime is the canonical delivery surface for the
    # platform-agent-identity settings.json gate.
    assert pai.SETTINGS_PATH == CONTRACT["runtimes"]["claude_code"]["settings_path"]


def test_mcp_server_name_matches_contract():
    assert pai.MANAGEMENT_MCP_NAME == CONTRACT["mcp_server_name"]


def test_legacy_binary_path_matches_contract():
    assert pai.MCPSERVER_PATH == CONTRACT["legacy_binary_path"]


def test_required_tool_matches_contract():
    # The management lifecycle verb the readiness probe enumerates and the core
    # gate (conciergePlatformMCPRequiredTool) matches MUST be the same literal.
    assert pai.REQUIRED_TOOL == CONTRACT["required_tool"]


def test_provision_tool_id_derives_from_contract_literals():
    # The fully-qualified id the heartbeat's loaded_mcp_tools must carry is
    # composed from the two contract-pinned building blocks via the canonical
    # mcp__<server>__<tool> formula — byte-identical to the core gate's
    # conciergePlatformMCPProvisionWorkspaceTool.
    expected = "mcp__{}__{}".format(CONTRACT["mcp_server_name"], CONTRACT["required_tool"])
    assert pai.MANAGEMENT_PROVISION_TOOL_ID == expected
    assert pai.MANAGEMENT_PROVISION_TOOL_ID == "mcp__molecule-platform__provision_workspace"


def test_loaded_mcp_tools_field_name_matches_contract():
    # The readiness signal is published under exactly the wire field the
    # server-side gate reads (payload.loaded_mcp_tools).
    pai.set_loaded_mcp_tools(["x"])
    try:
        payload = pai.identity_gate_payload()
    finally:
        pai.set_loaded_mcp_tools(None)
    assert CONTRACT["loaded_mcp_tools_field"] in payload


def test_readiness_probe_listed_as_consumer():
    # The active tools/list probe is the fourth party bound by the contract;
    # make that explicit so a future reader sees it.
    assert any("mcp_readiness_probe" in c for c in CONTRACT.get("consumers", []))


def test_settings_key_matches_contract():
    # Tie the SOURCE constant (used by _settings_has_management_mcp) to the
    # contract — not a bare literal — so a source-side rename of the settings
    # map key is caught here too.
    assert pai.MCPSERVERS_KEY == CONTRACT["runtimes"]["claude_code"]["key"]


def test_present_field_name_matches_contract():
    # identity_gate_payload() must emit exactly the field the server-side
    # RCA#2970 gate reads (payload.mcp_server_present).
    with patch.object(pai, "mcp_server_present", lambda: True):
        payload = pai.identity_gate_payload()
    assert CONTRACT["runtime_present_field"] in payload


def test_this_module_listed_as_consumer():
    # Make the contract's consumer list authoritative: this runtime check must
    # be declared so a future reader knows all parties bound by the contract.
    assert any(
        "platform_agent_identity" in c for c in CONTRACT.get("consumers", [])
    )


# ---------------------------------------------------------------------------
# Behavioral delivery-path gate
# ---------------------------------------------------------------------------
# The constant checks above are necessary but not sufficient. The Researcher
# RC (#12704) pointed out that a coordinated wrong-contract + source edit could
# still pass if we never exercise the real consumer functions against a
# delivered settings.json. The tests below materialize the actual
# plugin-delivered shape and assert _settings_has_management_mcp(),
# mcp_server_present(), and identity_gate_payload() behave correctly.


def _write_settings(tmp_path, contents):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(contents))
    return settings


def _patch_paths(tmp_path, monkeypatch, settings, binary_exists=False):
    if binary_exists:
        binary = tmp_path / "molecule-mcp-server"
        binary.write_text("#!/bin/sh\necho ok")
        monkeypatch.setattr(pai, "MCPSERVER_PATH", str(binary))
    else:
        monkeypatch.setattr(
            pai, "MCPSERVER_PATH", str(tmp_path / "no-binary")
        )
    monkeypatch.setattr(pai, "SETTINGS_PATH", str(settings))


def test_delivered_settings_makes_management_mcp_present(
    tmp_path, monkeypatch
):
    """A real plugin-delivered settings.json (contract key -> contract name,
    with command/args/env shape) is recognized by the consumer path."""
    settings = _write_settings(
        tmp_path,
        {
            CONTRACT["runtimes"]["claude_code"]["key"]: {
                CONTRACT["mcp_server_name"]: {
                    "command": "npx",
                    "args": ["-y", "@molecule/mcp-server"],
                    "env": {"MOLECULE_ORG_KEY": "secret"},
                }
            }
        },
    )
    _patch_paths(tmp_path, monkeypatch, settings, binary_exists=False)

    assert pai._settings_has_management_mcp() is True
    assert pai.mcp_server_present() is True
    payload = pai.identity_gate_payload()
    assert payload[CONTRACT["runtime_present_field"]] is True


def test_missing_settings_file_stays_fail_closed(tmp_path, monkeypatch):
    _patch_paths(
        tmp_path,
        monkeypatch,
        tmp_path / "no-settings.json",
        binary_exists=False,
    )
    assert pai._settings_has_management_mcp() is False
    assert pai.mcp_server_present() is False
    payload = pai.identity_gate_payload()
    assert payload[CONTRACT["runtime_present_field"]] is False


def test_wrong_settings_key_stays_fail_closed(tmp_path, monkeypatch):
    settings = _write_settings(
        tmp_path,
        {"otherServers": {CONTRACT["mcp_server_name"]: {"command": "npx"}}},
    )
    _patch_paths(tmp_path, monkeypatch, settings)
    assert pai._settings_has_management_mcp() is False
    assert pai.mcp_server_present() is False


def test_wrong_mcp_name_stays_fail_closed(tmp_path, monkeypatch):
    settings = _write_settings(
        tmp_path,
        {CONTRACT["runtimes"]["claude_code"]["key"]: {"other-platform": {"command": "npx"}}},
    )
    _patch_paths(tmp_path, monkeypatch, settings)
    assert pai._settings_has_management_mcp() is False
    assert pai.mcp_server_present() is False


def test_malformed_settings_stays_fail_closed(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json")
    _patch_paths(tmp_path, monkeypatch, settings)
    assert pai._settings_has_management_mcp() is False
    assert pai.mcp_server_present() is False


def test_top_level_not_dict_stays_fail_closed(tmp_path, monkeypatch):
    settings = _write_settings(tmp_path, [CONTRACT["mcp_server_name"]])
    _patch_paths(tmp_path, monkeypatch, settings)
    assert pai._settings_has_management_mcp() is False
    assert pai.mcp_server_present() is False


def test_mcpservers_not_dict_stays_fail_closed(tmp_path, monkeypatch):
    settings = _write_settings(
        tmp_path,
        {CONTRACT["runtimes"]["claude_code"]["key"]: [CONTRACT["mcp_server_name"]]},
    )
    _patch_paths(tmp_path, monkeypatch, settings)
    assert pai._settings_has_management_mcp() is False
    assert pai.mcp_server_present() is False


def test_legacy_binary_alone_satisfies_contract_gate(
    tmp_path, monkeypatch
):
    """The contract still allows the legacy baked binary path to satisfy the
    gate; this test verifies that path end-to-end using the contract field."""
    settings = tmp_path / "no-settings.json"
    _patch_paths(tmp_path, monkeypatch, settings, binary_exists=True)
    assert pai.mcp_server_present() is True
    payload = pai.identity_gate_payload()
    assert payload[CONTRACT["runtime_present_field"]] is True


# ===========================================================================
# RFC §5b Addition 1 — per-runtime MCP render matrix.
# ===========================================================================
# The bug this whole change fixes (#3159): a codex/hermes concierge had the
# management MCP written to .claude/settings.json — a file its runtime never
# reads — so it booted WITHOUT create_workspace and the RCA#2970 gate fail-
# closed it offline. These tests are the regression net: for every runtime,
# assert the renderer writes the RIGHT native config, and ASSERT NEGATIVELY
# that codex does NOT write .claude/settings.json. A renderer regression that
# re-introduces the "always write claude settings.json" behavior fails here
# before any image ships.

import pytest  # noqa: E402
import tomllib  # noqa: E402

from molecule_runtime import mcp_render  # noqa: E402

_PLATFORM_SPEC = {
    "command": "npx",
    "args": ["-y", "@molecule-ai/mcp-server"],
    "env": {"MOLECULE_MCP_MODE": "management", "MOLECULE_API_URL": "${MOLECULE_API_URL}"},
}


def test_runtime_matrix_contract_present():
    """The contract enumerates a per-runtime map; claude_code + codex must be
    concretely implemented (not stubs)."""
    runtimes = CONTRACT["runtimes"]
    assert runtimes["claude_code"]["status"] == "implemented"
    assert runtimes["codex"]["status"] == "implemented"
    # hermes graduated to a concrete renderer (runtime#181): its native
    # ~/.hermes/config.yaml `mcp_servers` surface is pinned.
    assert runtimes["hermes"]["status"] == "implemented"


def test_claude_render_writes_settings_json(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    mcp_render.render_claude_settings(settings, "molecule-platform", _PLATFORM_SPEC)
    assert settings.is_file()
    data = json.loads(settings.read_text())
    assert data["mcpServers"]["molecule-platform"] == _PLATFORM_SPEC


def test_codex_render_writes_config_toml_AND_NOT_claude_settings(tmp_path):
    """THE test that would have caught #3159: codex → config.toml, and the
    Claude settings.json is NOT touched."""
    config_toml = tmp_path / ".codex" / "config.toml"
    claude_settings = tmp_path / ".claude" / "settings.json"

    mcp_render.render_codex_config(config_toml, "molecule-platform", _PLATFORM_SPEC)

    # Wrote the codex-native file...
    assert config_toml.is_file(), "codex render must write ~/.codex/config.toml"
    parsed = tomllib.loads(config_toml.read_text())
    entry = parsed["mcp_servers"]["molecule-platform"]
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@molecule-ai/mcp-server"]
    assert entry["env"]["MOLECULE_MCP_MODE"] == "management"

    # ...and crucially did NOT write the Claude settings.json. This negative
    # assertion is the #3159 regression guard.
    assert not claude_settings.exists(), (
        "codex render must NOT write .claude/settings.json (the #3159 bug)"
    )


def test_codex_render_idempotent_and_additive(tmp_path):
    """Re-rendering the same server is idempotent; a second server is additive."""
    config_toml = tmp_path / "config.toml"
    mcp_render.render_codex_config(config_toml, "molecule-platform", _PLATFORM_SPEC)
    first = config_toml.read_text()
    # Re-install same server → byte-stable (no duplicate table).
    mcp_render.render_codex_config(config_toml, "molecule-platform", _PLATFORM_SPEC)
    assert config_toml.read_text() == first
    parsed = tomllib.loads(config_toml.read_text())
    assert list(parsed["mcp_servers"].keys()) == ["molecule-platform"]

    # Add a second server → both present, original untouched.
    mcp_render.render_codex_config(
        config_toml, "other-mcp", {"command": "uvx", "args": ["other"]}
    )
    parsed = tomllib.loads(config_toml.read_text())
    assert set(parsed["mcp_servers"].keys()) == {"molecule-platform", "other-mcp"}
    assert parsed["mcp_servers"]["molecule-platform"]["command"] == "npx"


def test_codex_render_preserves_handwritten_config(tmp_path):
    """A hand-written top-of-file setting outside our markers survives a render."""
    config_toml = tmp_path / "config.toml"
    config_toml.write_text('model = "gpt-5.5"\n')
    mcp_render.render_codex_config(config_toml, "molecule-platform", _PLATFORM_SPEC)
    parsed = tomllib.loads(config_toml.read_text())
    assert parsed["model"] == "gpt-5.5"
    assert "molecule-platform" in parsed["mcp_servers"]


@pytest.mark.parametrize("renderer_name", ["render_gemini_config"])
def test_unverified_runtimes_are_explicit_stubs(tmp_path, renderer_name):
    """The remaining unverified renderers (gemini/google-adk) are honest
    NotImplementedError stubs (native MCP convention unpinned) — NOT a silent
    wrong write. Implement concretely once the format is pinned against a live
    CLI. hermes graduated OUT (runtime#181) and is covered by
    test_hermes_render_writes_native_config below."""
    renderer = getattr(mcp_render, renderer_name)
    with pytest.raises(NotImplementedError):
        renderer(tmp_path / "out", "molecule-platform", _PLATFORM_SPEC)


def test_hermes_render_writes_native_config(tmp_path, monkeypatch):
    """hermes renderer writes ~/.hermes/config.yaml `mcp_servers.<name>` (and NOT
    .claude/settings.json) — the #3159 cross-runtime-mis-attribution guard for the
    hermes surface. Additive: an existing server + the `model` block survive."""
    import yaml

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    cfg = home / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        yaml.safe_dump(
            {"model": "nous:x", "mcp_servers": {"keep": {"url": "http://127.0.0.1:9100/mcp"}}},
            sort_keys=False,
        )
    )

    path = mcp_render.render_for_runtime(
        "hermes", str(tmp_path / "configs"), "molecule-platform", _PLATFORM_SPEC
    )
    assert str(path).endswith("/.hermes/config.yaml")

    parsed = yaml.safe_load(cfg.read_text())
    assert parsed["model"] == "nous:x"                                   # preserved
    assert parsed["mcp_servers"]["keep"] == {"url": "http://127.0.0.1:9100/mcp"}
    entry = parsed["mcp_servers"]["molecule-platform"]
    assert entry["command"] == "npx"
    assert entry["env"]["MOLECULE_MCP_MODE"] == "management"
    # and crucially did NOT write the Claude settings.json (the #3159 bug)
    assert not (tmp_path / "configs" / ".claude" / "settings.json").exists()
    # present-reader agrees with what the renderer just wrote
    assert mcp_render.management_mcp_present_for(
        "hermes", str(tmp_path / "configs"), "molecule-platform"
    ) is True


# ---------------------------------------------------------------------------
# PORT default-dispatch matrix: the BaseAdapter DEFAULT hook renders per-runtime
# by dispatching on self.name() — a codex adapter that overrides NOTHING but
# name() must still get codex rendering. This is the production-path guarantee
# the reviewers (CR2 #13467 / Researcher #13468) flagged as the gap: without it
# a real codex concierge writes the MCP to .claude/settings.json (the #3159 bug).
# ---------------------------------------------------------------------------

from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter  # noqa: E402


class _BaseTestAdapter(BaseAdapter):
    """A no-override adapter: only name() differs per subclass. It deliberately
    does NOT override mcp_settings_path / register_mcp_server_hook /
    management_mcp_present — proving the DEFAULT dispatch closes the loop."""

    @staticmethod
    def display_name():
        return "test"

    @staticmethod
    def description():
        return "test"

    async def setup(self, config):
        return None

    async def create_executor(self, config):
        return None


class _ClaudeOnlyNameAdapter(_BaseTestAdapter):
    @staticmethod
    def name():
        return "claude-code"


class _CodexOnlyNameAdapter(_BaseTestAdapter):
    @staticmethod
    def name():
        return "codex"


def test_default_hook_dispatches_claude_for_claude_runtime(tmp_path):
    cfg = AdapterConfig(model="anthropic:claude-sonnet-4-6", config_path=str(tmp_path))
    adapter = _ClaudeOnlyNameAdapter()
    assert adapter.management_mcp_present(cfg) is False
    adapter.register_mcp_server_hook(cfg, "molecule-platform", _PLATFORM_SPEC)
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert adapter.management_mcp_present(cfg) is True


def test_default_hook_dispatches_CODEX_for_codex_runtime(tmp_path, monkeypatch):
    """THE production-path regression guard: a codex adapter that overrides only
    name() gets codex config.toml from the DEFAULT hook — and NOT
    .claude/settings.json. Codex resolves ~/.codex/config.toml, so we redirect
    HOME into tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    cfg = AdapterConfig(model="openai:gpt-5.5", config_path=str(tmp_path / "configs"))
    adapter = _CodexOnlyNameAdapter()
    assert adapter.management_mcp_present(cfg) is False

    adapter.register_mcp_server_hook(cfg, "molecule-platform", _PLATFORM_SPEC)

    codex_cfg = home / ".codex" / "config.toml"
    assert codex_cfg.is_file(), "default hook must render codex config.toml for runtime=codex"
    parsed = tomllib.loads(codex_cfg.read_text())
    assert "molecule-platform" in parsed["mcp_servers"]
    # Negative: claude settings.json NOT written by a codex run (#3159).
    assert not (tmp_path / "configs" / ".claude" / "settings.json").exists()
    # The gate probe also reads codex's config.toml, not claude settings.json.
    assert adapter.management_mcp_present(cfg) is True


@pytest.mark.asyncio
async def test_install_plugins_via_registry_codex_writes_config_toml_not_claude(tmp_path, monkeypatch):
    """END-TO-END through the REAL production path
    (BaseAdapter.install_plugins_via_registry): a codex-configured run installing
    the molecule-platform-mcp plugin lands the management MCP in
    ~/.codex/config.toml and NOT in .claude/settings.json. This is the exact
    #3159 scenario, exercised without any synthetic adapter override."""
    from molecule_runtime.plugins import LoadedPlugins, Plugin

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # A real molecule-platform-mcp plugin dir shipping the mcpServers descriptor.
    plugin_root = tmp_path / "molecule-platform-mcp"
    plugin_root.mkdir()
    (plugin_root / "settings-fragment.json").write_text(json.dumps({
        "mcpServers": {"molecule-platform": {
            "command": "npx", "args": ["-y", "@molecule-ai/mcp-server"],
            "env": {"MOLECULE_MCP_MODE": "management"},
        }}
    }))

    configs = tmp_path / "configs"
    configs.mkdir()
    cfg = AdapterConfig(model="openai:gpt-5.5", config_path=str(configs))
    adapter = _CodexOnlyNameAdapter()  # overrides ONLY name()

    plugins = LoadedPlugins(plugins=[Plugin(name="molecule-platform-mcp", path=str(plugin_root))])
    results = await adapter.install_plugins_via_registry(cfg, plugins)
    assert results, "install must have run the MCPServerAdaptor"

    codex_cfg = home / ".codex" / "config.toml"
    assert codex_cfg.is_file(), "production path must write codex config.toml for runtime=codex"
    parsed = tomllib.loads(codex_cfg.read_text())
    assert parsed["mcp_servers"]["molecule-platform"]["command"] == "npx"
    assert not (configs / ".claude" / "settings.json").exists(), \
        "production codex path must NOT write .claude/settings.json (#3159)"
    # The runtime-agnostic gate probe sees the management MCP for codex.
    assert adapter.management_mcp_present(cfg) is True


@pytest.mark.asyncio
async def test_install_plugins_via_registry_claude_writes_claude_settings(tmp_path, monkeypatch):
    """Symmetric end-to-end for the claude runtime: the same production path
    writes .claude/settings.json (and the codex config.toml stays absent)."""
    from molecule_runtime.plugins import LoadedPlugins, Plugin

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    plugin_root = tmp_path / "molecule-platform-mcp"
    plugin_root.mkdir()
    (plugin_root / "settings-fragment.json").write_text(json.dumps({
        "mcpServers": {"molecule-platform": {"command": "npx", "args": ["-y", "x"]}}
    }))

    configs = tmp_path / "configs"
    configs.mkdir()
    cfg = AdapterConfig(model="anthropic:claude-sonnet-4-6", config_path=str(configs))
    adapter = _ClaudeOnlyNameAdapter()

    plugins = LoadedPlugins(plugins=[Plugin(name="molecule-platform-mcp", path=str(plugin_root))])
    await adapter.install_plugins_via_registry(cfg, plugins)

    assert (configs / ".claude" / "settings.json").is_file()
    assert not (home / ".codex" / "config.toml").exists()
    assert adapter.management_mcp_present(cfg) is True


@pytest.mark.asyncio
async def test_install_plugins_via_registry_unverified_fails_loud_for_privileged(tmp_path, monkeypatch):
    """A still-UNVERIFIED runtime (google-adk) installing the PRIVILEGED
    management MCP fails LOUDLY through the production path rather than booting a
    capability-less concierge — the deliberate fail-loud stub behavior. (hermes
    graduated OUT — runtime#181 — so it renders concretely; see the next test.)"""
    from molecule_runtime.plugins import LoadedPlugins, Plugin
    from molecule_runtime.adapter_base import PrivilegedPluginInstallError

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    plugin_root = tmp_path / "molecule-platform-mcp"
    plugin_root.mkdir()
    (plugin_root / "settings-fragment.json").write_text(json.dumps({
        "mcpServers": {"molecule-platform": {"command": "npx"}}
    }))

    cfg = AdapterConfig(model="x:y", config_path=str(tmp_path / "configs"))

    class _GoogleAdkAdapter(_BaseTestAdapter):
        @staticmethod
        def name():
            return "google_adk"

    plugins = LoadedPlugins(plugins=[Plugin(name="molecule-platform-mcp", path=str(plugin_root))])
    with pytest.raises(PrivilegedPluginInstallError):
        await _GoogleAdkAdapter().install_plugins_via_registry(cfg, plugins)


@pytest.mark.asyncio
async def test_install_plugins_via_registry_hermes_renders_into_native_config(tmp_path, monkeypatch):
    """A hermes concierge installing the PRIVILEGED management MCP through the
    production path renders it into ~/.hermes/config.yaml `mcp_servers` (NOT a
    fail-loud stub, NOT .claude/settings.json) — the runtime#181 graduation."""
    import yaml
    from molecule_runtime.plugins import LoadedPlugins, Plugin

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))

    plugin_root = tmp_path / "molecule-platform-mcp"
    plugin_root.mkdir()
    (plugin_root / "settings-fragment.json").write_text(json.dumps({
        "mcpServers": {"molecule-platform": {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]}}
    }))

    cfg = AdapterConfig(model="x:y", config_path=str(tmp_path / "configs"))

    class _HermesAdapter(_BaseTestAdapter):
        @staticmethod
        def name():
            return "hermes"

    adapter = _HermesAdapter()
    plugins = LoadedPlugins(plugins=[Plugin(name="molecule-platform-mcp", path=str(plugin_root))])
    await adapter.install_plugins_via_registry(cfg, plugins)

    parsed = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    assert parsed["mcp_servers"]["molecule-platform"]["command"] == "npx"
    assert not (tmp_path / "configs" / ".claude" / "settings.json").exists()
    assert adapter.management_mcp_present(cfg) is True
