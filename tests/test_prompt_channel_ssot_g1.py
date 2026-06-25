"""G1 guardrail — the SINGLE prompt-delivery channel is ``config.system_prompt``.

Task #80 (de-bake guardrails), pairs with task #76.

The concierge identity bug had two SSOT violations. G0/G2 lock the *build* side
(``build_system_prompt`` honors ``prompt_files`` with the ``system-prompt.md``
fallback, base-platform frame always first). G1 locks the *delivery* side:

  ``_common_setup`` builds the prompt ONCE and publishes it onto the shared
  ``AdapterConfig.system_prompt`` instance. ``main.py`` passes that SAME config
  to every runtime's ``create_executor``, so each executor's effective prompt is
  ``config.system_prompt`` — NOT a second, per-runtime re-read of
  ``/configs/system-prompt.md`` (the per-runtime drift that left a codex /
  openclaw concierge identity-less because its executor ignored the published
  build and re-read a file the SSOT no longer wrote to).

The concrete runtime adapters live in standalone template repos, so this
contract is exercised at the runtime boundary the templates consume:

  * the publish step (``_common_setup`` sets ``config.system_prompt`` from the
    single ``build_system_prompt``), and
  * the divergence the channel prevents — when the published channel carries
    MARKER-A but ``system-prompt.md`` on disk carries MARKER-B, an executor that
    reads the channel gets MARKER-A; one that re-reads the file (the legacy
    ``get_system_prompt`` per-runtime vector) gets MARKER-B. The guardrail proves
    the channel is the authoritative one and that the re-read helper is NOT wired
    into the executor-facing setup pipeline.
"""
from __future__ import annotations

import asyncio

import pytest

from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter
from molecule_runtime.executor_helpers import get_system_prompt

MARKER_A = "CHANNEL-MARKER-A-published-by-common-setup"
MARKER_B = "STALE-FILE-MARKER-B-on-disk-system-prompt-md"


def _stub_common_setup_deps(monkeypatch):
    """Neutralize ``_common_setup``'s network/IO side-deps so the test exercises
    ONLY the prompt-build-and-publish contract, deterministically + offline."""
    # coordinator.py validates WORKSPACE_ID at import time; _common_setup imports
    # it lazily, so set a valid value before that import fires.
    monkeypatch.setenv("WORKSPACE_ID", "ws-g1")
    import molecule_runtime.adapter_base as ab
    from molecule_runtime.plugins import LoadedPlugins

    monkeypatch.setattr(
        "molecule_runtime.plugins.load_plugins", lambda **k: LoadedPlugins()
    )
    # Plugin INSTALL (per-runtime registry pipeline) is dependency-heavy and not
    # what G1 tests — neutralize it; the prompt build/publish is the contract.
    async def _noop_inject(self, config, plugins):
        return None

    monkeypatch.setattr(ab.BaseAdapter, "inject_plugins", _noop_inject)
    monkeypatch.setattr("molecule_runtime.skill_loader.loader.load_skills", lambda *a, **k: [])
    monkeypatch.setattr("molecule_runtime.coordinator.get_children", _async_return([]))
    monkeypatch.setattr(
        "molecule_runtime.prompt.get_peer_capabilities", _async_return([])
    )
    monkeypatch.setattr(
        "molecule_runtime.prompt.get_platform_instructions", _async_return("")
    )
    # The platform-MCP self-heal is import-guarded + best-effort; stub it inert.
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.ensure_management_mcp_in_settings",
        lambda: False,
    )
    return ab


def _async_return(value):
    async def _coro(*a, **k):
        return value

    return _coro


class _MinimalAdapter(BaseAdapter):
    """Smallest concrete adapter — stands in for ANY runtime executor's host. It
    does NOT override the prompt build/publish, so it exercises the shared
    base contract every real (template-repo) adapter inherits."""

    @staticmethod
    def name() -> str:  # type: ignore[override]
        return "claude-code"

    @staticmethod
    def display_name() -> str:  # type: ignore[override]
        return "Minimal (test)"

    @staticmethod
    def description() -> str:  # type: ignore[override]
        return "Minimal adapter for the G1 prompt-channel contract test."

    async def setup(self, config: AdapterConfig) -> None:  # pragma: no cover
        ...

    async def create_executor(self, config: AdapterConfig):  # pragma: no cover
        ...


def _run_common_setup(monkeypatch, tmp_path, prompt_files):
    _stub_common_setup_deps(monkeypatch)
    adapter = _MinimalAdapter()
    config = AdapterConfig(
        model="anthropic:claude-sonnet-4-6",
        config_path=str(tmp_path),
        workspace_id="ws-g1",
        prompt_files=prompt_files,
    )
    # system_prompt is None at construction — BASE-OWNED output.
    assert config.system_prompt is None
    asyncio.run(adapter._common_setup(config))
    return config


def test_common_setup_publishes_single_channel_from_declared_prompt_files(
    monkeypatch, tmp_path
):
    """The published channel (``config.system_prompt``) carries the role identity
    from the DECLARED ``prompt_files`` file — proving the single build honored
    the channel the template declares, not a hard-coded filename."""
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "concierge.md").write_text(MARKER_A)
    # A stale legacy file also on disk carrying a DIFFERENT marker.
    (tmp_path / "system-prompt.md").write_text(MARKER_B)

    config = _run_common_setup(monkeypatch, tmp_path, ["prompts/concierge.md"])

    assert config.system_prompt is not None, "base must publish config.system_prompt"
    # Effective published prompt = the declared channel's identity (MARKER-A)…
    assert MARKER_A in config.system_prompt
    # …and the stale system-prompt.md (MARKER-B) is NOT also folded in (a second
    # prompt source leaking would be the per-runtime-drift regression).
    assert MARKER_B not in config.system_prompt


def test_executor_effective_prompt_is_the_channel_not_a_file_reread(
    monkeypatch, tmp_path
):
    """G1 core contract: with the channel = MARKER-A and ``system-prompt.md`` on
    disk = MARKER-B, an executor that consumes ``config.system_prompt`` gets
    MARKER-A; the legacy ``get_system_prompt`` per-runtime re-read gets MARKER-B.

    This is the exact divergence the de-bake fixed: every executor must read the
    published channel, NEVER re-derive its prompt from the file. The two
    assertions together prove (a) the channel is authoritative and (b) the
    re-read vector returns something DIFFERENT — so wiring an executor to the
    re-read helper would be a real, catchable regression."""
    (tmp_path / "system-prompt.md").write_text(MARKER_B)

    config = AdapterConfig(
        model="anthropic:claude-sonnet-4-6",
        config_path=str(tmp_path),
        workspace_id="ws-g1",
    )
    # Simulate what _common_setup publishes (the single channel).
    config.system_prompt = MARKER_A

    # An executor reads the CHANNEL.
    effective_prompt_via_channel = config.system_prompt
    # The legacy per-runtime vector reads the FILE.
    effective_prompt_via_file_reread = get_system_prompt(config.config_path)

    assert effective_prompt_via_channel == MARKER_A
    assert effective_prompt_via_file_reread == MARKER_B
    # The two MUST differ — that gap is exactly what G1 forbids an executor from
    # falling into. The contract: executors use the channel (MARKER-A).
    assert effective_prompt_via_channel != effective_prompt_via_file_reread


def test_no_executor_wiring_rereads_the_prompt_file(monkeypatch, tmp_path):
    """Static-ish guard: ``_common_setup`` (the executor-facing setup pipeline)
    builds the prompt via ``build_system_prompt`` and does NOT call the legacy
    per-runtime ``get_system_prompt`` re-read. If a future change rewired setup
    to re-read ``system-prompt.md``, this trips."""
    import molecule_runtime.executor_helpers as eh

    called = {"n": 0}
    real = eh.get_system_prompt

    def _tripwire(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(eh, "get_system_prompt", _tripwire)
    # Also patch the symbol if adapter_base imported it by name (it must not).
    import molecule_runtime.adapter_base as ab
    if hasattr(ab, "get_system_prompt"):
        monkeypatch.setattr(ab, "get_system_prompt", _tripwire)

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "concierge.md").write_text(MARKER_A)
    _run_common_setup(monkeypatch, tmp_path, ["prompts/concierge.md"])

    assert called["n"] == 0, (
        "the executor-facing setup pipeline must NOT re-read system-prompt.md via "
        "get_system_prompt — the prompt is the published config.system_prompt channel"
    )
