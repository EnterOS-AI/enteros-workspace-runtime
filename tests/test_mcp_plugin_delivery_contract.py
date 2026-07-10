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
``identity_gate_payload()`` report correctly. The CROSS-REPO guarantee that the
core/template/runtime copies stay byte-identical is enforced separately by the
``mcp-plugin-delivery-contract-drift`` workflow in molecule-core; wiring this
repo's copy into that byte-compare set is a tracked follow-up.
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


def test_management_mcp_prebake_constants_match_contract():
    # Guard D lockstep: the runtime's pre-bake constants (consumed by
    # scripts/prebake-mgmt-mcp.sh) MUST equal the contract's SSOT
    # management_mcp_server block — so the baked npm version can never silently
    # drift from the pin the plugin fragment launches (#54, launch-side RCA #2970).
    mms = CONTRACT["management_mcp_server"]
    assert pai.MANAGEMENT_MCP_NPM_PACKAGE == mms["npm_package"]
    assert pai.MANAGEMENT_MCP_PINNED_VERSION == mms["pinned_version"]
    assert pai.MANAGEMENT_MCP_REGISTRY == mms["registry"]
    assert pai.MANAGEMENT_MCP_REGISTRY_SCOPE == mms["registry_scope"]


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
# ADR-004 — the plugin→runtime MCP shape lives in the ADAPTER, not the engine.
# ===========================================================================
# The bug this whole line of work fixes (#3159): a codex/hermes concierge had the
# management MCP written to .claude/settings.json — a file its runtime never reads
# — so it booted WITHOUT create_workspace and the RCA#2970 gate fail-closed it
# offline. ADR-004 relocated the per-runtime render/read/present OUT of the shared
# engine (the deleted mcp_render.render_codex_config / render_hermes_config /
# render_claude_settings / _RUNTIME_SPECS dispatch) and INTO each adapter's
# template repo. So the #3159 regression net now lives in TWO places:
#
#   * PER-ADAPTER (native format): each template's SDK conformance suite
#     (``molecule_plugin.adapter_conformance`` — 16 checks) proves that adapter's
#     OWN register→read→present round-trips on ITS native file (codex → config.toml,
#     hermes → config.yaml, openclaw → openclaw.json) and NEVER false-greens against
#     claude's settings.json (the ``test_unmapped_runtime_*`` fail-closed checks +
#     ``test_native_mcp_path_is_runtime_specific``). Run in each template repo.
#
#   * BASE DEFAULT (this file): the BaseAdapter fallback — the ONLY thing that
#     survives in this engine repo — is now name-AGNOSTIC. A name-only adapter (one
#     that overrides NOTHING but name()) gets the generic JSON ``mcpServers`` default
#     at ``<config_path>/.claude/settings.json``. It does NOT dispatch codex→toml /
#     hermes→yaml by name any more (that per-runtime shape moved into the adapter).
#     The tests below pin that ADR-004 base-default contract + the delivery-contract
#     SSOT literals.

import pytest  # noqa: E402

from molecule_runtime import mcp_render  # noqa: E402

_PLATFORM_SPEC = {
    "command": "npx",
    "args": ["-y", "@molecule-ai/mcp-server"],
    "env": {"MOLECULE_MCP_MODE": "management", "MOLECULE_API_URL": "${MOLECULE_API_URL}"},
}


def test_runtime_matrix_contract_present():
    """The delivery contract enumerates a per-runtime map; platform runtimes must
    be concretely implemented (not stubs). This is the SSOT the SDK official
    registry mirrors — unchanged by ADR-004 (the CONTRACT still declares the
    per-runtime native path/format; the IMPLEMENTATION of it moved into the
    adapters)."""
    runtimes = CONTRACT["runtimes"]
    assert runtimes["claude_code"]["status"] == "implemented"
    assert runtimes["codex"]["status"] == "implemented"
    assert runtimes["openclaw"]["status"] == "implemented"
    assert runtimes["hermes"]["status"] == "implemented"


def test_base_default_json_render_writes_mcpservers(tmp_path):
    """The generic JSON ``mcpServers`` renderer the BaseAdapter default uses writes
    the descriptor byte-stably (ADR-004 kept this as the ONE name-free renderer; the
    per-runtime renderers moved into the adapters). Byte-shape: json.dumps(indent=2)
    + trailing newline — the golden-parity invariant, unchanged from the deleted
    claude renderer."""
    settings = tmp_path / ".claude" / "settings.json"
    mcp_render.render_json_mcp_servers(settings, "molecule-platform", _PLATFORM_SPEC)
    assert settings.is_file()
    data = json.loads(settings.read_text())
    assert data["mcpServers"]["molecule-platform"] == _PLATFORM_SPEC
    # byte-shape pinned (the migration must not churn native output)
    assert settings.read_text() == json.dumps(
        {"mcpServers": {"molecule-platform": _PLATFORM_SPEC}}, indent=2
    ) + "\n"


# ---------------------------------------------------------------------------
# PORT default matrix (ADR-004): the BaseAdapter DEFAULT hook is name-AGNOSTIC.
# A name-only adapter (overrides NOTHING but name()) writes the generic JSON
# ``mcpServers`` default — it NO LONGER dispatches codex→toml / hermes→yaml by
# name (that per-runtime shape moved into the adapter's own override, proven by
# the template's SDK conformance suite). The real codex/hermes/openclaw adapters
# get their native format because they OVERRIDE the seam, not via base dispatch.
# ---------------------------------------------------------------------------

from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter  # noqa: E402


class _BaseTestAdapter(BaseAdapter):
    """A no-override adapter: only name() differs per subclass. It deliberately
    does NOT override mcp_settings_path / register_mcp_server_hook /
    management_mcp_present — so it exercises the ADR-004 name-agnostic BASE
    DEFAULT (generic JSON ``mcpServers``), NOT any per-runtime native format."""

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


class _ThirdPartyNameAdapter(_BaseTestAdapter):
    @staticmethod
    def name():
        return "some-third-party-runtime"


def test_base_default_writes_generic_json_and_present_probes_it(tmp_path):
    """The name-agnostic base default: render → present round-trips on the generic
    JSON ``mcpServers`` config at ``<config_path>/.claude/settings.json`` for ANY
    adapter that doesn't override the seam (here a claude-named one). This is the
    ADR-004 replacement for the deleted engine lockstep."""
    cfg = AdapterConfig(model="anthropic:claude-sonnet-4-6", config_path=str(tmp_path))
    adapter = _ClaudeOnlyNameAdapter()
    assert adapter.management_mcp_present(cfg) is False
    adapter.register_mcp_server_hook(cfg, "molecule-platform", _PLATFORM_SPEC)
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert adapter.management_mcp_present(cfg) is True


def test_base_default_is_name_agnostic_codex_name_gets_generic_json(tmp_path, monkeypatch):
    """ADR-004 CONTRACT INVERSION (was: 'default hook dispatches CODEX for codex').
    A codex-NAMED adapter that overrides ONLY name() now gets the GENERIC JSON
    default (``<config_path>/.claude/settings.json``), NOT ~/.codex/config.toml —
    because the base no longer dispatches by runtime name. The REAL codex adapter
    writes config.toml because it OVERRIDES register_mcp_server_hook (proven by the
    codex template's SDK conformance suite), not via the base."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    configs = tmp_path / "configs"
    cfg = AdapterConfig(model="openai:gpt-5.5", config_path=str(configs))
    adapter = _CodexOnlyNameAdapter()
    assert adapter.management_mcp_present(cfg) is False

    adapter.register_mcp_server_hook(cfg, "molecule-platform", _PLATFORM_SPEC)

    # ADR-004: the base default wrote the GENERIC JSON config, not codex's toml.
    assert (configs / ".claude" / "settings.json").is_file(), (
        "the name-agnostic base default writes the generic JSON mcpServers config"
    )
    assert not (home / ".codex" / "config.toml").exists(), (
        "the base default is name-agnostic — it does NOT render codex's config.toml "
        "by name (that per-runtime shape moved into the codex adapter's override)"
    )
    # The base present-probe reads that same generic JSON config (render/present
    # lockstep on the base default's own file).
    assert adapter.management_mcp_present(cfg) is True


def test_base_default_third_party_runtime_gets_generic_json(tmp_path):
    """A genuinely third-party runtime name (no override) also gets the generic
    JSON default — ADR-004's 'bring your own adapter' seam: a third party gets a
    sane fail-closed default from the base, then overrides the seam to render its
    own native format."""
    configs = tmp_path / "configs"
    cfg = AdapterConfig(model="x:y", config_path=str(configs))
    adapter = _ThirdPartyNameAdapter()
    adapter.register_mcp_server_hook(cfg, "molecule-platform", _PLATFORM_SPEC)
    assert (configs / ".claude" / "settings.json").is_file()
    assert adapter.management_mcp_present(cfg) is True


@pytest.mark.asyncio
async def test_install_plugins_via_registry_base_default_writes_generic_json(tmp_path, monkeypatch):
    """END-TO-END through the REAL production path
    (BaseAdapter.install_plugins_via_registry) for a name-only adapter: the
    management MCP lands in the generic JSON ``mcpServers`` config (the ADR-004
    base default). The REAL codex/hermes production paths write their native
    formats via their OWN adapter overrides — exercised in the template repos'
    conformance/e2e jobs, not here (the engine repo has no per-runtime renderer to
    test)."""
    from molecule_runtime.plugins import LoadedPlugins, Plugin

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

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
    cfg = AdapterConfig(model="x:y", config_path=str(configs))
    adapter = _ThirdPartyNameAdapter()  # overrides ONLY name()

    plugins = LoadedPlugins(plugins=[Plugin(name="molecule-platform-mcp", path=str(plugin_root))])
    results = await adapter.install_plugins_via_registry(cfg, plugins)
    assert results, "install must have run the MCPServerAdaptor"

    # The base default wired the management MCP into the generic JSON config.
    assert (configs / ".claude" / "settings.json").is_file(), (
        "production path (base default) must write the generic JSON mcpServers config"
    )
    parsed = json.loads((configs / ".claude" / "settings.json").read_text())
    assert parsed["mcpServers"]["molecule-platform"]["command"] == "npx"
    # No native codex/hermes file appears — the base default is name-agnostic.
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".hermes" / "config.yaml").exists()
    # The runtime-agnostic gate probe sees the management MCP.
    assert adapter.management_mcp_present(cfg) is True


@pytest.mark.asyncio
async def test_install_plugins_via_registry_claude_writes_claude_settings(tmp_path, monkeypatch):
    """The claude runtime end-to-end: the production path writes
    .claude/settings.json (claude-code's native format IS the generic JSON default,
    so this holds for both the base default and claude's own adapter)."""
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
