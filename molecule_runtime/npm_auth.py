"""Gitea npm-registry auth installer (npm companion to credential_helper.py).

Background — fleet-wide concierge degrade (RCA 2026-06-24)
=========================================================

The concierge's management MCP runs as ``npx @molecule-ai/mcp-server@<ver>`` —
an **npm** fetch from the PRIVATE gitea npm registry. The box already
authenticates its **git** fetches (the boot-install of the ``gitea://`` plugin
repo) via ``GIT_HTTP_USERNAME``/``GIT_HTTP_PASSWORD`` (see
``workspace-server`` ``conciergePlatformMCPEnv`` + ``setup-gitea-netrc``). But
npm/npx had **no registry auth**, so it only ever saw the *unauthenticated*
view of the private package — ``npm view`` returned just ``1.0.0`` and
``npx @molecule-ai/mcp-server@1.6.1`` failed ``ETARGET`` → the MCP server never
started → ``create_workspace`` never loaded → every concierge sat ``degraded``
(``mcp_server_present=true`` but the required tool absent). Proven: supply a
read:package token and the same ``npx`` starts ("Molecule AI MCP server running
on stdio (96 tools available)").

This module writes ``~/.npmrc`` with the gitea ``@molecule-ai`` registry and an
``_authToken`` taken from the **same** gitea read token the git auth already
uses (SSOT — no new credential). It mirrors :func:`install_credential_helper`:
called once early in startup, fail-soft, idempotent, additive (it preserves any
unrelated ``.npmrc`` lines). Generic by construction — any ``npm``/``npx`` in
the container that fetches a private ``@molecule-ai`` package now authenticates,
not just the concierge MCP.

Registry resolution (single deriver, SSOT with git — audit finding C1)
======================================================================
The npm registry URL resolves via :func:`resolve_npm_registry`: an explicit
``MOLECULE_GITEA_NPM_REGISTRY`` full URL wins; else it is DERIVED from the
forge base host (:func:`resolve_gitea_base` — ``MOLECULE_PLUGIN_REGISTRY`` →
``MOLECULE_GITEA_BASE_URL`` → the documented default) plus the canonical npm
path suffix. Before C1 this module hardcoded the registry host and never read
the base-URL env, so entrypoints that SET ``MOLECULE_GITEA_BASE_URL`` were
silently ignored here while git (``plugin_sources``) honored it — a
registry-host migration would have left npm on the stale host. The base
resolver now lives here (the lower module) and ``plugin_sources`` imports it,
so the forge host is spelled in exactly one place for both git and npm.

Token source (precedence): canonical ``MOLECULE_TEMPLATE_REPO_TOKEN`` →
``GITEA_TOKEN`` → the gitea HTTPS-auth pair. The HTTPS-auth pair has two shapes:
the normal basic-auth case carries the token in ``GIT_HTTP_PASSWORD``; the
**verified live concierge** case (workspace-server ``conciergePlatformMCPEnv``)
carries the PAT in ``GIT_HTTP_USERNAME`` with ``GIT_HTTP_PASSWORD`` set to the
literal ``x-oauth-basic`` sentinel. We resolve both, and NEVER write the literal
``x-oauth-basic`` as the ``_authToken`` secret.

NOTE (credential prerequisite): the gitea token MUST carry ``read:package``
scope. The token used for git fetches (``MOLECULE_TEMPLATE_REPO_TOKEN``) is
``read:repository`` only by default; for the SSOT to hold it must be widened to
``read:repository,read:package`` so the one token serves both git and npm.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger(__name__)

_SCOPE = "@molecule-ai"

# ---------------------------------------------------------------------------
# Gitea forge base-host resolution (SSOT for BOTH git and npm).
#
# The box configures ONE forge host. git fetches (plugin_sources) and npm
# fetches (this module) must resolve it identically — a divergence is the exact
# drift audit finding C1 flagged: entrypoints SET MOLECULE_GITEA_BASE_URL but
# npm_auth NEVER read it, so on a registry-host migration the npm helper kept
# using a stale hardcoded host while git honored the override.
#
# This module owns the resolver because it is the lower layer (plugin_sources
# already imports gitea_read_token from here); plugin_sources imports these back
# so the constants live in exactly one place, not two.
# ---------------------------------------------------------------------------
# Env vars that carry the forge base host, in precedence order.
# MOLECULE_PLUGIN_REGISTRY is the provider-agnostic canonical name core SETS on
# the box (conciergePlatformMCPEnv) — the knob a self-host/mirror/airgap uses to
# point sourcing at a different forge. MOLECULE_GITEA_BASE_URL is the shell
# mirror's back-compat alias for the same host.
_REGISTRY_ENV = "MOLECULE_PLUGIN_REGISTRY"
_BASE_URL_ENV = "MOLECULE_GITEA_BASE_URL"
_BASE_ENV_PRECEDENCE = (_REGISTRY_ENV, _BASE_URL_ENV)

# Documented back-compat default forge base host, used only when neither base
# env var is set (emitted NON-SILENTLY by resolve_gitea_base so the reliance is
# observable, matching the shell's ``${MOLECULE_GITEA_BASE_URL:-<this>}``). This
# is the ONE literal for the canonical prod host in the runtime.
_BACKCOMPAT_GITEA_BASE = "https://git.moleculesai.app"

# The npm-registry path suffix appended to the forge base host to form the
# ``@molecule-ai`` npm registry URL (``<base>/api/packages/molecule-ai/npm/``).
# Centralized here so the org/path segment is spelled exactly once.
_NPM_REGISTRY_PATH = "/api/packages/molecule-ai/npm/"

# Explicit full-URL npm-registry override. When set it wins outright (no base
# composition) — an operator pointing npm at an arbitrary registry URL. Nothing
# in the fleet sets it today; kept as the explicit escape hatch.
_NPM_REGISTRY_ENV = "MOLECULE_GITEA_NPM_REGISTRY"


def resolve_gitea_base(env: Mapping[str, str] | None = None) -> str:
    """Return the configured gitea forge base host (no trailing slash guarantee).

    Resolution order: ``MOLECULE_PLUGIN_REGISTRY`` → ``MOLECULE_GITEA_BASE_URL``
    → the documented back-compat default (LOGged non-silently when used). This
    is the SSOT both git (plugin_sources) and npm (this module) resolve from, so
    a forge-host migration flips one env var and both follow.
    """
    if env is None:
        env = os.environ
    for name in _BASE_ENV_PRECEDENCE:
        configured = (env.get(name) or "").strip()
        if configured:
            return configured
    log.info(
        "npm_auth: neither %s nor %s set — using documented back-compat gitea "
        "base %s (set %s to silence)",
        _REGISTRY_ENV, _BASE_URL_ENV, _BACKCOMPAT_GITEA_BASE, _REGISTRY_ENV,
    )
    return _BACKCOMPAT_GITEA_BASE


def resolve_npm_registry(env: Mapping[str, str] | None = None) -> str:
    """Return the ``@molecule-ai`` npm-registry URL (always trailing-slashed).

    Precedence:
      1. explicit ``MOLECULE_GITEA_NPM_REGISTRY`` full URL — wins if set;
      2. else derive from the resolved forge base host (``resolve_gitea_base``:
         ``MOLECULE_PLUGIN_REGISTRY`` → ``MOLECULE_GITEA_BASE_URL`` → default)
         plus the canonical npm path suffix — THIS is what makes a set-but-unread
         base-URL override actually take effect (audit finding C1, runtime-side);
      3. the back-compat default host is the last-resort fallback, sourced from
         the one ``resolve_gitea_base`` literal — not re-spelled here.

    Trailing slashes are normalized when composing so ``<base>`` with or without
    a trailing ``/`` yields exactly one separating slash.
    """
    if env is None:
        env = os.environ
    explicit = (env.get(_NPM_REGISTRY_ENV) or "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else explicit + "/"
    base = resolve_gitea_base(env).rstrip("/")
    return base + _NPM_REGISTRY_PATH

# Canonical gitea token env vars, in precedence order.
# MOLECULE_TEMPLATE_REPO_TOKEN is the read token the box holds for fetching
# template/plugin repos (and, once widened with read:package, packages too);
# GITEA_TOKEN is its alias. These take precedence over the gitea HTTPS-auth pair.
_CANONICAL_TOKEN_ENV_PRECEDENCE = ("MOLECULE_TEMPLATE_REPO_TOKEN", "GITEA_TOKEN")

# Sentinel value core's concierge stores in GIT_HTTP_PASSWORD when the real PAT
# lives in GIT_HTTP_USERNAME (the verified live concierge shape: workspace-server
# conciergePlatformMCPEnv sets GIT_HTTP_USERNAME=<PAT>, GIT_HTTP_PASSWORD=
# "x-oauth-basic"). We must NEVER write this literal as the _authToken secret.
_OAUTH_BASIC_SENTINEL = "x-oauth-basic"


def gitea_read_token(env: Mapping[str, str] | None = None) -> str:
    """Return the gitea read token, or "" if none is present.

    SSOT: the SAME token the box uses for git fetches (git-native plugin_sources
    clones consume this exact resolver too — one credential, one resolution).
    Must carry read:package scope for npm fetches to succeed (see docstring).

    ``env`` defaults to ``os.environ``; callers that resolve from an explicit
    mapping (e.g. the boot-install's ``env=`` parameter) pass it through.

    Resolution order (token-source precedence):
      1. MOLECULE_TEMPLATE_REPO_TOKEN (canonical, highest precedence)
      2. GITEA_TOKEN (its alias)
      3. GIT_HTTP_PASSWORD when set and != the x-oauth-basic sentinel
         (the normal basic-auth password-as-token shape, e.g. credential_helper's
         name+password model where the secret lives in the password field)
      4. GIT_HTTP_USERNAME when GIT_HTTP_PASSWORD == the x-oauth-basic sentinel
         (the VERIFIED live concierge shape: PAT carried in the username field,
         the password field holding only the literal sentinel)

    The literal x-oauth-basic sentinel is never returned as a token: it merely
    routes us to read the PAT from GIT_HTTP_USERNAME instead.
    """
    if env is None:
        env = os.environ
    for var in _CANONICAL_TOKEN_ENV_PRECEDENCE:
        v = (env.get(var) or "").strip()
        if v:
            return v

    http_password = (env.get("GIT_HTTP_PASSWORD") or "").strip()
    http_username = (env.get("GIT_HTTP_USERNAME") or "").strip()
    if http_password and http_password.lower() != _OAUTH_BASIC_SENTINEL:
        # Normal basic-auth shape: the secret/token is the password.
        return http_password
    if http_password.lower() == _OAUTH_BASIC_SENTINEL and http_username:
        # Verified live concierge shape: the PAT is the username, the password
        # is only the sentinel. Use the username as the token; never the sentinel.
        return http_username
    return ""


# Back-compat private alias (pre-existing internal name).
_gitea_read_token = gitea_read_token


def _auth_key(registry: str) -> str | None:
    """Derive npm's per-registry auth config key from a registry URL.

    npm keys per-registry auth as ``//<host>/<path>/:_authToken`` — i.e. the
    registry URL with the scheme stripped, a leading ``//``, and a trailing
    slash. Returns None if the registry has no scheme (caller skips).
    """
    if "://" not in registry:
        return None
    path = registry.split("://", 1)[1]
    return "//" + path.rstrip("/") + "/"


def install_npm_gitea_auth() -> None:
    """Write ~/.npmrc so npm/npx can fetch private ``@molecule-ai`` packages.

    Safe to call multiple times (idempotent). No-op when no gitea token is
    present (non-concierge workspaces, pure-local dev). Fail-soft: a write error
    logs a warning and returns — the runtime starting matters more than npm
    auth being perfect, and the loud RCA#2970/#3082 gates surface a still-broken
    MCP downstream.
    """
    token = gitea_read_token()
    if not token:
        log.info(
            "npm_auth: no gitea token present (%s/GIT_HTTP_PASSWORD/GIT_HTTP_USERNAME)"
            " — skipping npm registry auth",
            "/".join(_CANONICAL_TOKEN_ENV_PRECEDENCE),
        )
        return

    # Single deriver (explicit full-URL override → base-host derivation → default).
    # resolve_npm_registry already normalizes the trailing slash.
    registry = resolve_npm_registry()
    key = _auth_key(registry)
    if key is None:
        log.warning("npm_auth: %s=%r has no scheme — skipping", _NPM_REGISTRY_ENV, registry)
        return

    registry_line = f"{_SCOPE}:registry={registry}"
    auth_line = f"{key}:_authToken={token}"

    npmrc = Path(os.environ.get("HOME", "/home/agent")) / ".npmrc"
    try:
        existing = npmrc.read_text().splitlines() if npmrc.exists() else []
        # Additive + idempotent: drop our own prior lines (so a token rotation
        # replaces cleanly, no duplicates) and keep every unrelated line.
        keep = [
            ln for ln in existing
            if not ln.startswith(f"{_SCOPE}:registry=")
            and not ln.startswith(f"{key}:_authToken=")
        ]
        content = "\n".join(keep + [registry_line, auth_line]) + "\n"
        # Create 0600 from the start so the token never sits at a world-readable
        # default mode (no write-then-chmod TOCTOU window); O_TRUNC rewrites an
        # existing file. Then chmod to ALSO tighten a pre-existing file, whose
        # mode O_CREAT would not change.
        fd = os.open(npmrc, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        try:
            os.chmod(npmrc, 0o600)  # token at rest — restrict like .netrc
        except OSError as exc:
            # A failed hardening of a secret file should be observable, not silent.
            log.warning("npm_auth: could not chmod %s to 0600 (%s) — token left at default perms", npmrc, exc)
        # SECURITY: never log the token value — only its length.
        log.info(
            "npm_auth: wrote %s with gitea %s registry + _authToken (token len=%d)",
            npmrc, _SCOPE, len(token),
        )
    except OSError as exc:
        log.warning(
            "npm_auth: could not write %s (%s) — npx of private %s packages may fail",
            npmrc, exc, _SCOPE,
        )
