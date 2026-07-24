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


@pytest.fixture(autouse=True)
def _isolate_prepare_env():
    """prepare_sync writes MOLECULE_RUNTIME_PREPARE_ONLY into the real process
    env (correct in production — it runs in its own subprocess). In-test that
    would leak into sibling suites (e.g. boot_step_emit's, which the env now
    silences), so snapshot and restore it around every test here."""
    import os
    saved = os.environ.get("MOLECULE_RUNTIME_PREPARE_ONLY")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("MOLECULE_RUNTIME_PREPARE_ONLY", None)
        else:
            os.environ["MOLECULE_RUNTIME_PREPARE_ONLY"] = saved


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

    import os
    # prepare_sync writes MOLECULE_RUNTIME_PREPARE_ONLY into the REAL process
    # env (in production it runs in its own subprocess so that never leaks to
    # the serve; in-test it would leak into other tests, e.g. boot_step_emit's).
    # Restore it around this test.
    _saved = os.environ.pop("MOLECULE_RUNTIME_PREPARE_ONLY", None)
    try:
        with mock.patch.object(main_mod, "main", fake_main):
            with pytest.raises(SystemExit) as exc:
                main_mod.prepare_sync()

        assert called["prepare_only"] is True
        assert exc.value.code == expected_code
        # review wf_3a7b849d #7: prepare_sync couples the adapter env to the
        # param at the source, so the binary is self-sufficient (the adapter
        # probe-skip fires even if a caller forgets to export the env).
        assert os.environ.get("MOLECULE_RUNTIME_PREPARE_ONLY") == "1"
    finally:
        os.environ.pop("MOLECULE_RUNTIME_PREPARE_ONLY", None)
        if _saved is not None:
            os.environ["MOLECULE_RUNTIME_PREPARE_ONLY"] = _saved


def test_prepare_sync_bounds_with_internal_deadline(monkeypatch):
    """review wf_3a7b849d #15: prepare_sync must impose its OWN deadline so a
    hung caller (no external timeout) cannot wedge boot forever — on timeout it
    exits non-zero (fell short) so the caller falls back."""
    import asyncio as _asyncio

    monkeypatch.setenv("MOLECULE_RUNTIME_PREPARE_DEADLINE_SECS", "0.2")
    monkeypatch.delenv("MOLECULE_RUNTIME_PREPARE_ONLY", raising=False)

    async def hang(prepare_only=False):
        await _asyncio.sleep(10)  # exceeds the 0.2s deadline
        return 0

    try:
        with mock.patch.object(main_mod, "main", hang):
            with pytest.raises(SystemExit) as exc:
                main_mod.prepare_sync()
        assert exc.value.code == 1, "a prepare that blows its deadline must exit non-zero (fall back), not hang"
    finally:
        import os
        os.environ.pop("MOLECULE_RUNTIME_PREPARE_ONLY", None)


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
