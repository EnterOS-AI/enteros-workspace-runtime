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
MOLECULE_ORG_ID``. A codex/claude/hermes concierge using the BASE hook gets the
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

Gating — now audience-driven (RFC plugin-mcp-audience-contract, self-schedule v1)
--------------------------------------------------------------------------------
``inject_privileged_env`` dispatches on a plugin's DECLARED ``audience`` (the
scalar ``self``|``org`` on an mcpServers descriptor, SDK contract-version 0.5.0).
When ``audience`` is ABSENT it is DERIVED — ``org`` for ``MANAGEMENT_MCP_NAME``
(``molecule-platform``), else ``None`` — a backward-compat bridge so every
existing manifest keeps its EXACT current behavior with no flag-day.

  * ``org``  → the org-admin env merge, byte-identical to before, and STILL gated
    internally to ``name == MANAGEMENT_MCP_NAME``. This is THE invariant that keeps
    the org-admin key off ordinary boxes (core ``platform_agent_test.go``): a
    self-declared ``audience:"org"`` on any other plugin name resolves NOTHING, and
    v1 defers org injection to that core-verified name (review security-major-2).
  * ``self`` → the workspace's own-identity credential, the credential-critical
    keys injected AUTHORITATIVELY (injector-wins, like ``MOLECULE_MCP_MODE``): the
    workspace-token FILE PATH (never the token VALUE — /configs/.auth_token is
    restart-rotated), ``MOLECULE_MCP_MODE=self``, the workspace's OWN id
    (``WORKSPACE_ID`` / ``MOLECULE_WORKSPACE_ID``, so the self server resolves its
    own id), and the tenant API base URL. NO org key, ever.
  * ``None`` → no-op (a tenant's image-gen / other non-audience MCP can NEVER
    receive an admin token — the original invariant, preserved).

Merge is idempotent (descriptor-wins, same values), so during the template
cutover the openclaw override's own loop double-injecting the identical legacy
values is harmless.
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
    # Canonical org-api-key per molecule-ai-sdk contracts/credentials
    # (management_mcp_env.required). The management tools authenticate with
    # MOLECULE_ORG_API_KEY (mcp-server src/tools/management/client.ts, strict, no
    # alias) and core sets it under that name — so the child MUST receive it under
    # MOLECULE_ORG_API_KEY. The unprefixed ORG_API_KEY was the old allowlist name,
    # set/read by nobody; it stripped MOLECULE_ORG_API_KEY and AUTH_ERROR'd the
    # concierge. Demoted to the legacy alias below.
    "MOLECULE_ORG_API_KEY",
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
    "MOLECULE_ORG_API_KEY": "ORG_API_KEY",
    "ORG_SLUG": "MOLECULE_ORG_ID",
}

# Legacy keys the proven openclaw image injected that have no canonical rename
# but ARE read by the privileged MCP — carried verbatim from their own env var.
_LEGACY_ONLY_KEYS: tuple[str, ...] = ("PLATFORM_URL", "WORKSPACE_ID")

# ── audience=self delivery (RFC plugin-mcp-audience-contract, self-schedule v1) ──
# The self audience is the workspace acting as ITSELF; its bearer is the
# NON-privileged workspace token, NOT the org key. The in-container SSOT for that
# token is the restart-ROTATED file /configs/.auth_token (platform_auth.py), so the
# self injector passes the FILE PATH and NEVER the token VALUE — a spawned mcp-server
# child re-reads the file each request and never holds a snapshot that 401s after the
# next restart rotates the token (RFC review BLOCKER). MOLECULE_MCP_MODE=self selects
# the workspace-token registry inside @molecule-ai/mcp-server. No org key is ever
# injected on this path — the audience mapping IS the security model.
WORKSPACE_TOKEN_FILE = "/configs/.auth_token"
WORKSPACE_TOKEN_FILE_ENV = "MOLECULE_WORKSPACE_TOKEN_FILE"
SELF_MCP_MODE = "self"
# The tenant API base URL the self MCP calls; resolved from whichever the container
# already carries (canonical first, then its legacy alias, then PLATFORM_URL). This
# is the base URL, NOT the org key. The self server otherwise falls back to
# localhost, so we ALWAYS resolve one and log a misconfig when the container carries
# none (review: reachability).
_SELF_API_URL_ENV = "MOLECULE_API_URL"
_SELF_API_URL_SOURCE_KEYS: tuple[str, ...] = (
    "MOLECULE_API_URL",
    "MOLECULE_CP_URL",  # legacy alias of MOLECULE_API_URL (see _ALIAS_PAIRS)
    "PLATFORM_URL",
)

# The workspace's OWN id. The self server's ``selfWorkspaceId()`` reads ONLY these
# names, so create_schedule's self-default resolves the own id reliably only when
# they are present in the child's env. The org branch already injects WORKSPACE_ID;
# the self branch must too — the spawned MCP child is not guaranteed to inherit the
# container env ambiently across runtimes (review: functional). Sourced from the
# container env; injected under BOTH names the server may read.
_SELF_WORKSPACE_ID_ENVS: tuple[str, ...] = ("WORKSPACE_ID", "MOLECULE_WORKSPACE_ID")
_SELF_WORKSPACE_ID_SOURCE_KEYS: tuple[str, ...] = ("WORKSPACE_ID", "MOLECULE_WORKSPACE_ID")

# Sentinel distinguishing an ABSENT ``audience`` key (→ derive the default) from a
# PRESENT one whose value is falsy/invalid (→ honored as an explicit, no-injection
# declaration; NEVER silently re-derived to the name-based default). Used by
# inject_privileged_env to close the truthy-``or`` coercion hole (review:
# correctness).
_MISSING: object = object()

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


def _without_audience(spec: dict) -> dict:
    """Drop the delivery-directive ``audience`` key from a rendered spec.

    ``audience`` is a delivery directive, not a child env var, so it must never
    render into the native ``mcpServers`` entry. Returns the SAME object when there
    is no ``audience`` key — so the org path's ``out is spec`` no-op identity is
    preserved for every existing manifest (none of which carry ``audience``);
    otherwise a shallow copy without it.
    """
    if "audience" not in spec:
        return spec
    stripped = dict(spec)
    stripped.pop("audience", None)
    return stripped


def inject_privileged_env(
    name: str,
    spec: dict,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Return a spec with the credential for its DECLARED AUDIENCE injected.

    The audience field IS the security model (RFC plugin-mcp-audience-contract):

      * ``audience`` is read from the descriptor; when ABSENT it is DERIVED —
        ``org`` for the management MCP name (``MANAGEMENT_MCP_NAME``), else
        ``None``. This backward-compat bridge keeps every existing manifest on its
        exact current behavior with NO flag-day.
      * ``org``  → the org-admin env merge, EXACTLY as before, and STILL gated
        internally to ``name == MANAGEMENT_MCP_NAME`` (a core-verified signal). A
        self-declared ``audience:"org"`` on any other plugin name resolves NOTHING:
        the org key can never reach an ordinary box, and v1 defers org injection to
        the core-verified name (review security-major-2).
      * ``self`` → inject the workspace-token FILE PATH, ``MOLECULE_MCP_MODE=self``,
        and the tenant API base URL. Never injects any org key or token VALUE.
      * ``None`` → no-op; the spec is returned unchanged (same object).

    The ``audience`` key itself is stripped from the rendered spec.
    """
    from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

    # Presence check, NOT a truthy-``or``: an EXPLICITLY declared audience — even a
    # falsy one (``""`` / ``null`` / ``False``) — is honored AS DECLARED and NEVER
    # silently re-derived to the name-based default. Only an ABSENT ``audience`` key
    # derives (the backward-compat bridge: ``org`` for the management name, else
    # ``None``). A present-but-falsy/invalid value therefore resolves to NO injection
    # below — closing the coercion hole where e.g. ``audience: null`` (or ``""``) on
    # the management name would have been upgraded to a full org-admin injection.
    declared = spec.get("audience", _MISSING)
    if declared is _MISSING:
        audience = "org" if name == MANAGEMENT_MCP_NAME else None
        if audience is None:
            return spec  # not an audience-bearing surface — untouched (same object)
    else:
        audience = declared
    if env is None:
        env = os.environ

    rendered = _without_audience(spec)
    if audience == "self":
        return _inject_self_env(name, rendered, env)
    if audience == "org":
        return _inject_org_env(name, rendered, env)
    # A PRESENT but falsy/unknown audience (absent-derived None was returned above;
    # the schema forbids other values) — fail safe: inject NOTHING, but still strip
    # the bogus key so it never renders as a literal env directive.
    return rendered


def _inject_org_env(
    name: str,
    spec: dict,
    env: Mapping[str, str],
) -> dict:
    """audience=``org`` — the org-admin env merge, byte-identical to the original
    ``inject_privileged_env`` behavior.

    STILL gated to ``name == MANAGEMENT_MCP_NAME``: this is the invariant that keeps
    the org-admin key off ordinary boxes (core ``platform_agent_test.go``). A
    self-declared ``audience:"org"`` on a non-management plugin therefore resolves
    NOTHING — org injection stays anchored to the core-verified NAME, never a
    manifest field (review security-major-2). Each injected key absent from the
    descriptor ``env`` is resolved (canonical-or-alias) and merged; descriptor keys
    win. Returns the spec unchanged when nothing resolves.
    """
    from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

    if name != MANAGEMENT_MCP_NAME:
        return spec

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


def _inject_self_env(
    name: str,
    spec: dict,
    env: Mapping[str, str],
) -> dict:
    """audience=``self`` — inject the workspace's OWN-identity credential.

    Injects, all sourced from the container env:

      * ``MOLECULE_WORKSPACE_TOKEN_FILE`` -> ``/configs/.auth_token`` — the Bearer
        FILE PATH (never the token VALUE — the file is restart-rotated, so the child
        re-reads it and never 401s on a stale snapshot). AUTHORITATIVE (injector-
        wins): a descriptor can NOT repoint the self server's Bearer at a different
        file (review: security). Forced to the in-container SSOT, exactly like the
        mode.
      * ``MOLECULE_MCP_MODE=self`` — the workspace-token registry. AUTHORITATIVE: a
        descriptor can never downgrade a self surface to ``management`` to fish for
        the org registry.
      * ``WORKSPACE_ID`` / ``MOLECULE_WORKSPACE_ID`` — the workspace's own id, the
        only names the self server's ``selfWorkspaceId()`` reads, so
        create_schedule's self-default resolves the own id. Descriptor-declared wins
        (not a credential — the token file is the authority and the server scopes to
        the token's own id, 401ing a foreign one).
      * ``MOLECULE_API_URL`` — the tenant API base URL (from ``MOLECULE_API_URL``,
        its ``MOLECULE_CP_URL`` alias, or ``PLATFORM_URL``) so the child does not
        fall back to localhost. Descriptor-declared wins; a container carrying NONE
        is a logged misconfig, not a silent omission.

    NEVER injects the org key or any ``PRIVILEGED_ENV_KEYS`` credential — the self
    surface is the non-privileged workspace-token bearer, scoped to its own ``:id``
    by construction (a foreign id 401s server-side).
    """
    descriptor_env = dict(spec.get("env") or {})
    # AUTHORITATIVE (injector-wins) credential-critical keys — a descriptor can NEVER
    # override these. MOLECULE_MCP_MODE=self IS the self registry; the token-file is
    # the Bearer source, so a descriptor-overridable path would let a manifest point
    # the self server's Bearer at a DIFFERENT file (review: security).
    injected: dict[str, str] = {
        "MOLECULE_MCP_MODE": SELF_MCP_MODE,
        WORKSPACE_TOKEN_FILE_ENV: WORKSPACE_TOKEN_FILE,
    }
    # The workspace's OWN id — descriptor-declared wins (not a credential). Sourced
    # from the container env like the org branch; injected under both read-names.
    wsid = None
    for src in _SELF_WORKSPACE_ID_SOURCE_KEYS:
        val = env.get(src)
        if val:
            wsid = val
            break
    if wsid:
        for key in _SELF_WORKSPACE_ID_ENVS:
            if key not in descriptor_env:
                injected[key] = wsid
    # The tenant API base URL — descriptor-declared wins; else resolve the best
    # available source. If the container carries NONE, that is a real workspace
    # misconfig: log it loudly rather than silently omit and let the child fall back
    # to an unreachable localhost (review: reachability).
    if _SELF_API_URL_ENV not in descriptor_env:
        for src in _SELF_API_URL_SOURCE_KEYS:
            val = env.get(src)
            if val:
                injected[_SELF_API_URL_ENV] = val
                break
        else:
            log.warning(
                "inject_privileged_env: self-audience MCP %r has NO tenant API URL "
                "in the container env (%s all unset) — the self server would fall "
                "back to localhost and be unreachable. Workspace misconfig; set "
                "MOLECULE_API_URL.",
                name, "/".join(_SELF_API_URL_SOURCE_KEYS),
            )

    new_spec = dict(spec)
    descriptor_env.update(injected)
    new_spec["env"] = descriptor_env
    log.info(
        "inject_privileged_env: wired self-audience MCP %r "
        "(workspace-token-file[forced] + MOLECULE_MCP_MODE=self%s%s)",
        name,
        ", workspace-id" if any(k in injected for k in _SELF_WORKSPACE_ID_ENVS) else "",
        ", api-url" if _SELF_API_URL_ENV in injected else "",
    )
    return new_spec
