"""SSOT for the set of LIVE runtimes and the operator-default runtime.

This module is the *single list* of the runtimes a workspace can boot on today,
declared ONCE. It is the **membership SSOT** the SDK official registry references
(``molecule-ai-sdk:contracts/adapter/official-runtimes.registry.json`` ->
``live_runtimes_ssot``); the manifest/skill-gating story reads it too.

ADR-004 note
------------
This module NO LONGER feeds a per-runtime *dispatch* fallback. Before ADR-004 the
shared engine held per-runtime renderers/readers/materializers in ``_RUNTIME_*``
dispatch tables (``mcp_render._RUNTIME_SPECS`` / ``persona_render._RUNTIME_PERSONA``)
and imported ``DEFAULT_RUNTIME`` from here as their unmapped-runtime fallback. ADR-004
moved that per-runtime shape INTO each adapter and deleted the engine dispatch
tables, so the engine no longer resolves a runtime by name and no longer imports
``DEFAULT_RUNTIME``. ``LIVE_RUNTIMES`` / ``DEFAULT_RUNTIME`` remain as the plain
membership + operator-default facts (a tuple + a string — carrying NO dispatch),
consumed by the SDK registry and any membership/policy check that needs the axis.

Membership policy
-----------------
``LIVE_RUNTIMES`` is the set of runtimes a workspace can actually boot on today.
The manifest ``runtimes`` field accepts a bounded, open RuntimeId namespace so
third-party adapters do not require a central schema edit. This tuple is the
separate curated registry of officially supported runtimes. Adding a runtime here
declares it part of that supported axis; its adapter (in the template repo) is what
implements the SDK adapter socket for it — the engine holds nothing per-runtime.
"""
from __future__ import annotations

# Underscore dispatch-key spelling (matches ``mcp_render.normalize_runtime`` /
# the ``_RUNTIME_*`` registry keys). ``openclaw`` is the operator DEFAULT.
#
# NOTE: keep this a plain tuple of normalized keys — every consumer normalizes
# its own input via ``normalize_runtime`` before comparing, so callers may pass
# either hyphen or underscore spelling.
LIVE_RUNTIMES: tuple[str, ...] = (
    "openclaw",     # operator DEFAULT (see DEFAULT_RUNTIME)
    "claude_code",
    "codex",
    "hermes",
)

# The operator-default runtime. This is the ONLY value the unmapped-runtime
# fallback in each contract renderer may resolve to — never ``claude_code``.
# Changing this changes every renderer's fallback in lockstep (single edit).
DEFAULT_RUNTIME: str = "openclaw"

assert DEFAULT_RUNTIME in LIVE_RUNTIMES, "DEFAULT_RUNTIME must be a live runtime"
