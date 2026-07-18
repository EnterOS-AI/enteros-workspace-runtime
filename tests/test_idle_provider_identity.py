"""Unit tests for the identity-capabilities digest provider (task #219, PR-3).

Seams (persona reader, tool inventory, platform-agent check) are injected, so
these run without a live workspace or the heavy platform modules.
"""
from __future__ import annotations

import pytest

from molecule_runtime.idle_digest import Band, Policy, assemble, validate
from molecule_runtime.idle_digest.providers import IdentityCapabilitiesProvider


def _provider(persona="", tools=None, is_platform=False, **kw):
    return IdentityCapabilitiesProvider(
        workspace_name=kw.pop("workspace_name", "core-devops"),
        runtime_kind=kw.pop("runtime_kind", "claude-code"),
        persona_reader=lambda cp, pf: persona,
        tools_source=lambda: tools,
        platform_agent_check=lambda: is_platform,
        # Neutralize the in-process bridge union so these tests keep pinning
        # the OBSERVED-inventory rendering; the union has its own tests below.
        bridge_tools_source=kw.pop("bridge_tools_source", lambda: []),
        **kw,
    )


@pytest.mark.asyncio
async def test_second_person_voice_and_responsibility():
    persona = "Fleet infrastructure engineer for the molecule-ai org.\nKeep CI, deploys, and fleet infra healthy."
    [c] = await _provider(persona=persona).contribute()
    assert c.summary.startswith("You are core-devops — Fleet infrastructure engineer")
    assert "Your responsibility: Keep CI, deploys" in c.summary
    assert "Priorities: user first;" in c.summary


@pytest.mark.asyncio
async def test_empty_persona_falls_back():
    [c] = await _provider(persona="", runtime_kind="codex").contribute()
    assert c.summary.startswith("You are core-devops — codex workspace.")
    assert "Your responsibility:" not in c.summary  # nothing to derive


@pytest.mark.asyncio
async def test_markdown_stripped_from_role_line():
    [c] = await _provider(persona="# Docs specialist\n").contribute()
    assert "You are core-devops — Docs specialist" in c.summary
    assert "#" not in c.summary.splitlines()[0]


@pytest.mark.asyncio
async def test_is_pinned_tier0_and_valid():
    [c] = await _provider(persona="X").contribute()
    assert c.band is Band.PINNED and c.tier == 0
    validate(c)  # engine accepts the pinned header from the reserved id


@pytest.mark.asyncio
async def test_no_time_varying_field():
    [c] = await _provider(persona="X").contribute()
    assert c.age_band.value == "none" and c.item_ids == ()


# ---------------------------------------------------------------------------
# tool partitioning by origin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_and_plugin_groups():
    tools = [
        "mcp__molecule__delegate_task",
        "mcp__molecule__task_list",
        "mcp__gitea__list_prs",
        "mcp__grafana__query",
    ]
    [c] = await _provider(tools=tools, is_platform=False).contribute()
    assert "workspace MCP (2): delegate_task · task_list" in c.summary
    assert "from plugins: gitea · grafana" in c.summary
    assert c.count == 4


@pytest.mark.asyncio
async def test_platform_group_hidden_for_specialist():
    tools = [
        "mcp__molecule__task_list",
        "mcp__molecule-platform__create_workspace",
    ]
    [c] = await _provider(tools=tools, is_platform=False).contribute()
    assert "platform MCP" not in c.summary  # specialist never sees it
    assert "workspace MCP (1)" in c.summary


@pytest.mark.asyncio
async def test_platform_group_shown_for_platform_agent():
    tools = [
        "mcp__molecule__task_list",
        "mcp__molecule-platform__create_workspace",
        "mcp__molecule-platform__assign_agent",
    ]
    [c] = await _provider(tools=tools, is_platform=True).contribute()
    assert "platform MCP (2): assign_agent · create_workspace" in c.summary


@pytest.mark.asyncio
async def test_names_capped_with_ellipsis():
    tools = [f"mcp__molecule__tool_{n:02d}" for n in range(20)]
    [c] = await _provider(tools=tools).contribute()
    line = next(ln for ln in c.summary.splitlines() if "workspace MCP" in ln)
    assert "workspace MCP (20):" in line and line.rstrip().endswith("…")


@pytest.mark.asyncio
async def test_none_tools_inventory_is_safe():
    [c] = await _provider(tools=None).contribute()
    assert "workspace MCP (0)" in c.summary and c.count == 0


# ---------------------------------------------------------------------------
# graceful failure of injected seams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_reader_raising_is_survived():
    def boom(cp, pf):
        raise OSError("no such file")

    p = IdentityCapabilitiesProvider(
        workspace_name="x",
        persona_reader=boom,
        tools_source=lambda: [],
        platform_agent_check=lambda: False,
        bridge_tools_source=lambda: [],
    )
    [c] = await p.contribute()
    assert c.summary.startswith("You are x —")  # fell back, did not raise


@pytest.mark.asyncio
async def test_tools_source_raising_is_survived():
    def boom():
        raise RuntimeError("probe down")

    p = IdentityCapabilitiesProvider(
        workspace_name="x",
        persona_reader=lambda cp, pf: "Role",
        tools_source=boom,
        platform_agent_check=lambda: False,
        bridge_tools_source=lambda: [],
    )
    [c] = await p.contribute()
    assert c.count == 0 and "workspace MCP (0)" in c.summary


# ---------------------------------------------------------------------------
# integration with the assembler engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_is_official_and_accepted_by_runner():
    """The provider owns the reserved 'identity-capabilities' id, so it MUST be
    marked official or the runner's reserved-id trust check would reject it."""
    from molecule_runtime.idle_digest import ProviderRunner

    p = _provider(persona="Role")
    assert p.official is True and p.provider_id == "identity-capabilities"
    res = await ProviderRunner(Policy()).gather([p])
    assert len(res.contributions) == 1 and not res.newly_disabled


@pytest.mark.asyncio
async def test_header_assembles_first_and_is_empty_alone():
    from molecule_runtime.idle_digest import Contribution, Urgency, AgeBand

    [header] = await _provider(persona="Role line").contribute()
    body = Contribution(
        provider_id="task-queue", band=Band.BASE, tier=1, urgency=Urgency.NORMAL,
        count=1, summary="1 queued task", age_band=AgeBand.NONE,
    )
    d = assemble([body, header], Policy())
    assert d.contributions[0].band is Band.PINNED  # header renders first
    assert d.is_empty is False
    # header alone is empty (never fires)
    d_header_only = assemble([header], Policy())
    assert d_header_only.is_empty is True


@pytest.mark.asyncio
async def test_bridge_union_fills_workspace_mcp_for_http_wired_runtimes():
    """The core defect: the stdio spawn-probe skips http/url-wired servers, so
    a hermes-style runtime OBSERVES only the management MCP and the header
    reads "workspace MCP (0)" — telling the agent it lacks the bridge tools it
    actually serves in-process. The bridge union must fill them in."""
    p = _provider(
        persona="Role",
        tools=["mcp__molecule-platform__create_workspace"],
        is_platform=True,
        bridge_tools_source=lambda: [
            "mcp__molecule__send_message_to_user",
            "mcp__molecule__delegate_task",
        ],
    )
    [c] = await p.contribute()
    assert "workspace MCP (2): delegate_task \u00b7 send_message_to_user" in c.summary or \
        "workspace MCP (2): delegate_task · send_message_to_user" in c.summary
    assert "workspace MCP (0)" not in c.summary


@pytest.mark.asyncio
async def test_bridge_union_dedups_stdio_probed_ids():
    """Stdio-wired runtimes already observe the same mcp__molecule__* ids —
    the union must not double-count."""
    p = _provider(
        persona="Role",
        tools=["mcp__molecule__send_message_to_user"],
        bridge_tools_source=lambda: ["mcp__molecule__send_message_to_user"],
    )
    [c] = await p.contribute()
    assert "workspace MCP (1)" in c.summary


@pytest.mark.asyncio
async def test_bridge_source_raising_is_survived():
    def boom():
        raise RuntimeError("registry import broke")

    p = _provider(persona="Role", tools=["mcp__molecule__x"], bridge_tools_source=boom)
    [c] = await p.contribute()
    assert "workspace MCP (1)" in c.summary  # observed still renders
