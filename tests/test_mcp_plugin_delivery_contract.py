"""SSOT gate — platform_agent_identity literals MUST match the canonical
mcp-plugin-delivery contract.

This is the check that would have caught the RCA#2970 concierge-online bug:
``mcp_server_present()`` looked for a path/name that had drifted from how the
management MCP is actually delivered (plugin -> settings.json), so a healthy
concierge self-reported false and the server gate refused to mark it online.

``contracts/mcp-plugin-delivery.contract.json`` is the single source of truth,
held byte-identical here, in molecule-core, and in the claude-code template
(cross-repo drift enforced by the mcp-plugin-delivery-contract-drift workflow).
If you change a literal in platform_agent_identity.py, update the contract (and
all copies) — or this gate fails loudly, in-repo, before any image ships.
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


def test_settings_key_is_mcpservers():
    # platform_agent_identity._settings_has_management_mcp() and the executor
    # both look under contract["key"] in settings.json.
    assert CONTRACT["key"] == "mcpServers"


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
