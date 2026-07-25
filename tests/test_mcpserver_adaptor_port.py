"""MCPServerAdaptor must route mcpServers through the runtime-agnostic PORT.

Regression net for #3159: the adaptor used to write .claude/settings.json
directly via _install_claude_layer regardless of runtime. It now parses the
mcpServers descriptor and calls ctx.register_mcp_server(name, spec) per entry,
so the active adapter renders the MCP into the file ITS runtime reads.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from molecule_runtime.plugins_registry.builtins import (
    MCPServerAdaptor,
    _PRIVILEGED_MCP_PLUGIN,
    _read_mcp_descriptor,
    _resolve_mcp_secret_refs,
)
from molecule_runtime.plugins_registry.protocol import InstallContext
from molecule_runtime.adapter_base import PrivilegedPluginInstallError


def _make_plugin(tmp_path: Path, fragment: dict, filename="settings-fragment.json") -> Path:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / filename).write_text(json.dumps(fragment))
    return root


def _ctx(plugin_root, configs_dir, recorder):
    return InstallContext(
        configs_dir=configs_dir,
        workspace_id="ws-test",
        runtime="claude_code",
        plugin_root=plugin_root,
        register_mcp_server=recorder,
        logger=logging.getLogger("t"),
    )


def test_read_descriptor_from_settings_fragment(tmp_path):
    root = _make_plugin(
        tmp_path,
        {"mcpServers": {"molecule-platform": {"command": "npx", "args": ["-y", "x"]}}},
    )
    desc = _read_mcp_descriptor(root)
    assert desc == {"molecule-platform": {"command": "npx", "args": ["-y", "x"]}}


def test_read_descriptor_prefers_mcp_servers_json(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "settings-fragment.json").write_text(json.dumps({"mcpServers": {"old": {"command": "a"}}}))
    (root / "mcp-servers.json").write_text(json.dumps({"new": {"command": "b"}}))
    assert _read_mcp_descriptor(root) == {"new": {"command": "b"}}


def test_read_descriptor_from_canonical_plugin_manifest(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.yaml").write_text(
        "name: demo-mcp\n"
        "version: 0.1.0\n"
        "description: demo\n"
        "contributes:\n"
        "  mcpServers:\n"
        "    - name: demo-mcp\n"
        "      command: python\n"
        "      args: [server.py]\n"
    )

    assert _read_mcp_descriptor(root) == {
        "demo-mcp": {"command": "python", "args": ["server.py"]}
    }


def test_read_descriptor_does_not_follow_manifest_symlink(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        "contributes:\n"
        "  mcpServers:\n"
        "    - name: escaped\n"
        "      command: outside\n"
    )
    (root / "plugin.yaml").symlink_to(outside)

    assert _read_mcp_descriptor(root) == {}


@pytest.mark.asyncio
async def test_install_routes_each_server_through_port(tmp_path):
    """install() calls ctx.register_mcp_server per descriptor entry — it does
    NOT write .claude/settings.json directly."""
    root = _make_plugin(
        tmp_path,
        {"mcpServers": {
            "molecule-platform": {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]},
        }},
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    calls = []
    ctx = _ctx(root, configs, lambda n, s: calls.append((n, s)))

    await MCPServerAdaptor("molecule-platform-mcp", "claude_code").install(ctx)

    assert calls == [
        ("molecule-platform", {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]}),
    ]
    # The adaptor itself wrote no claude settings.json — the PORT (which we
    # stubbed) owns that. This is the #3159 separation.
    assert not (configs / ".claude" / "settings.json").exists()


@pytest.mark.asyncio
async def test_generated_mcp_server_launches_from_runtime_working_directory(tmp_path):
    """The SDK scaffold declares ``python server.py`` relative to its plugin.

    Runtime MCP processes start from the workspace/runtime working directory,
    so the adaptor must make plugin-local executable arguments location-safe
    before handing them to the native renderer.
    """
    root = tmp_path / "generated-mcp"
    root.mkdir()
    (root / "plugin.yaml").write_text(
        "name: generated-mcp\n"
        "version: 0.1.0\n"
        "description: generated MCP scaffold\n"
        "kind: mcp-server\n"
        "contributes:\n"
        "  mcpServers:\n"
        "    - name: generated-mcp\n"
        "      command: python\n"
        "      args: [server.py]\n"
    )
    (root / "server.py").write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], "
        "'result': {'content': [{'type': 'text', 'text': 'hello'}]}}), flush=True)\n"
    )
    unrelated_cwd = tmp_path / "runtime-cwd"
    unrelated_cwd.mkdir()
    test_bin = tmp_path / "bin"
    test_bin.mkdir()
    (test_bin / "python").symlink_to(sys.executable)
    configs = tmp_path / "configs"
    configs.mkdir()
    calls = []

    await MCPServerAdaptor("generated-mcp", "claude_code").install(
        _ctx(root, configs, lambda n, s: calls.append((n, s)))
    )

    assert len(calls) == 1
    name, spec = calls[0]
    assert name == "generated-mcp"
    assert spec["args"] == [str((root / "server.py").resolve())]
    process = subprocess.run(
        [spec["command"], *spec["args"]],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/call"}\n',
        cwd=unrelated_cwd,
        env={**os.environ, "PATH": f"{test_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    response = json.loads(process.stdout)
    assert response["result"]["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_privileged_plugin_raises_when_runtime_unsupported(tmp_path):
    """If the active runtime's register_mcp_server raises NotImplementedError
    (an unverified-runtime fail-loud stub), the privileged management MCP install
    fails LOUDLY — a concierge on that runtime must not boot without
    create_workspace."""
    root = _make_plugin(
        tmp_path, {"mcpServers": {"molecule-platform": {"command": "npx"}}}
    )
    configs = tmp_path / "configs"
    configs.mkdir()

    def _unsupported(name, spec):
        raise NotImplementedError("runtime renderer not implemented")

    ctx = _ctx(root, configs, _unsupported)

    with pytest.raises(PrivilegedPluginInstallError):
        await MCPServerAdaptor(_PRIVILEGED_MCP_PLUGIN, "some-unverified-runtime").install(ctx)


@pytest.mark.asyncio
async def test_nonprivileged_plugin_records_error_when_runtime_unsupported(tmp_path):
    """A non-privileged MCP plugin on an unsupported runtime records an error
    but does not raise (ordinary plugin hiccup, not a concierge brick)."""
    root = _make_plugin(tmp_path, {"mcpServers": {"some-mcp": {"command": "npx"}}})
    configs = tmp_path / "configs"
    configs.mkdir()

    def _unsupported(name, spec):
        raise NotImplementedError("runtime renderer not implemented")

    ctx = _ctx(root, configs, _unsupported)
    result = await MCPServerAdaptor("some-mcp-plugin", "some-unverified-runtime").install(ctx)
    assert result.errors
    assert any("unsupported on runtime" in e for e in result.errors)


# ===========================================================================
# (d) Opt-out teardown — an audience=self entry is stripped from settings.json on
# uninstall, while every other (intentionally-not-removed) entry is preserved
# (RFC plugin-mcp-audience-contract, review MAJOR-4).
# ===========================================================================


@pytest.mark.asyncio
async def test_uninstall_strips_self_audience_entry_but_keeps_others(tmp_path):
    """(d) On opt-out, an audience=self mcpServers entry — credentialed at install
    with the workspace-token-file path + MOLECULE_MCP_MODE=self — is stripped from
    the rendered settings.json (entry AND its injected credential env), while every
    OTHER entry keeps the intentionally-not-removed behavior.

    Negative control on BOTH sides: inverting the audience gate so it strips
    nothing leaves 'schedule-self' present (first assert fails); dropping the
    per-entry audience scope so it strips everything removes 'image-gen' (second
    assert fails)."""
    from molecule_runtime import mcp_render

    # The plugin declares an audience=self mcpServers entry (what install wired).
    root = _make_plugin(
        tmp_path,
        {"mcpServers": {
            "schedule-self": {
                "command": "npx",
                "args": ["-y", "@molecule-ai/mcp-server"],
                "audience": "self",
            },
        }},
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    settings = mcp_render.default_json_settings_path(configs)

    # Simulate what install rendered: the self entry AFTER credential injection ...
    mcp_render.render_json_mcp_servers(settings, "schedule-self", {
        "command": "npx",
        "args": ["-y", "@molecule-ai/mcp-server"],
        "env": {
            "MOLECULE_MCP_MODE": "self",
            "MOLECULE_WORKSPACE_TOKEN_FILE": "/configs/.auth_token",
        },
    })
    # ... plus an unrelated, curated MCP entry that must SURVIVE opt-out.
    mcp_render.render_json_mcp_servers(settings, "image-gen", {"command": "npx", "args": ["gen"]})

    await MCPServerAdaptor("molecule-schedule-self", "claude_code").uninstall(
        _ctx(root, configs, lambda n, s: None)
    )

    servers = mcp_render.read_json_mcp_servers(settings)
    # the credentialed self entry (+ its injected token-file / self-mode env) is gone
    assert "schedule-self" not in servers
    # every other entry is intentionally preserved
    assert servers["image-gen"] == {"command": "npx", "args": ["gen"]}


@pytest.mark.asyncio
async def test_uninstall_leaves_non_self_entry_in_place(tmp_path):
    """(d) The narrow scope holds: a plugin whose mcpServers entry has NO audience
    (or a non-self audience) keeps the original intentionally-not-removed behavior —
    uninstall does NOT strip it. Inverting the audience scope to strip unconditionally
    fails here."""
    from molecule_runtime import mcp_render

    root = _make_plugin(
        tmp_path,
        {"mcpServers": {"image-gen": {"command": "npx", "args": ["gen"]}}},  # no audience
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    settings = mcp_render.default_json_settings_path(configs)
    mcp_render.render_json_mcp_servers(settings, "image-gen", {"command": "npx", "args": ["gen"]})

    await MCPServerAdaptor("image-gen-plugin", "claude_code").uninstall(
        _ctx(root, configs, lambda n, s: None)
    )

    # untouched — the intentionally-not-removed behavior for non-self entries
    assert mcp_render.read_json_mcp_servers(settings)["image-gen"] == {"command": "npx", "args": ["gen"]}


# --- ${secret:NAME} declarative env binding -------------------------------------


def test_secret_ref_resolves_from_lookup():
    """`${secret:NAME}` is replaced with the resolved value; input not mutated."""
    spec = {"command": "python", "env": {"ODOO_API_KEY": "${secret:ODOO_API_KEY}"}}
    out = _resolve_mcp_secret_refs(spec, lookup={"ODOO_API_KEY": "real-key-123"}.get)
    assert out["env"] == {"ODOO_API_KEY": "real-key-123"}
    assert spec["env"]["ODOO_API_KEY"] == "${secret:ODOO_API_KEY}"  # not mutated


def test_secret_ref_missing_secret_drops_key():
    """An unresolved `${secret:...}` is DROPPED (not passed as a literal) so the
    server can fall back to its own resolution (e.g. an audience:self self-fetch)."""
    spec = {"env": {"ODOO_API_KEY": "${secret:ODOO_API_KEY}", "KEEP": "x"}}
    out = _resolve_mcp_secret_refs(spec, lookup=lambda n: None)
    assert out["env"] == {"KEEP": "x"}


def test_secret_ref_leaves_bare_var_and_literals_untouched():
    """Only the explicit sigil resolves; bare `${VAR}`, literals, non-strings pass
    through (backward compatible with the hermes `${VAR}` passthrough)."""
    spec = {"env": {"BARE": "${ODOO_API_KEY}", "LIT": "http://x:8069", "N": 1}}
    out = _resolve_mcp_secret_refs(spec, lookup=lambda n: "SHOULD_NOT_APPEAR")
    assert out["env"] == {"BARE": "${ODOO_API_KEY}", "LIT": "http://x:8069", "N": 1}


def test_secret_ref_no_env_is_noop():
    spec = {"command": "python", "args": ["s.py"]}
    assert _resolve_mcp_secret_refs(spec, lookup=lambda n: "x") == spec


@pytest.mark.asyncio
async def test_install_resolves_secret_refs_in_env(tmp_path, monkeypatch):
    """End to end: install() resolves `${secret:NAME}` from the workspace env
    (os.environ) BEFORE the spec reaches register_mcp_server — so every runtime,
    including the claude-code JSON adapter that does not expand ${VAR}, receives
    the real value. Literals are left untouched."""
    monkeypatch.setenv("ODOO_API_KEY", "env-secret-xyz")
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.yaml").write_text(
        "name: demo-mcp\n"
        "version: 0.1.0\n"
        "description: demo\n"
        "contributes:\n"
        "  mcpServers:\n"
        "    - name: demo-mcp\n"
        "      command: python\n"
        "      args: [server.py]\n"
        "      env:\n"
        '        ODOO_API_KEY: "${secret:ODOO_API_KEY}"\n'
        '        ODOO_BASE_URL: "http://odoo:8069"\n'
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    calls = []
    ctx = _ctx(root, configs, lambda n, s: calls.append((n, s)))

    await MCPServerAdaptor("demo-mcp", "claude_code").install(ctx)

    assert len(calls) == 1
    _name, spec = calls[0]
    assert spec["env"]["ODOO_API_KEY"] == "env-secret-xyz"      # resolved from os.environ
    assert spec["env"]["ODOO_BASE_URL"] == "http://odoo:8069"   # literal untouched
