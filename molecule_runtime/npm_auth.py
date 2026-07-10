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

# The gitea npm registry for the ``@molecule-ai`` scope. Overridable via env so
# the host is not hardcoded in two places (the molecule-platform plugin's
# settings-fragment also references this registry); the default is the canonical
# prod registry. SSOT-friendly: set MOLECULE_GITEA_NPM_REGISTRY to override.
_DEFAULT_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
_SCOPE = "@molecule-ai"

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

    registry = (os.environ.get("MOLECULE_GITEA_NPM_REGISTRY") or _DEFAULT_REGISTRY).strip()
    key = _auth_key(registry)
    if key is None:
        log.warning("npm_auth: MOLECULE_GITEA_NPM_REGISTRY=%r has no scheme — skipping", registry)
        return

    if not registry.endswith("/"):
        registry += "/"
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
