"""Compatibility alias for the canonical runtime plugin adaptors.

The implementation lives in :mod:`molecule_runtime.plugins_registry.builtins`.
This top-level package remains importable because published plugin adaptors use
``from plugins_registry.builtins import ...``.
"""

from __future__ import annotations

from molecule_runtime.plugins_registry import builtins as _canonical

AgentskillsAdaptor = _canonical.AgentskillsAdaptor
MCPServerAdaptor = _canonical.MCPServerAdaptor
SKIP_ROOT_MD = _canonical.SKIP_ROOT_MD

__all__ = ["AgentskillsAdaptor", "MCPServerAdaptor", "SKIP_ROOT_MD"]


def __getattr__(name: str):
    """Preserve private compatibility without creating a second implementation."""
    return getattr(_canonical, name)
