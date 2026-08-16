"""npm ``@molecule-ai`` scope-registry config (companion to credential_helper.py).

Why this exists (PLATFORM CONTRACT, hard)
=========================================

The concierge's management MCP runs as ``npx @molecule-ai/mcp-server@<ver>``.
Every runtime container MUST be able to resolve the ``@molecule-ai`` npm scope
or the MCP never starts, ``create_workspace`` never loads, and the platform
FAIL-CLOSES the workspace (core#3082 ``loaded_mcp_tools`` gate). So this module
writes the ``@molecule-ai:registry`` line into ``~/.npmrc`` UNCONDITIONALLY.

Anonymous is the floor; auth is additive-only
=============================================

Proven live 2026-07-09: the Gitea npm registry serves the ``@molecule-ai``
scope FULLY ANONYMOUSLY — anonymous packument HTTP 200 AND ``npm pack
@molecule-ai/mcp-server`` fetches the full tarball with a scope-line-only
``.npmrc`` (no token). So NO token is required for the common case.

Critically, a token WITHOUT ``read:package`` is REJECTED with HTTP 401, whereas
NO token falls through to anonymous 200 — **a mis-scoped token is strictly
WORSE than no token** (it turns working anonymous access into a hard 401). The
git-transport credential the concierge carries (``GIT_HTTP_USERNAME`` +
``GIT_HTTP_PASSWORD=x-oauth-basic``, derived by core from a ``read:repository``
PAT for the ``gitea://`` plugin git-clone — see ``workspace-server``
``conciergePlatformMCPEnv`` + ``setup-gitea-netrc``) is exactly such a token:
writing it as the npm ``_authToken`` 401-poisoned the registry and fail-closed
the hermes concierge. So the npm ``_authToken`` is attached ONLY from a var
explicitly DESIGNATED as a package token — ``MOLECULE_NPM_TOKEN`` alone (see
``_PACKAGE_TOKEN_ENV_PRECEDENCE`` / :func:`_npm_package_token`) — never inferred
from a git-transport credential.

**2026-08-15: the guard had a hole and this is the patch.** The precedence list
also carried ``MOLECULE_TEMPLATE_REPO_TOKEN`` and ``GITEA_TOKEN``, described as
"intended read:package". Measured on prod, the former is a ``read:repository``
token: repo API 200, packages API **401**, whoami **403**, anonymous **200**.
So the module re-admitted, under a friendlier name, exactly the class of
credential its own git-transport exclusion exists to keep out — and every
not-pre-baked MCP plugin fail-closed at launch as a result
(``molecule-ai-plugin-image-gen#2``). Designation must be judged on the token's
CAPABILITY, never on how the variable is spelled.

npm token vs git token — one forge, two protocols (SSOT boundary)
=================================================================
This module ALSO owns the shared forge-host + token resolvers that ``git`` uses
(``plugin_sources`` imports :func:`resolve_gitea_base` and
:func:`gitea_read_token` — audit finding C1: one host, one credential
resolution for BOTH git and npm). BUT the two protocols have DIFFERENT scope
needs from that one forge:

  * git (private plugin-repo clone) legitimately consumes a ``read:repository``
    credential, INCLUDING the git-transport ``GIT_HTTP_*`` pair (a private
    repo's 401 triggers git's credential helper, which supplies
    :func:`gitea_read_token`). So :func:`gitea_read_token` KEEPS the git-http
    path — dropping it would break private plugin clones on a concierge box that
    carries only the git-transport credential.
  * npm needs a ``read:package`` token and 401-poisons on a ``read:repository``
    one. So npm does NOT reuse :func:`gitea_read_token`; it uses the strictly
    designated-package-token resolver :func:`_npm_package_token`, which NEVER
    consults the git-http pair.

Same forge host (``resolve_npm_registry`` derives from ``resolve_gitea_base``),
DIFFERENT token — the split is the reconciliation of the registry-host SSOT
(finding C1) with the npm 401-poison guard.

Registry resolution (single deriver, SSOT with git — audit finding C1)
======================================================================
The npm registry URL resolves via :func:`resolve_npm_registry`: the
provider-neutral ``MOLECULE_NPM_REGISTRY`` full URL wins outright, then the
legacy ``MOLECULE_GITEA_NPM_REGISTRY`` alias, else it is DERIVED from the forge
base host (:func:`resolve_gitea_base` — ``MOLECULE_PLUGIN_REGISTRY`` →
``MOLECULE_GITEA_BASE_URL`` → the documented default) plus the canonical npm
path suffix. Before C1 this module hardcoded the registry host and never read
the base-URL env, so entrypoints that SET ``MOLECULE_GITEA_BASE_URL`` were
silently ignored here while git (``plugin_sources``) honored it. The base
resolver lives here (the lower module) and ``plugin_sources`` imports it, so the
forge host is spelled in exactly one place for both git and npm. The contract
is "the ``@molecule-ai`` scope resolves", not "it resolves from Gitea" —
``MOLECULE_NPM_REGISTRY`` is the provider-neutral escape hatch (GitHub Packages,
a mirror, an artifact proxy, ...).

Called at boot (main.py step 0.2b) AND re-asserted after ``adapter.setup()`` —
template setup steps that install their own node stacks (hermes) clobbered the
boot write. Writes to BOTH ``$HOME`` and the canonical agent home (HOME-split).
Fail-soft, idempotent, additive.
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

# Provider-neutral full-URL npm-registry override. When set it wins outright (no
# base composition) — the contract is "the @molecule-ai scope resolves", not
# "it resolves from Gitea" (GitHub Packages, a mirror, an artifact proxy, ...).
_PROVIDER_NEUTRAL_NPM_REGISTRY_ENV = "MOLECULE_NPM_REGISTRY"

# Legacy explicit full-URL npm-registry override (Gitea-flavoured name). Kept as
# a back-compat alias behind the provider-neutral override above.
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
      1. provider-neutral ``MOLECULE_NPM_REGISTRY`` full URL — wins outright
         (the contract is "the scope resolves", not "it resolves from Gitea");
      2. legacy ``MOLECULE_GITEA_NPM_REGISTRY`` full URL — back-compat alias;
      3. else derive from the resolved forge base host (``resolve_gitea_base``:
         ``MOLECULE_PLUGIN_REGISTRY`` → ``MOLECULE_GITEA_BASE_URL`` → default)
         plus the canonical npm path suffix — THIS is what makes a set-but-unread
         base-URL override actually take effect (audit finding C1, runtime-side);
      4. the back-compat default host is the last-resort fallback, sourced from
         the one ``resolve_gitea_base`` literal — not re-spelled here.

    Trailing slashes are normalized when composing so ``<base>`` with or without
    a trailing ``/`` yields exactly one separating slash.
    """
    if env is None:
        env = os.environ
    for name in (_PROVIDER_NEUTRAL_NPM_REGISTRY_ENV, _NPM_REGISTRY_ENV):
        explicit = (env.get(name) or "").strip()
        if explicit:
            return explicit if explicit.endswith("/") else explicit + "/"
    base = resolve_gitea_base(env).rstrip("/")
    return base + _NPM_REGISTRY_PATH


# ---------------------------------------------------------------------------
# Token resolution — TWO resolvers for ONE forge, because git and npm need
# DIFFERENT scopes from the same host (see module docstring).
# ---------------------------------------------------------------------------
# Canonical gitea token env vars, in precedence order. GIT ONLY.
#
# MOLECULE_TEMPLATE_REPO_TOKEN is the read token the box holds for fetching
# template/plugin repos; GITEA_TOKEN is its alias. These take precedence over
# the gitea HTTPS-auth pair.
#
# This comment used to add "(and, once widened with read:package, packages
# too)". That is false in practice and the aspiration is what caused the bug
# below: measured on prod 2026-08-15 the live token returns 200 on the repo API
# and **401 on the packages API**, so it was never widened, and treating it as
# though it might be is precisely the spelling-over-capability error the npm
# resolver now refuses to make. If a package token is ever wanted, it gets its
# own var — see _PACKAGE_TOKEN_ENV_PRECEDENCE.
_CANONICAL_TOKEN_ENV_PRECEDENCE = ("MOLECULE_TEMPLATE_REPO_TOKEN", "GITEA_TOKEN")

# npm PACKAGE-token env vars, in precedence order. ONLY vars whose token is
# DESIGNATED to carry ``read:package`` scope — NEVER inferred from a
# git-transport credential.
#
# WHY NOT the git-http pair (GIT_HTTP_USERNAME/PASSWORD/x-oauth-basic) for npm:
# proven 2026-07-09 against the live Gitea npm registry, a token WITHOUT
# read:package is REJECTED with HTTP 401, whereas NO token falls through to
# ANONYMOUS access (HTTP 200) — the registry serves the @molecule-ai scope
# (packument AND tarball) anonymously. The git-http PAT is a repo-transport
# credential (read:repository), so writing it as the npm ``_authToken`` turns
# working anonymous access into a hard 401. **A mis-scoped token is strictly
# worse than no token.** So npm auth is ADDITIVE-ONLY over the anonymous floor:
# it attaches an ``_authToken`` ONLY from a var explicitly designated as a
# package token. MOLECULE_NPM_TOKEN is the purpose-specific override; the
# canonical MOLECULE_TEMPLATE_REPO_TOKEN / GITEA_TOKEN are kept because they are
# *intended* to be widened to read:package (see module docstring's credential
# prerequisite), but the git-transport shapes are dropped for npm entirely.
#
# NOTE: this is SEPARATE from _CANONICAL_TOKEN_ENV_PRECEDENCE / gitea_read_token
# below, which git (plugin_sources) uses and which DELIBERATELY keeps the
# git-http path — git legitimately needs the read:repository transport cred.
_PACKAGE_TOKEN_ENV_PRECEDENCE = ("MOLECULE_NPM_TOKEN",)
# ONE var, and it is the only one whose NAME designates a package token.
#
# This tuple used to also carry MOLECULE_TEMPLATE_REPO_TOKEN ("canonical;
# intended read:package") and GITEA_TOKEN ("its alias"). Measured on prod
# 2026-08-15, MOLECULE_TEMPLATE_REPO_TOKEN is NOT a package token:
#
#   GET /api/v1/repos/molecule-ai/<repo>        -> 200   (read:repository — its real scope)
#   GET /api/packages/molecule-ai/npm/<pkg>     -> 401
#   GET /api/v1/user            (whoami)        -> 403   (cannot even identify itself)
#   the same GET with NO Authorization header   -> 200   (anonymous floor)
#
# So writing it as the npm _authToken did the precise thing this module's
# header warns about — "a mis-scoped token is strictly WORSE than no token" —
# turning a working anonymous fetch into a hard 401. Live effect: every
# not-pre-baked MCP plugin fail-closed on launch. `npx @molecule-ai/
# mcp-image-gen` E401'd on a package the same box fetches fine anonymously,
# the MCP never started, and the agent was told only that its tools were
# "unavailable this turn" (molecule-ai-plugin-image-gen#2).
#
# The management MCP survived solely because it is pre-baked into the image and
# resolves offline — it never presents the credential. That is insulation, not
# correctness: any cache miss puts it on the same path.
#
# The guard was already here in spirit. It excluded the git-transport
# GIT_HTTP_* pair for exactly this reason, then re-admitted the same class of
# credential under a friendlier name. The lesson is that the check has to be on
# the token's CAPABILITY, not on how the variable is spelled — so this tuple now
# holds only the var that exists for no other purpose.
#
# If a genuinely private @molecule-ai package ever needs auth, set
# MOLECULE_NPM_TOKEN to a token minted with read:package. Do NOT re-add a
# general-purpose forge token here; anonymous is the documented floor and it
# beats a 401 every time.

# Sentinel value core's concierge stores in GIT_HTTP_PASSWORD when the real PAT
# lives in GIT_HTTP_USERNAME (the verified live concierge shape: workspace-server
# conciergePlatformMCPEnv sets GIT_HTTP_USERNAME=<PAT>, GIT_HTTP_PASSWORD=
# "x-oauth-basic"). We must NEVER write this literal as the _authToken secret.
_OAUTH_BASIC_SENTINEL = "x-oauth-basic"


def gitea_read_token(env: Mapping[str, str] | None = None) -> str:
    """Return the gitea read token for GIT fetches, or "" if none is present.

    SSOT for GIT: the SAME token the box uses for git fetches (git-native
    plugin_sources clones consume this exact resolver — one credential, one
    resolution). A private plugin repo's 401 triggers git's credential helper,
    which supplies this token, so the git-transport ``GIT_HTTP_*`` pair is a
    LEGITIMATE source here (a ``read:repository`` cred is exactly what a git
    clone needs).

    ``env`` defaults to ``os.environ``; callers that resolve from an explicit
    mapping (e.g. the boot-install's ``env=`` parameter) pass it through.

    Resolution order (token-source precedence):
      1. MOLECULE_TEMPLATE_REPO_TOKEN (canonical, highest precedence)
      2. GITEA_TOKEN (its alias)
      3. GIT_HTTP_PASSWORD when set and != the x-oauth-basic sentinel
         (the normal basic-auth password-as-token shape)
      4. GIT_HTTP_USERNAME when GIT_HTTP_PASSWORD == the x-oauth-basic sentinel
         (the VERIFIED live concierge shape: PAT carried in the username field,
         the password field holding only the literal sentinel)

    The literal x-oauth-basic sentinel is never returned as a token: it merely
    routes us to read the PAT from GIT_HTTP_USERNAME instead.

    IMPORTANT: npm does NOT use this resolver — the git-transport cred is
    ``read:repository`` and 401-poisons the npm registry. npm uses the strictly
    designated-package-token resolver :func:`_npm_package_token`.
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


def _npm_package_token(env: Mapping[str, str] | None = None) -> str:
    """Return a DESIGNATED npm package token, or "" for anonymous access.

    Resolution: MOLECULE_NPM_TOKEN only — the one variable that exists for no
    purpose other than package access.

    Nothing else is consulted, and that is the whole point. Git-transport
    credentials (GIT_HTTP_USERNAME/GIT_HTTP_PASSWORD, incl. the x-oauth-basic
    concierge shape) were always excluded because they are repo-scoped and 401
    the package registry. MOLECULE_TEMPLATE_REPO_TOKEN and GITEA_TOKEN were
    removed 2026-08-15 for the same measured reason — see
    _PACKAGE_TOKEN_ENV_PRECEDENCE for the HTTP codes.

    Returns "" when no designated package token is set. The caller then writes
    the scope line only, which the registry serves anonymously — and anonymous
    is strictly better than a token the registry rejects.
    """
    if env is None:
        env = os.environ
    for var in _PACKAGE_TOKEN_ENV_PRECEDENCE:
        v = (env.get(var) or "").strip()
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


# The canonical agent home. Several runtimes split users between the runtime
# main (often PID1 as root, HOME=/root) and the process that actually spawns
# the management MCP (the agent user, HOME=/home/agent) — hermes proved this
# live 2026-07-09: main.py wrote /root/.npmrc while the MCP's npx resolved
# against a different home, and hermes's own node install clobbered the boot
# write anyway. Writing the scope config to BOTH homes closes the HOME-split
# class. Tests monkeypatch this constant.
_AGENT_HOME = Path("/home/agent")


def _npmrc_targets() -> list[Path]:
    """The npmrc locations to configure: $HOME plus the canonical agent home."""
    targets = [Path(os.environ.get("HOME", str(_AGENT_HOME))) / ".npmrc"]
    agent_npmrc = _AGENT_HOME / ".npmrc"
    if _AGENT_HOME.is_dir() and agent_npmrc not in targets:
        targets.append(agent_npmrc)
    return targets


def _write_npmrc(npmrc: Path, key: str, registry_line: str, auth_line: "str | None") -> None:
    """Additive + idempotent write of our scope (and optional auth) lines."""
    existing = npmrc.read_text().splitlines() if npmrc.exists() else []
    # Drop our own prior lines (so a token rotation replaces cleanly, no
    # duplicates) and keep every unrelated line.
    keep = [
        ln for ln in existing
        if not ln.startswith(f"{_SCOPE}:registry=")
        and not ln.startswith(f"{key}:_authToken=")
    ]
    ours = [registry_line] + ([auth_line] if auth_line else [])
    content = "\n".join(keep + ours) + "\n"
    # Create 0600 from the start so a token never sits at a world-readable
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
        log.warning("npm_auth: could not chmod %s to 0600 (%s)", npmrc, exc)
    # When root writes into another user's home (the HOME-split case), a
    # root-owned 0600 file would be UNREADABLE by that user's npm — chown to the
    # home directory's owner. Only root can chown to an arbitrary owner, so this
    # is a no-op (and would EPERM) when we ourselves are the non-root agent
    # re-asserting our own npmrc — guard on being root to avoid the noise.
    if os.getuid() == 0:
        try:
            st = os.stat(npmrc.parent)
            if (st.st_uid, st.st_gid) != (0, 0):
                os.chown(npmrc, st.st_uid, st.st_gid)
        except OSError as exc:
            log.warning("npm_auth: could not chown %s to home owner (%s)", npmrc, exc)


def install_npm_gitea_auth() -> None:
    """Configure npm so ``npx @molecule-ai/...`` resolves from the gitea registry.

    PLATFORM CONTRACT (HARD, not optional): every runtime container must be able
    to resolve the ``@molecule-ai`` scope — the concierge's management MCP is
    ``npx @molecule-ai/mcp-server`` and the core#3082 ``loaded_mcp_tools`` gate
    fail-closes the workspace when it cannot start. The gitea registry serves
    the scope ANONYMOUSLY (D3 ruling, 2026-07-07), so the scope-registry line is
    written UNCONDITIONALLY — a missing token no longer skips it (the
    pre-2026-07-09 token-coupled skip is exactly what fail-closed the tokenless
    self-host hermes concierge). The ``_authToken`` line is added only when a
    DESIGNATED package token is present (:func:`_npm_package_token`); a
    git-transport credential is NEVER written (it 401-poisons — see module
    docstring).

    Safe to call multiple times (idempotent, additive). Called at boot (main.py
    step 0.2b) AND re-asserted after ``adapter.setup()`` — template setup steps
    that install their own node stacks (hermes) clobbered the boot write. Writes
    to BOTH ``$HOME`` and the canonical agent home (HOME-split). Fail-soft per
    target: a write error logs a warning — the runtime starting matters more,
    and the loud RCA#2970/#3082 gates surface a still-broken MCP downstream.
    """
    token = _npm_package_token()

    # Single deriver: provider-neutral override → legacy alias → base-host
    # derivation → default. resolve_npm_registry already normalizes the trailing
    # slash.
    registry = resolve_npm_registry()
    key = _auth_key(registry)
    if key is None:
        # Don't name a single env var — resolve_npm_registry() may have taken the
        # value from the provider-neutral MOLECULE_NPM_REGISTRY, the legacy
        # MOLECULE_GITEA_NPM_REGISTRY alias, or forge-host derivation. Naming only
        # one would misdirect an operator who set a different one (review [3]).
        log.warning(
            "npm_auth: resolved registry %r has no scheme — skipping "
            "(check %s / %s)",
            registry, _PROVIDER_NEUTRAL_NPM_REGISTRY_ENV, _NPM_REGISTRY_ENV,
        )
        return

    registry_line = f"{_SCOPE}:registry={registry}"
    auth_line = f"{key}:_authToken={token}" if token else None
    if not token:
        log.info(
            "npm_auth: no designated package token present — writing anonymous %s"
            " scope registry (the registry serves the scope without auth)",
            _SCOPE,
        )

    for npmrc in _npmrc_targets():
        try:
            _write_npmrc(npmrc, key, registry_line, auth_line)
            # SECURITY: never log the token value — only its length.
            log.info(
                "npm_auth: wrote %s with gitea %s registry%s",
                npmrc, _SCOPE,
                f" + _authToken (token len={len(token)})" if token else " (anonymous)",
            )
        except OSError as exc:
            log.warning(
                "npm_auth: could not write %s (%s) — npx of %s packages may fail",
                npmrc, exc, _SCOPE,
            )
