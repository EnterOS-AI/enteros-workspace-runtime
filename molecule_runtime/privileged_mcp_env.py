"""Privileged-MCP env contract — generalize the per-template org-admin env merge.

Background — only openclaw-with-its-override got the org-admin env
================================================================
The privileged management MCP (``molecule-platform`` = ``@molecule-ai/mcp-server``)
must be spawned with the org-admin env (the controlplane URL + admin token +
org scope) or ``create_workspace`` / ``provision_workspace`` 401s even though the
server is declared. Today that env-merge lives ONLY in the openclaw template's
``register_mcp_server_hook`` override (``_oc-template/adapter.py``), which
hardcodes a loop over the LEGACY names
``MOLECULE_CP_URL / MOLECULE_ADMIN_TOKEN / PLATFORM_URL / WORKSPACE_ID /
MOLECULE_ORG_ID``. A codex/claude/gemini concierge using the BASE hook gets the
management MCP rendered with NO admin env.

This module lifts that merge into the base so EVERY runtime's privileged MCP is
enriched uniformly, wired at the ONE base funnel all adapters share — the
``register_mcp_server`` lambda in
``adapter_base.install_plugins_via_registry``. Because the lambda enriches the
spec BEFORE dispatch, even an overriding hook (openclaw) receives the
pre-enriched spec.

Env-name SSOT (resolves the legacy-vs-canonical discrepancy)
-----------------------------------------------------------
The task/#131 contract names the keys
``MOLECULE_API_URL / MOLECULE_API_KEY / ORG_API_KEY / ORG_SLUG / AUDIT_ACTOR``
(the canonical set), whereas the proven on-disk openclaw image injects the
LEGACY names. The proven server (verified on staging, image sha256:457cc621…)
reads the LEGACY names, so dropping them would 401 ``create_workspace``. We
therefore inject BOTH: each canonical key and its legacy alias are populated
from whichever of the two is set in env (canonical preferred), and the proven
LEGACY-only keys (``PLATFORM_URL``, ``WORKSPACE_ID``) are carried verbatim.
Descriptor-declared keys ALWAYS win — a spec that hardcodes a value is never
overwritten. Until the server's canonical-name migration is verified on
staging, keeping the aliases is the regression-safe choice.

Gating
------
``inject_privileged_env`` is a NO-OP unless ``name == MANAGEMENT_MCP_NAME``
(``molecule-platform``), so a tenant's image-gen / other MCP can NEVER receive
an admin token. Merge is idempotent (descriptor-wins, same values), so during
the template cutover the openclaw override's own loop double-injecting the
identical legacy values is harmless.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping

log = logging.getLogger(__name__)

# Canonical contract keys (task/#131). Public so tests + future callers assert
# against the SSOT set rather than re-listing the names.
PRIVILEGED_ENV_KEYS: tuple[str, ...] = (
    "MOLECULE_API_URL",
    "MOLECULE_API_KEY",
    "ORG_API_KEY",
    "ORG_SLUG",
    "AUDIT_ACTOR",
)

# Bidirectional 1:1 canonical<->legacy renames. A box that sets EITHER name
# populates BOTH keys (canonical value preferred when both are set), so:
#   * the proven server reading the legacy name keeps working (no regression);
#   * a server migrated to the canonical name works too (forward-compat).
_ALIAS_PAIRS: dict[str, str] = {
    "MOLECULE_API_URL": "MOLECULE_CP_URL",
    "MOLECULE_API_KEY": "MOLECULE_ADMIN_TOKEN",
    "ORG_SLUG": "MOLECULE_ORG_ID",
}

# Legacy keys the proven openclaw image injected that have no canonical rename
# but ARE read by the privileged MCP — carried verbatim from their own env var.
_LEGACY_ONLY_KEYS: tuple[str, ...] = ("PLATFORM_URL", "WORKSPACE_ID")

# Bidirectional alias lookup: canonical->legacy and legacy->canonical.
_ALIAS_OF: dict[str, str] = {}
for _canon, _legacy in _ALIAS_PAIRS.items():
    _ALIAS_OF[_canon] = _legacy
    _ALIAS_OF[_legacy] = _canon

# Every key we inject = canonical set ∪ legacy proven set. Ordered, de-duped.
_INJECTED_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *PRIVILEGED_ENV_KEYS,
            *_ALIAS_PAIRS.values(),
            *_LEGACY_ONLY_KEYS,
        )
    )
)


def _resolve(key: str, env: Mapping[str, str]) -> str | None:
    """Resolve a key's value from env: its own var first, then its alias var.

    Returns None when neither is set (so an absent value is never injected as an
    empty string — the descriptor / process env is left to provide it).
    """
    val = env.get(key)
    if val:
        return val
    alias = _ALIAS_OF.get(key)
    if alias:
        alias_val = env.get(alias)
        if alias_val:
            return alias_val
    return None


def inject_privileged_env(
    name: str,
    spec: dict,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Return a NEW spec with the org-admin env merged in — gated to the
    privileged management MCP.

    No-op (returns ``spec`` unchanged) unless ``name == MANAGEMENT_MCP_NAME``.
    For the management MCP, each injected key absent from the descriptor's
    ``env`` is resolved (canonical-or-alias) and merged as a literal;
    descriptor-declared keys win. Returns the spec unchanged when no values
    resolve (e.g. a non-concierge box with none of the env vars set), so an
    ordinary workspace's MCP specs are untouched.
    """
    from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

    if name != MANAGEMENT_MCP_NAME:
        return spec
    if env is None:
        env = os.environ

    descriptor_env = dict(spec.get("env") or {})
    resolved: dict[str, str] = {}
    for key in _INJECTED_KEYS:
        if key in descriptor_env:
            continue  # descriptor-declared value wins — never overwrite
        val = _resolve(key, env)
        if val:
            resolved[key] = val

    if not resolved:
        return spec

    new_spec = dict(spec)
    descriptor_env.update(resolved)
    new_spec["env"] = descriptor_env
    log.info(
        "inject_privileged_env: merged %d org-admin env key(s) into %r MCP spec",
        len(resolved), name,
    )
    return new_spec
