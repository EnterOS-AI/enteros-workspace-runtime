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
    assert pai.SETTINGS_PATH == CONTRACT["settings_path"]


def test_mcp_server_name_matches_contract():
    assert pai.MANAGEMENT_MCP_NAME == CONTRACT["mcp_server_name"]


def test_legacy_binary_path_matches_contract():
    assert pai.MCPSERVER_PATH == CONTRACT["legacy_binary_path"]


def test_settings_key_matches_contract():
    # Tie the SOURCE constant (used by _settings_has_management_mcp) to the
    # contract — not a bare literal — so a source-side rename of the settings
    # map key is caught here too.
    assert pai.MCPSERVERS_KEY == CONTRACT["key"]


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
            CONTRACT["key"]: {
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
        {CONTRACT["key"]: {"other-platform": {"command": "npx"}}},
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
        {CONTRACT["key"]: [CONTRACT["mcp_server_name"]]},
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
