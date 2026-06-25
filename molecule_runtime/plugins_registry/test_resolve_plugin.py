"""Tests for _load_module_from_path sys.modules injection fix (issue #296).

Verifies that plugin adapters using "from plugins_registry.builtins import ..."
can be loaded via _load_module_from_path() without ModuleNotFoundError.
"""
import os
import tempfile
from pathlib import Path

from molecule_runtime.plugins_registry import _load_module_from_path


def test_load_adapter_with_plugins_registry_import():
    """Plugin adapter using 'from plugins_registry.builtins import ...' loads cleanly."""
    # Write a temp adapter file that does the exact import from the bug report.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
    ) as f:
        f.write("from plugins_registry.builtins import AgentskillsAdaptor as Adaptor\n")
        f.write("assert Adaptor is not None\n")
        adapter_path = Path(f.name)

    try:
        module = _load_module_from_path("test_adapter", adapter_path)
        assert module is not None, "module should load without error"
        assert hasattr(module, "Adaptor"), "module should expose Adaptor"
    finally:
        os.unlink(adapter_path)


def test_load_adapter_with_full_plugins_registry_import():
    """Plugin adapter using 'from plugins_registry import ...' loads cleanly."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
    ) as f:
        f.write("from plugins_registry import InstallContext, resolve\n")
        f.write("from plugins_registry.protocol import PluginAdaptor\n")
        f.write("assert InstallContext is not None\n")
        f.write("assert resolve is not None\n")
        f.write("assert PluginAdaptor is not None\n")
        adapter_path = Path(f.name)

    try:
        module = _load_module_from_path("test_adapter_full", adapter_path)
        assert module is not None, "module should load without error"
        assert hasattr(module, "InstallContext"), "module should expose InstallContext"
        assert hasattr(module, "resolve"), "module should expose resolve"
        assert hasattr(module, "PluginAdaptor"), "module should expose PluginAdaptor"
    finally:
        os.unlink(adapter_path)


def test_openclaw_privileged_mcp_resolves_to_mcp_adaptor(tmp_path):
    """Phase P4 / STEP 3: the privileged ``molecule-platform-mcp`` plugin on the
    ``openclaw`` runtime must resolve to :class:`MCPServerAdaptor` (which wires
    the MCP via ``register_mcp_server`` → openclaw renderer) — NOT to
    :class:`RawDropAdaptor`, which would silently copy files and warn "no tools
    wired" (a capability-less concierge).

    PROVE-FAIL: without the registry pin (``plugins_registry/
    molecule-platform-mcp/openclaw.py``) AND with a plugin_root that ships no
    descriptor, resolve() falls through every path to RawDropAdaptor. The registry
    pin (resolution path #1) makes the mapping explicit and unconditional.
    """
    from molecule_runtime.plugins_registry import AdaptorSource, resolve
    from molecule_runtime.plugins_registry.builtins import MCPServerAdaptor
    from molecule_runtime.plugins_registry.raw_drop import RawDropAdaptor

    # Empty plugin_root → no plugin-shipped adapter, no descriptor to sniff. Only
    # the platform-registry pin can keep this off the RawDrop fallback.
    adaptor, source = resolve("molecule-platform-mcp", "openclaw", tmp_path)

    assert source == AdaptorSource.REGISTRY
    assert isinstance(adaptor, MCPServerAdaptor)
    assert not isinstance(adaptor, RawDropAdaptor)


if __name__ == "__main__":
    test_load_adapter_with_plugins_registry_import()
    test_load_adapter_with_full_plugins_registry_import()
    test_openclaw_privileged_mcp_resolves_to_mcp_adaptor(Path(tempfile.mkdtemp()))
    print("ALL TESTS PASS")
