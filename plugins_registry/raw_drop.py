"""Compatibility alias for :mod:`molecule_runtime.plugins_registry.raw_drop`."""

from __future__ import annotations

import importlib
import sys

sys.modules[__name__] = importlib.import_module(
    "molecule_runtime.plugins_registry.raw_drop"
)
