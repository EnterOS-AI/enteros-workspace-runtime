"""Smoke tests for module imports.

Catches the class of bug fixed by the absolute-import switch
(monorepo legacy `from adapters import …` worked when something on
sys.path had an `adapters/` package; modular template repos that ship
only `/app/adapter.py` broke). Any future regression to relative-style
top-level imports gets caught here before publish.

These tests run in a clean Python environment with NO `adapters/`
package on sys.path — exactly the deployment shape consumers see.
"""

import importlib
import sys

import pytest


# Every module that previously had `from adapters import …` or
# `from adapters.… import …`. Importing any of them must succeed
# without an `adapters/` package on sys.path.
MODULES = [
    "molecule_runtime",
    "molecule_runtime.adapters",
    "molecule_runtime.adapters.base",
    "molecule_runtime.main",
    "molecule_runtime.a2a_executor",
    "molecule_runtime.coordinator",
    "molecule_runtime.prompt",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports_without_top_level_adapters_pkg(module_name, monkeypatch):
    # Sanity: no top-level `adapters` package shadowing molecule_runtime.adapters.
    assert "adapters" not in sys.modules or sys.modules["adapters"].__name__ != "adapters", (
        "test environment must not have a top-level `adapters` package — "
        "this test catches the regression of importing `from adapters import …` "
        "instead of `from molecule_runtime.adapters import …`"
    )
    # main + coordinator require WORKSPACE_ID at import time (production-strict
    # gate added monorepo-side); set a sentinel so the import smoke test
    # exercises the import path without triggering the runtime side-effect.
    monkeypatch.setenv("WORKSPACE_ID", "test-import-smoke")
    mod = importlib.import_module(module_name)
    assert mod is not None
    # Re-import via importlib should be idempotent.
    mod2 = importlib.import_module(module_name)
    assert mod is mod2


def test_get_adapter_resolves_via_absolute_path():
    from molecule_runtime.adapters import get_adapter
    assert callable(get_adapter)


def test_no_top_level_adapters_imports_remain():
    """Grep guard: keep the import-style invariant explicit so a future
    drive-by change doesn't silently reintroduce `from adapters import`."""
    import os
    pkg_dir = os.path.dirname(importlib.import_module("molecule_runtime").__file__)
    offenders = []
    for dirpath, _, files in os.walk(pkg_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    stripped = line.lstrip()
                    if stripped.startswith("from adapters") or stripped.startswith("import adapters"):
                        offenders.append(f"{path}:{lineno}: {line.rstrip()}")
    assert not offenders, (
        "Top-level `adapters` imports found — must be `from molecule_runtime.adapters …`:\n  "
        + "\n  ".join(offenders)
    )
