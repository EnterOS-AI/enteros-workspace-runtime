"""G6 guardrail — renderer-completeness for the kind=platform runtime allowlist.

Task #80 (de-bake guardrails).

A concierge (kind=platform) can run on any runtime in the platform allowlist.
Each such runtime MUST have a CONCRETE ``mcp_render._RUNTIME_SPECS`` entry — its
own native MCP-config path + renderer + present-reader — so the management MCP is
written to (and probed in) the file THAT runtime actually reads.

The #3159 failure mode: a runtime with no concrete entry falls through
``_spec_for`` to ``_DEFAULT_RUNTIME`` (claude_code) and is rendered/probed against
``.claude/settings.json`` — a file e.g. a codex or openclaw concierge never reads.
That silently capability-less concierge is exactly what the de-bake forbids.

This guardrail pins the invariant: for EVERY runtime a concierge is allowed to run
on, ``_spec_for`` returns that runtime's OWN concrete entry, never the default
fallback, and its renderer is not one of the deliberate fail-loud stubs. Adding a
new platform runtime without a concrete renderer trips this RED.
"""
from __future__ import annotations

import pytest

from molecule_runtime import mcp_render

# The kind=platform runtime allowlist (task #80): the runtimes a concierge is
# allowed to run on AND that have been pinned against a live CLI. claude-code is
# the base; codex (#142), openclaw (#179 / phase P4), and hermes are concrete
# renderers in the de-bake. Any runtime listed here may host the platform agent,
# so it must have native MCP rendering and probing.
PLATFORM_RUNTIME_ALLOWLIST = ("claude-code", "codex", "openclaw", "hermes")


@pytest.mark.parametrize("runtime", PLATFORM_RUNTIME_ALLOWLIST)
def test_platform_runtime_has_concrete_render_spec(runtime):
    """Each allowlisted platform runtime resolves to its OWN concrete
    ``_RUNTIME_SPECS`` entry — NOT the ``_DEFAULT_RUNTIME`` fallback."""
    key = mcp_render.normalize_runtime(runtime)
    assert key in mcp_render._RUNTIME_SPECS, (
        f"runtime {runtime!r} (key {key!r}) has NO concrete _RUNTIME_SPECS entry — "
        f"it would fall through to _DEFAULT_RUNTIME ({mcp_render._DEFAULT_RUNTIME}) "
        f"and be rendered against the wrong runtime's MCP config (the #3159 bug)."
    )
    # _spec_for must return the runtime's OWN tuple, not the default's. For
    # claude_code (which IS the default) identity holds trivially; for codex /
    # openclaw the tuples must be DISTINCT from the default to prove no fallback.
    own = mcp_render._spec_for(runtime)
    default = mcp_render._RUNTIME_SPECS[mcp_render._DEFAULT_RUNTIME]
    assert own is mcp_render._RUNTIME_SPECS[key]
    if key != mcp_render._DEFAULT_RUNTIME:
        assert own is not default, (
            f"{runtime!r} resolved to the _DEFAULT_RUNTIME spec — it has no "
            f"runtime-specific renderer (silent claude fallback)."
        )


@pytest.mark.parametrize("runtime", PLATFORM_RUNTIME_ALLOWLIST)
def test_platform_runtime_renderer_is_not_a_failloud_stub(runtime):
    """A concrete entry must have a REAL renderer, not a fail-loud stub.

    ``is_runtime_supported`` is False for deliberate stubs whose renderers
    raise NotImplementedError. Every allowlisted platform runtime must be
    supported."""
    assert mcp_render.is_runtime_supported(runtime) is True, (
        f"{runtime!r} is an unverified/stub runtime — a concierge cannot be "
        f"provisioned on it until its renderer is pinned against a live CLI."
    )
    assert mcp_render.normalize_runtime(runtime) not in mcp_render._UNVERIFIED_RUNTIMES


@pytest.mark.parametrize("runtime", PLATFORM_RUNTIME_ALLOWLIST)
def test_platform_runtime_native_path_is_runtime_specific(runtime, tmp_path):
    """The native MCP-config path each platform runtime resolves to is the one
    THAT runtime reads — and codex/openclaw do NOT resolve to claude's
    ``.claude/settings.json`` (the cross-runtime mis-attribution)."""
    path = str(mcp_render.mcp_settings_path_for(runtime, tmp_path))
    key = mcp_render.normalize_runtime(runtime)
    if key == "claude_code":
        assert path.endswith("/.claude/settings.json")
    elif key == "codex":
        assert path.endswith("/.codex/config.toml")
        assert ".claude/settings.json" not in path
    elif key == "openclaw":
        assert path.endswith("/.openclaw/openclaw.json")
        assert ".claude/settings.json" not in path
    elif key == "hermes":
        assert path.endswith("/.hermes/config.yaml")
        assert ".claude/settings.json" not in path
    else:  # pragma: no cover — allowlist is the parametrize source
        pytest.fail(f"unexpected allowlist runtime {runtime!r}")
