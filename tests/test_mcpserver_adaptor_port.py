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
