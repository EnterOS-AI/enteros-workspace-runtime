"""Legacy ``plugins_registry`` imports must share the canonical runtime API."""

from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys
import textwrap

import molecule_runtime.plugins_registry as canonical


def test_legacy_registry_reexports_canonical_contract() -> None:
    legacy = importlib.import_module("plugins_registry")
    legacy_builtins = importlib.import_module("plugins_registry.builtins")
    legacy_protocol = importlib.import_module("plugins_registry.protocol")
    canonical_builtins = importlib.import_module(
        "molecule_runtime.plugins_registry.builtins"
    )
    canonical_protocol = importlib.import_module(
        "molecule_runtime.plugins_registry.protocol"
    )

    assert legacy.resolve is canonical.resolve
    assert legacy.AdaptorSource is canonical.AdaptorSource
    assert legacy_builtins is canonical_builtins
    assert legacy_protocol is canonical_protocol
    assert legacy_protocol.InstallContext is canonical_protocol.InstallContext
    assert "register_mcp_server" in canonical_protocol.InstallContext.__dataclass_fields__


def test_legacy_registry_uses_canonical_mcp_resolution(tmp_path) -> None:
    legacy = importlib.import_module("plugins_registry")
    canonical_builtins = importlib.import_module(
        "molecule_runtime.plugins_registry.builtins"
    )
    plugin = tmp_path / "generated-mcp"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "name: generated-mcp\n"
        "version: 0.1.0\n"
        "description: compatibility test\n"
        "contributes:\n"
        "  mcpServers:\n"
        "    - name: generated-mcp\n"
        "      command: python\n"
        "      args: [server.py]\n"
    )

    adaptor, source = legacy.resolve("generated-mcp", "codex", plugin)

    assert isinstance(adaptor, canonical_builtins.MCPServerAdaptor)
    assert source == canonical.AdaptorSource.MCP_SERVER


def test_canonical_first_adapter_import_uses_identical_legacy_submodules() -> None:
    code = textwrap.dedent(
        """
        import importlib
        from pathlib import Path
        import tempfile

        import molecule_runtime.plugins_registry as canonical
        canonical_builtins = importlib.import_module(
            "molecule_runtime.plugins_registry.builtins"
        )
        root = Path(tempfile.mkdtemp())
        adapter = root / "adapter.py"
        adapter.write_text(
            "from plugins_registry.builtins import AgentskillsAdaptor as Adaptor\\n"
        )

        loaded = canonical._load_module_from_path("compat_order.adapter", adapter)
        legacy_builtins = importlib.import_module("plugins_registry.builtins")

        assert loaded is not None
        assert legacy_builtins is canonical_builtins
        assert loaded.Adaptor is canonical_builtins.AgentskillsAdaptor
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
