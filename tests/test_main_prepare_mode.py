"""Contract tests for the ``molecule-runtime-prepare`` entry (runtime#357 /
core#4587).

Prepare mode materializes config (mcp_servers + persona + skills) then exits
BEFORE serving, so a template can pre-write a complete config and launch its
agent gateway against it on the FIRST boot — eliminating the post-launch
gateway restart hermes >= 0.19 needed to pick up eagerly-discovered MCP
servers (a ~90s unreachable boot window).

``main()`` itself is ``# pragma: no cover`` (the whole boot orchestration); its
deep "returns right after adapter.setup()" behavior is validated by the live
boot/e2e gates. These tests pin the pieces that are unit-testable and that the
template depends on:
  * ``prepare_sync`` runs ``main(prepare_only=True)`` and translates its return
    into the process exit code (0 = materialized, non-zero = fell short so the
    caller falls back).
  * ``main`` accepts the ``prepare_only`` keyword — the template contract.
  * the ``molecule-runtime-prepare`` console entry is declared.
"""
from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from unittest import mock

import pytest

import molecule_runtime.main as main_mod


def test_main_accepts_prepare_only_keyword():
    """The template invokes prepare via a dedicated binary, but the param is
    the contract seam. If a refactor drops/renames it, prepare silently
    becomes a full serve — catch that here."""
    sig = inspect.signature(main_mod.main)
    assert "prepare_only" in sig.parameters
    assert sig.parameters["prepare_only"].default is False


@pytest.mark.parametrize(
    "returned, expected_code",
    [(0, 0), (1, 1), (None, 0)],  # None → defensive `or 0`
)
def test_prepare_sync_translates_main_return_to_exit_code(returned, expected_code):
    """prepare_sync must run main(prepare_only=True) and exit with main's
    return code (0 materialized / non-zero fell short). None coerces to 0."""
    called = {}

    async def fake_main(prepare_only=False):
        called["prepare_only"] = prepare_only
        return returned

    with mock.patch.object(main_mod, "main", fake_main):
        with pytest.raises(SystemExit) as exc:
            main_mod.prepare_sync()

    assert called["prepare_only"] is True
    assert exc.value.code == expected_code


def test_prepare_sync_propagates_setup_exception():
    """A privileged-plugin failure re-raises out of main(); prepare_sync must
    NOT swallow it into exit 0 — the caller needs the non-zero signal to fall
    back to its normal launch path."""

    async def boom(prepare_only=False):
        raise RuntimeError("privileged plugin setup blew up")

    with mock.patch.object(main_mod, "main", boom):
        with pytest.raises(RuntimeError, match="privileged plugin"):
            main_mod.prepare_sync()


def test_console_entry_declared():
    """The dedicated console entry is the capability signal the template
    detects via `command -v molecule-runtime-prepare`; a wheel without it
    must make the template skip the pre-step. Guard the declaration."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts.get("molecule-runtime-prepare") == "molecule_runtime.main:prepare_sync"
