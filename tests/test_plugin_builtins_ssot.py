"""Keep the package runtime adaptor as the single builtins implementation."""

from __future__ import annotations

import inspect

import plugins_registry.builtins as compatibility
from molecule_runtime.plugins_registry import builtins as canonical


def test_compatibility_builtins_reexports_canonical_classes() -> None:
    assert compatibility.AgentskillsAdaptor is canonical.AgentskillsAdaptor
    assert compatibility.MCPServerAdaptor is canonical.MCPServerAdaptor
    assert compatibility.SKIP_ROOT_MD is canonical.SKIP_ROOT_MD
    assert compatibility._SCRUB_KEYS is canonical._SCRUB_KEYS


def test_compatibility_builtins_contains_no_duplicate_implementation() -> None:
    source = inspect.getsource(compatibility)
    assert "class AgentskillsAdaptor" not in source
    assert "class MCPServerAdaptor" not in source
