"""Tests for get_adapter() ADAPTER_MODULE resolution.

The claude-code, langgraph, and openclaw template adapters in prod on
2026-04-20 did NOT add the `Adapter = XxxAdapter` alias that the loader
originally required. The runtime now falls back to scanning the module
for any BaseAdapter subclass when the alias is missing. These tests pin
that behaviour so a future "cleanup" doesn't re-introduce the footgun.
"""

import sys
import types
import pytest

from molecule_runtime.adapters import get_adapter
from molecule_runtime.adapters.base import BaseAdapter


class _FakeAdapterCC(BaseAdapter):
    """Shaped like the real ClaudeCodeAdapter (class name != 'Adapter')."""

    @staticmethod
    def name() -> str:
        return "claude-code"

    @staticmethod
    def display_name() -> str:
        return "Claude Code"

    @staticmethod
    def description() -> str:
        return "Test adapter"

    @staticmethod
    def get_config_schema() -> dict:
        return {}

    async def setup(self, config):
        return None

    async def build_agent_executor(self, config):
        return None


def _install_module(monkeypatch, name, **attrs):
    """Stage a fake module in sys.modules so importlib.import_module finds it."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)


def test_loader_picks_up_explicit_alias(monkeypatch):
    _install_module(monkeypatch, "adapter_with_alias", Adapter=_FakeAdapterCC)
    monkeypatch.setenv("ADAPTER_MODULE", "adapter_with_alias")
    cls = get_adapter("claude-code")
    assert cls is _FakeAdapterCC


def test_loader_falls_back_to_any_baseadapter_subclass(monkeypatch):
    # Only the canonical-name export is missing. The ClaudeCodeAdapter
    # class alone should still resolve — that's the whole bug fix.
    _install_module(
        monkeypatch, "adapter_without_alias", ClaudeCodeAdapter=_FakeAdapterCC
    )
    monkeypatch.setenv("ADAPTER_MODULE", "adapter_without_alias")
    cls = get_adapter("claude-code")
    assert cls is _FakeAdapterCC


def test_loader_ignores_non_adapter_attrs(monkeypatch):
    # Module has noise (constants, functions, unrelated classes); the
    # loader must skip past them and still find the real adapter.
    class NotAnAdapter:  # intentionally NOT a BaseAdapter
        pass

    _install_module(
        monkeypatch,
        "adapter_with_noise",
        SOME_CONST=42,
        helper=lambda: None,
        Junk=NotAnAdapter,
        MyClaudeAdapter=_FakeAdapterCC,
    )
    monkeypatch.setenv("ADAPTER_MODULE", "adapter_with_noise")
    cls = get_adapter("claude-code")
    assert cls is _FakeAdapterCC


def test_loader_errors_when_module_has_no_subclass(monkeypatch):
    _install_module(monkeypatch, "adapter_empty", SOMETHING=42)
    monkeypatch.setenv("ADAPTER_MODULE", "adapter_empty")
    with pytest.raises(KeyError, match="no BaseAdapter subclass"):
        get_adapter("claude-code")


def test_loader_errors_when_module_missing(monkeypatch):
    monkeypatch.setenv("ADAPTER_MODULE", "definitely_not_installed_xyz")
    with pytest.raises(KeyError, match="could not be imported"):
        get_adapter("claude-code")


def test_loader_does_not_return_baseadapter_itself(monkeypatch):
    # Don't accidentally return BaseAdapter when it's re-exported by the
    # module — regression guard for the fallback scan.
    _install_module(monkeypatch, "adapter_reexports_base", BaseAdapter=BaseAdapter)
    monkeypatch.setenv("ADAPTER_MODULE", "adapter_reexports_base")
    with pytest.raises(KeyError, match="no BaseAdapter subclass"):
        get_adapter("claude-code")
