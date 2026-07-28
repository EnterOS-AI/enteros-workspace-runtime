"""D5: ``${config:key}`` in an MCP server's env, same seam as ``${secret:NAME}``.

An MCP server is just another subprocess a plugin declares, so an install-time
setting should reach its env exactly the way it reaches a trigger daemon's.

The property that matters most here is the one that would fail SILENTLY:
settings are keyed on the INSTALL DIRECTORY, not on ``plugin.yaml``'s ``name:``.
For the real scheduler those differ — ``molecule-ai-plugin-scheduler`` versus
``molecule-scheduler`` — and a missing settings file is a clean no-op, so keying
on the wrong one produces no error, no value, and no clue.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from molecule_runtime import plugin_settings  # noqa: E402
from molecule_runtime.plugins_registry.builtins import _resolve_mcp_config_refs  # noqa: E402

INSTALL_DIR = "molecule-ai-plugin-scheduler"
MANIFEST_NAME = "molecule-scheduler"

MANIFEST = textwrap.dedent(
    f"""
    name: {MANIFEST_NAME}
    version: 0.2.0
    description: mcp config-ref test
    kind: trigger
    contributes:
      configuration:
        properties:
          region:
            type: string
            default: us-east-1
          poll_seconds:
            type: integer
            default: 30
      mcpServers:
        - name: molecule-self
          command: npx
    """
).lstrip()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    root = configs / "plugins" / INSTALL_DIR
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(MANIFEST)
    (configs / plugin_settings.PLUGIN_SETTINGS_DIRNAME).mkdir(parents=True)
    monkeypatch.setenv("CONFIGS_DIR", str(configs))
    return {
        "root": str(root),
        "configs": configs,
        "settings": configs / plugin_settings.PLUGIN_SETTINGS_DIRNAME / f"{INSTALL_DIR}.json",
    }


def test_delivered_setting_reaches_the_mcp_env(box):
    box["settings"].write_text(json.dumps({"region": "eu-west-1"}))
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"REGION": "${config:region}", "STATIC": "unchanged"}},
        box["root"],
    )
    assert out["env"]["REGION"] == "eu-west-1"
    assert out["env"]["STATIC"] == "unchanged"


def test_declared_default_is_used_when_nothing_is_delivered(box):
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"REGION": "${config:region}"}}, box["root"]
    )
    assert out["env"]["REGION"] == "us-east-1"


def test_reference_inside_a_larger_string_is_interpolated(box):
    box["settings"].write_text(json.dumps({"region": "eu-west-1"}))
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"URL": "https://${config:region}.example.com/v1"}},
        box["root"],
    )
    assert out["env"]["URL"] == "https://eu-west-1.example.com/v1"


# --- THE SILENT-FAILURE GUARD -----------------------------------------------

def test_settings_are_keyed_on_the_install_dir_not_the_manifest_name(box):
    """A settings file under the MANIFEST name must not be picked up.

    Core never writes that filename, so honouring it would mask a real delivery
    bug — and because a missing file is a clean no-op, the failure is silent.
    """
    wrong = box["configs"] / plugin_settings.PLUGIN_SETTINGS_DIRNAME / f"{MANIFEST_NAME}.json"
    wrong.write_text(json.dumps({"region": "WRONG-KEY"}))
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"REGION": "${config:region}"}}, box["root"]
    )
    assert out["env"]["REGION"] == "us-east-1", "a manifest-name-keyed file must be ignored"

    box["settings"].write_text(json.dumps({"region": "RIGHT-KEY"}))
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"REGION": "${config:region}"}}, box["root"]
    )
    assert out["env"]["REGION"] == "RIGHT-KEY"


# --- inertness ---------------------------------------------------------------

def test_spec_without_config_refs_is_returned_untouched(box):
    spec = {"command": "npx", "env": {"A": "1", "B": "${secret:TOKEN}", "C": "${VAR}"}}
    out = _resolve_mcp_config_refs(spec, box["root"])
    assert out is spec, "no ${config:} present — must short-circuit, byte-identical"


def test_spec_without_env_is_returned_untouched(box):
    spec = {"command": "npx", "args": ["x"]}
    assert _resolve_mcp_config_refs(spec, box["root"]) is spec


def test_secret_and_var_sigils_are_left_for_their_own_resolvers(box):
    box["settings"].write_text(json.dumps({"region": "eu-west-1"}))
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {
            "REGION": "${config:region}",
            "TOKEN": "${secret:UPSTREAM_TOKEN}",
            "PASSTHRU": "${HOME}",
        }},
        box["root"],
    )
    assert out["env"]["REGION"] == "eu-west-1"
    assert out["env"]["TOKEN"] == "${secret:UPSTREAM_TOKEN}", "the secret resolver runs after us"
    assert out["env"]["PASSTHRU"] == "${HOME}", "a bare ${VAR} must pass through untouched"


def test_unresolvable_reference_renders_empty_not_the_literal(box):
    """Same as the daemon path. A sigil that behaves differently in two places
    is worse than either behaviour, and shipping the literal would have the
    server treat '${config:nope}' as a real value."""
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"X": "${config:nope}"}}, box["root"]
    )
    assert out["env"]["X"] == ""


def test_input_spec_is_not_mutated(box):
    box["settings"].write_text(json.dumps({"region": "eu-west-1"}))
    spec = {"command": "npx", "env": {"REGION": "${config:region}"}}
    out = _resolve_mcp_config_refs(spec, box["root"])
    assert spec["env"]["REGION"] == "${config:region}"
    assert out["env"]["REGION"] == "eu-west-1"


def test_broken_plugin_root_leaves_the_spec_unresolved_not_an_error(box):
    """Fail-soft: a settings problem must never block an install."""
    spec = {"command": "npx", "env": {"REGION": "${config:region}"}}
    out = _resolve_mcp_config_refs(spec, "/nonexistent/plugin/root")
    assert out["env"]["REGION"] in ("${config:region}", "")


def test_non_string_env_values_survive(box):
    box["settings"].write_text(json.dumps({"region": "eu-west-1"}))
    out = _resolve_mcp_config_refs(
        {"command": "npx", "env": {"REGION": "${config:region}", "N": 5, "B": True}},
        box["root"],
    )
    assert out["env"]["N"] == 5 and out["env"]["B"] is True
