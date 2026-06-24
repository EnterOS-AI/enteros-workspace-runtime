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

NOTE (credential prerequisite): the gitea token MUST carry ``read:package``
scope. The token used for git fetches (``MOLECULE_TEMPLATE_REPO_TOKEN``) is
``read:repository`` only by default; for the SSOT to hold it must be widened to
``read:repository,read:package`` so the one token serves both git and npm.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# The gitea npm registry for the ``@molecule-ai`` scope. Overridable via env so
# the host is not hardcoded in two places (the molecule-platform plugin's
# settings-fragment also references this registry); the default is the canonical
# prod registry. SSOT-friendly: set MOLECULE_GITEA_NPM_REGISTRY to override.
_DEFAULT_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
_SCOPE = "@molecule-ai"

# Token env precedence. MOLECULE_TEMPLATE_REPO_TOKEN is the canonical gitea read
# token the box holds for fetching template/plugin repos (and, once widened with
# read:package, packages too). GITEA_TOKEN / GIT_HTTP_PASSWORD are the aliases
# credential_helper recognises for the Gitea flow. GIT_HTTP_USERNAME is only a
# username placeholder; the actual secret lives in GIT_HTTP_PASSWORD.
_TOKEN_ENV_PRECEDENCE = ("MOLECULE_TEMPLATE_REPO_TOKEN", "GITEA_TOKEN", "GIT_HTTP_PASSWORD")


def _gitea_read_token() -> str:
    """Return the gitea read token, or "" if none is present.

    SSOT: the SAME token the box uses for git fetches. Must carry read:package
    scope for npm fetches to succeed (see module docstring).
    """
    for var in _TOKEN_ENV_PRECEDENCE:
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    return ""


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
    token = _gitea_read_token()
    if not token:
        log.info(
            "npm_auth: no gitea token present (%s) — skipping npm registry auth",
            "/".join(_TOKEN_ENV_PRECEDENCE),
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
        npmrc.write_text("\n".join(keep + [registry_line, auth_line]) + "\n")
        try:
            npmrc.chmod(0o600)  # token at rest — restrict like .netrc
        except OSError:
            pass
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
