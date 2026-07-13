"""Compatibility surface for the canonical runtime plugin registry.

Plugin adapters historically imported ``plugins_registry`` directly. The
implementation now lives under :mod:`molecule_runtime.plugins_registry`; this
package deliberately re-exports that one contract so import order cannot bind
an adapter to stale classes or resolution behavior.
"""

from __future__ import annotations

import importlib
import sys

from molecule_runtime import plugins_registry as _canonical

__all__ = list(_canonical.__all__)

for _name in __all__:
    globals()[_name] = getattr(_canonical, _name)

# Submodule imports must return the canonical module objects as well. This is
# load-bearing for ``from plugins_registry.builtins import AgentskillsAdaptor``
# in already-published plugin adapters.
for _submodule in ("builtins", "protocol", "raw_drop"):
    sys.modules[f"{__name__}.{_submodule}"] = importlib.import_module(
        f"molecule_runtime.plugins_registry.{_submodule}"
    )


def __getattr__(name: str):
    return getattr(_canonical, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_canonical)))
