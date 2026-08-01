"""Declared-plugins boot-install — Python SSOT for the proven shell fetcher.

Background — agent-skills must survive a SaaS "restart"
======================================================
A workspace's DECLARED plugins (the DB desired-set, passed as
``MOLECULE_DECLARED_PLUGINS`` — a comma-separated list of plugin sources) are
installed into ``<config_path>/plugins`` BEFORE the runtime reads that
directory, so agent-skills survive the full ephemeral re-provision that a SaaS
"restart" performs (fresh instance + disk). This was proven first as a shell
block in the template entrypoint (``wt-claude-code/entrypoint.sh`` — runs as
ROOT pre-gosu) and ported to ``_oc-template`` (openclaw #130).

This module is the **base-runtime** Python equivalent of that shell block, so
EVERY runtime gets the boot-install uniformly — no per-template fork. It is
called from ``main.main()`` (step 0.2c) right after the npm-registry auth and
BEFORE ``load_config`` / ``adapter.setup``, so the plugins land on disk before
``adapter_base._common_setup`` reads ``<config_path>/plugins`` and
``install_plugins_via_registry`` wires their MCP/skills.

Fetch mechanism — git-native, provider-agnostic (this change)
=============================================================
The fetch step is a **git clone**, not a forge-specific archive REST call:

    git clone --depth 1 --single-branch --branch <ref> <repoURL> <dir>

then (for a declared subpath) the subdir is copied out of the checked-out tree;
the common case (the mgmt-MCP has NO subpath) copies the whole tree. This is
universal across forges — gitea, github, gitlab, self-hosted — and removes the
box's coupling to gitea's ``/api/v1/repos/.../archive/<ref>.tar.gz`` endpoint.

**Anonymous by default (credential-as-abstraction, private-only).** No token is
ever placed in the clone URL or on git's argv. Instead, when the box holds a
token for the repo's host, we wire a **per-host git credential helper** (keyed
on ``<scheme>://<host>``) that reads the token from the ``MOLECULE_GIT_CRED_TOKEN``
child-process env var. Git invokes a credential helper ONLY after the server
answers ``401`` — so a PUBLIC repo (the mgmt-MCP plugin repo is public) clones
anonymously and NO token is ever transmitted, while a PRIVATE repo's ``401``
triggers the helper and the token is supplied on the authenticated retry. This
closes the 401-poison risk of the old unconditional ``Authorization: token``
header (RCA #2970 class). ``GIT_TERMINAL_PROMPT=0`` is always set so a genuinely
private repo with no available credential fails fast instead of blocking boot on
an interactive prompt.

Two accepted source forms (any forge)
-------------------------------------
  * ``gitea://owner/repo[/subpath][#ref]`` — host resolved from config
    (``MOLECULE_GITEA_BASE_URL``); back-compat form, ``#ref`` default ``main``.
  * a full git URL — ``https://host/owner/repo[.git/subpath][#ref]`` (also
    ``git+https://``) — self-contained host, works for
    github/gitlab/self-hosted. Plain HTTP is rejected so plugin code and any
    configured repository credential never cross an unauthenticated transport.
    A ``.git/`` in the path delimits the repo from an in-repo subpath.
Any other scheme (e.g. ``github://``, ``presign://``) is skipped + logged,
exactly like the shell.

Token source — PER HOST, N providers (``_host_token_map``). A source may name any
forge, so the credential layer is keyed by host, not to one forge:

  * ``MOLECULE_GIT_TOKENS`` — JSON ``{"<host>": "<token>", ...}`` (general form);
  * ``MOLECULE_GIT_TOKEN__<HOST>`` — one var per host, ``.``/``-`` folded to
    ``_`` (e.g. ``MOLECULE_GIT_TOKEN__GITHUB_COM``);
  * the gitea read token (SSOT ``npm_auth.gitea_read_token``:
    ``MOLECULE_TEMPLATE_REPO_TOKEN`` → ``GITEA_TOKEN`` → the gitea git-http cred)
    seeded for the configured gitea host — the original single-forge credential,
    kept so nothing regresses.

A token is offered ONLY to the host it is keyed to (git resolves the helper per
``<scheme>://<host>``), so one forge's credential is never transmitted to
another. Before this, the map held only the gitea host, so a PRIVATE plugin repo
on github/gitlab/self-hosted got no token, 401'd, and failed the boot — the
fetch layer was provider-agnostic while the credential layer was not.

Atomic build-then-swap (hardening win #2 over the shell)
========================================================
The shell — and the first Python port — ``rm -rf``'d ``<plugins_dir>`` BEFORE
re-fetching, so a single transient fetch failure wiped skills a prior boot had
already materialized. This module instead builds the WHOLE declared set into a
sibling ``staging`` dir and only ``os.replace``-swaps it into place when every
source succeeds; any failure leaves the existing live tree untouched (retried
next boot). Full-replace semantics (a de-declared plugin doesn't linger) are
preserved — the swap just never leaves the live dir half-built/empty.

Hardening win #1 over the shell: the subpath copied out of the tree is
containment-guarded (``_is_within``) so a crafted ``../``-escaping subpath is
rejected. (With ``git clone`` the checked-out tree is written by git within the
clone dir — there is no archive-member write-escape vector as the shell
``cp -a`` had; the ``.git`` metadata dir is stripped before copy so the plugins
tree matches the old archive semantics.)

Provider seam (source-provider-ecosystem): ``_PROVIDERS`` maps a URL scheme to a
fetch handler; both handlers funnel into one ``_git_fetch_tree`` clone core.
``parse_declared_plugins`` accepts any registered scheme automatically; an
unknown scheme is skipped + logged (matches the shell).

Idempotency / cutover: this rebuilds ``<plugins_dir>`` from the same source list
the shell block uses, so during the template cutover BOTH run (shell first as
root pre-gosu, this second as the agent uid in ``main``) and the second run
simply rebuilds the identical tree via staging+swap — harmless, and this Python
run is authoritative (it runs second, in ``main``).

Shell mirror status (NOT yet parity — do not assume it): the boot-install shell
block lives in the TEMPLATE repos (``entrypoint.sh``), NOT here, and still uses
the archive REST fetch. It fetches the (public) mgmt-MCP anonymously today only
because ``MOLECULE_TEMPLATE_REPO_TOKEN`` is on the box FORBIDDEN-ENV denylist
(``core workspace_provision_forbidden_env.go``), so its unconditional-token
branch never fires on the box. A matching git-native + per-host-cred-helper
rewrite of each template's shell block is a REQUIRED FOLLOW-UP (one PR per
template repo) before the shell can be treated as consistent with this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from molecule_runtime import manifest_ssot
from molecule_runtime.npm_auth import gitea_read_token, resolve_gitea_base

log = logging.getLogger(__name__)

# Configured gitea forge base host (for the ``gitea://`` back-compat form) is
# resolved by npm_auth.resolve_gitea_base — the SSOT shared with npm registry
# resolution: MOLECULE_PLUGIN_REGISTRY (provider-agnostic name core SETS on the
# box) → MOLECULE_GITEA_BASE_URL (shell-mirror back-compat alias) → documented
# default. The resolver + its constants live in npm_auth so the forge host is
# spelled once for BOTH git and npm (audit finding C1 — no re-spelled host
# literal). The back-compat-default log now emits from npm_auth's logger.

# git binary. PATH ``git`` (consistent with credential_helper.py). Overridable so
# an operator can pin an absolute path; our clone never puts a token on the URL,
# so a token-stripping git shell-wrapper (if any) is harmless to this path.
_GIT_BINARY_ENV = "MOLECULE_GIT_BINARY"
_DEFAULT_GIT_BINARY = "git"

# Child-env var the per-host inline credential helper reads the token from. Kept
# OUT of git's argv (no ``ps`` leak) and out of the clone URL — git reads it only
# when it invokes the helper, which happens only on a 401 challenge.
_CRED_TOKEN_ENVVAR = "MOLECULE_GIT_CRED_TOKEN"

# Per-host credential env convention for the N-provider case:
# MOLECULE_GIT_TOKEN__GITHUB_COM=ghp_… (host `.`/`-` folded to `_`). The JSON map
# MOLECULE_GIT_TOKENS is the general form; this is the flat-env alternative.
_PER_HOST_TOKEN_PREFIX = "MOLECULE_GIT_TOKEN__"

# Git URL schemes accepted as a full self-contained source (any forge). ``git+``
# variants are normalized to plain http(s) for the actual clone.
_GIT_URL_SCHEMES = ("https", "git+https")

# Clone timeout — mirrors the shell ``--max-time``/``timeout`` guard. Overridable
# so an operator on a slow link can widen it without a code change.
_DEFAULT_FETCH_TIMEOUT_SECONDS = 120.0
_FETCH_TIMEOUT_ENV = "MOLECULE_PLUGIN_FETCH_TIMEOUT"

_URL_USERINFO_RE = re.compile(r"(?i)\b((?:git\+)?https?://)[^/\s@]+@")
_URL_QUERY_VALUE_RE = re.compile(r"([?&][^=\s&]+)=[^&\s]+")
_URL_FRAGMENT_RE = re.compile(
    r"((?:git\+)?https?://[^\s#]+)#[^\s]+", re.IGNORECASE
)


def _redact_log_text(value: object, *secrets: str) -> str:
    """Remove credentials from untrusted text before it reaches a log."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    text = _URL_QUERY_VALUE_RE.sub(r"\1=<redacted>", text)
    return _URL_FRAGMENT_RE.sub(r"\1#<redacted>", text)


def _source_log_label(raw: str) -> str:
    """Return a useful source label without userinfo, query, or ref values."""
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
        has_userinfo = parts.username is not None or parts.password is not None
    except (TypeError, ValueError):
        return "<invalid plugin source>"
    if not scheme:
        return "<invalid plugin source>"

    host = hostname or "<invalid-host>"
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    userinfo = "<redacted>@" if has_userinfo else ""
    query = "?<redacted>" if parts.query else ""
    fragment = "#<ref>" if parts.fragment else ""
    return f"{scheme}://{userinfo}{host}{parts.path}{query}{fragment}"


@dataclass(frozen=True)
class PluginSource:
    """One parsed declared-plugin source.

    Populated differently by the two accepted forms:
      * ``gitea://owner/repo[/subpath][#ref]`` sets ``owner``/``repo`` and leaves
        ``host``/``clone_url`` empty — the host is resolved from config at fetch.
      * a full git URL sets ``host``/``clone_url`` (self-contained) and leaves
        ``owner``/``repo`` empty.

    ``name`` is the on-disk directory created under ``<plugins_dir>`` — the last
    path segment of ``subpath`` when a subpath is given, else the repo name.
    ``raw`` keeps the original token for the skip/installed/failed log lines.
    """

    scheme: str
    subpath: str
    ref: str
    name: str
    raw: str
    owner: str = ""
    repo: str = ""
    host: str = ""
    clone_url: str = ""


@dataclass
class InstallReport:
    """Outcome of a boot-install run — for the one-line boot summary + tests."""

    declared: bool = False
    plugins_dir: str | None = None
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # True once the freshly-built staging tree was atomically swapped into the
    # live ``plugins_dir``. ``installed`` lists sources that materialized into
    # STAGING; they only went LIVE when ``swapped`` is True.
    #
    # A PARTIAL PROMOTION IS STILL A PROMOTION.
    #
    # This comment used to say False meant the tree was left intact "either
    # because a source failed (we never promote a partial build) or the swap
    # rename itself failed". The first half stopped being true at the test5 fix
    # (2026-07-13, see the long note at the swap site below): "a failed source
    # fails THAT SOURCE — not the whole tree". A boot with a failed source now
    # carries every live dir the successful sources are not replacing into
    # staging, logs "promoting the N that succeeded", swaps, and sets
    # ``swapped = True`` with ``failed`` NON-EMPTY. The stale wording outlived
    # the behaviour by a year and propagated a wrong liveness rule
    # (``declared && swapped && failed == []``) into the SDK contract, core's
    # receiver and a migration comment, where it read healthy partially-degraded
    # workspaces as NOT LIVE. Corrected in molecule-ai-sdk#190 +
    # molecule-core#4972 to:
    #
    #     live     iff declared && swapped        (the tree was promoted)
    #     degraded iff live && failed != []       (…but not everything is in it)
    #
    # So ``swapped`` answers exactly one question — was the staging tree promoted
    # — and ``failed`` is a separate axis. False means the LIVE TREE WAS NOT
    # TOUCHED, which happens in exactly three places, none of them "some source
    # failed":
    #
    #   * every source failed, so there was nothing to promote;
    #   * carrying an already-installed dir forward failed, so promoting staging
    #     would have DELETED a working plugin;
    #   * the atomic rename itself raised.
    swapped: bool = False

    def summary(self) -> str:
        if not self.declared:
            return "[plugins] no MOLECULE_DECLARED_PLUGINS declared — boot-install skipped"
        return (
            "[plugins] boot-install complete: "
            f"installed={len(self.installed)} skipped={len(self.skipped)} "
            f"failed={len(self.failed)} swapped={self.swapped} -> {self.plugins_dir}"
        )


# ---------------------------------------------------------------------------
# Parsing — pure, side-effect-light (logs skips like the shell), unit-testable.
# ---------------------------------------------------------------------------
def _is_safe_install_name(name: str) -> bool:
    """Return whether *name* is one portable directory entry.

    Source names become children of the private staging directory. Reject dot
    components and either platform's separator so that contract remains safe
    if parsing or installation is reused outside the current Linux runtime.
    """
    return bool(name) and name not in {".", ".."} and not any(
        marker in name for marker in ("/", "\\", "\x00")
    )


def _install_destination(staging_dir: Path, name: str) -> Path:
    """Resolve a source destination and prove it remains below staging."""
    if not _is_safe_install_name(name):
        raise ValueError(f"unsafe plugin install name: {name!r}")

    staging_root = staging_dir.resolve()
    destination = staging_dir / name
    resolved_destination = destination.resolve()
    try:
        resolved_destination.relative_to(staging_root)
    except ValueError as exc:
        raise ValueError(f"plugin destination escapes staging: {name!r}") from exc
    if resolved_destination == staging_root:
        raise ValueError(f"plugin destination is staging root: {name!r}")
    return destination


def _parse_gitea(token: str) -> PluginSource | None:
    """Parse a ``gitea://owner/repo[/subpath][#ref]`` token (host resolved later).

    Mirrors ``entrypoint.sh`` lines 241-256: ``#ref`` default ``main``, name =
    last path segment of subpath else repo; a structurally-invalid spec is
    skipped + logged like the shell's ``bad source`` branch.
    """
    try:
        parts = urlsplit(token)
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None
    if has_userinfo or parts.query:
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None

    spec = token.split("://", 1)[1]

    # '#ref' suffix -> ref; default 'main'. Faithful to the shell:
    #   ref  = ${spec##*#}  (everything after the LAST '#')
    #   spec = ${spec%%#*}  (everything before the FIRST '#')
    ref = "main"
    if "#" in spec:
        ref = spec.rsplit("#", 1)[1] or "main"
        spec = spec.split("#", 1)[0]

    owner, _slash, rest = spec.partition("/")
    repo, _slash2, subpath = rest.partition("/")
    # name = last path segment of subpath, else repo (entrypoint.sh:255-256)
    name = subpath.rsplit("/", 1)[-1] if subpath else repo

    if not owner or not repo or not _is_safe_install_name(name):
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None

    return PluginSource(
        scheme="gitea",
        owner=owner,
        repo=repo,
        subpath=subpath,
        ref=ref,
        name=name,
        raw=token,
    )


def _parse_git_url(token: str) -> PluginSource | None:
    """Parse a full git URL source (``https|git+https://...``).

    Self-contained host; ``#ref`` default ``main``. A ``.git/`` in the path
    delimits the repo (clone target) from an in-repo subpath; otherwise the whole
    path is the repo and there is no subpath. ``name`` = last subpath segment
    else the repo's last segment (trailing ``.git`` stripped). Malformed → skip.
    """
    try:
        parts = urlsplit(token)
        username = parts.username
        password = parts.password
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None
    scheme = parts.scheme.lower()
    # Normalize git+https -> https for the actual clone URL.
    clone_scheme = scheme[len("git+"):] if scheme.startswith("git+") else scheme
    if username is not None or password is not None or parts.query:
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None
    host = hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    ref = parts.fragment or "main"
    path = parts.path

    if not host or not path.strip("/"):
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None

    # Split repo vs in-repo subpath on a ``.git/`` delimiter (optional).
    subpath = ""
    repo_path = path
    marker = ".git/"
    idx = path.find(marker)
    if idx != -1:
        repo_path = path[: idx + len(".git")]  # include the ``.git`` suffix
        subpath = path[idx + len(marker):].strip("/")

    clone_url = f"{clone_scheme}://{host}{repo_path}"

    repo_seg = repo_path.rstrip("/").rsplit("/", 1)[-1]
    if repo_seg.endswith(".git"):
        repo_seg = repo_seg[: -len(".git")]
    name = subpath.rsplit("/", 1)[-1] if subpath else repo_seg

    if not _is_safe_install_name(name):
        log.info("[plugins] bad source: %s", _source_log_label(token))
        return None

    return PluginSource(
        scheme=scheme,
        subpath=subpath,
        ref=ref,
        name=name,
        raw=token,
        host=host,
        clone_url=clone_url,
    )


def _parse_one(token: str) -> PluginSource | None:
    """Parse one comma-token into a :class:`PluginSource`, or None to skip.

    A token with an unknown scheme (not registered in ``_PROVIDERS``) or a
    structurally-invalid spec is skipped + logged, exactly like the shell's
    ``skip unsupported source`` / ``bad source`` branches.
    """
    if "://" not in token:
        log.info("[plugins] skip unsupported source: %s", _source_log_label(token))
        return None
    scheme = token.split("://", 1)[0].lower()
    if scheme not in _PROVIDERS:
        log.info("[plugins] skip unsupported source: %s", _source_log_label(token))
        return None
    if scheme == "gitea":
        return _parse_gitea(token)
    return _parse_git_url(token)


def parse_declared_plugins(raw: str | None) -> list[PluginSource]:
    """Split + validate ``MOLECULE_DECLARED_PLUGINS`` into ordered sources.

    Empty / all-whitespace tokens are dropped; unknown-scheme and malformed
    tokens are skipped + logged. Pure: no fetch, no disk writes.
    """
    sources: list[PluginSource] = []
    if not raw:
        return sources
    for token in raw.split(","):
        # ``tr -d '[:space:]'`` — strip ALL whitespace, not just ends.
        token = "".join(token.split())
        if not token:
            continue
        src = _parse_one(token)
        if src is not None:
            sources.append(src)
    return sources


# ---------------------------------------------------------------------------
# Containment guard (the hardening win — a crafted subpath must not escape).
# ---------------------------------------------------------------------------
def _is_within(base: Path, target: Path) -> bool:
    """True iff ``target`` is ``base`` or nested under it (resolved)."""
    base_r = base.resolve()
    target_r = target.resolve()
    return target_r == base_r or base_r in target_r.parents


def _is_generated_bytecode(rel: str) -> bool:
    """True for CPython's generated bytecode — never part of a plugin's content.

    core#5009 root cause. A spawned MCP server imports its modules out of the
    LIVE tree, and CPython writes ``__pycache__/*.pyc`` back into that tree. The
    next boot-install pass then compares live (with bytecode) against a freshly
    cloned staging tree (without), sees a difference, and swaps — replacing the
    tree underneath the very server whose imports created those files. The
    running server's own bytecode triggers the swap that orphans it, so it
    recurred on every boot.

    It also hid itself: the .pyc exist only between spawn and swap, so anyone
    inspecting afterwards finds a clean tree and rules bytecode out. Measured on
    a customer workspace: MCP spawn 01:19:53, 28 .pyc listed as the entire diff,
    swap 01:19:58.

    These files are reproducible from the .py sources and are never shipped by a
    plugin author, so excluding them cannot mask a real update.
    """
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith((".pyc", ".pyo"))


def _tree_fingerprint(root: Path) -> tuple[dict[str, str], bool]:
    """``(relative posix path -> content+mode digest, fully_readable)``.

    Content-addressed on purpose: mtimes change on every build (the tree is
    re-cloned), so only hashes can answer "did anything actually change".

    THE EXEC BIT IS PART OF THE ANSWER. git tracks 100644 vs 100755, so a
    "forgot chmod +x" fix upstream is a real update whose blob is byte-identical.
    Hashing content alone reported that as unchanged, skipped the swap, and left
    the entrypoint non-executable while the install report claimed the tree was
    live and correct — and no later boot could repair it, because the difference
    can never become a content difference. Only the exec bit is compared, not
    the full mode: the control plane chowns delivered trees and umask differs
    between the clone and the live copy, so comparing every permission bit would
    swap on noise and reintroduce the core#5009 orphaning it exists to prevent.

    Symlinks are recorded by TARGET rather than followed — a retarget is a real
    change, and following one could wander outside the tree.

    ``fully_readable`` is False when any file or directory could not be read.
    Callers MUST NOT treat two fingerprints as equal in that case: "I could not
    read this" is not evidence of equality, and the previous symmetric
    ``unreadable:<class>`` sentinel made two identically-unreadable files
    compare EQUAL — so a genuine change to a file the runtime cannot read was
    silently never promoted, on that boot and every future one. Worse,
    ``Path.rglob`` swallows permission errors while descending, so an unreadable
    SUBDIRECTORY contributed zero entries to both sides and an entire subtree
    could change while the comparison still reported "unchanged". ``os.walk``
    with ``onerror`` is used so a directory we cannot descend is observed
    instead of silently skipped.
    """
    out: dict[str, str] = {}
    readable = True
    if not root.is_dir():
        return out, readable

    def _onerror(exc: OSError) -> None:
        nonlocal readable
        readable = False
        log.warning(
            "[plugins] fingerprint: %s could not be read (%s) — cannot prove the "
            "live tree matches, so the swap will not be skipped",
            getattr(exc, "filename", "?"), exc.__class__.__name__,
        )

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror, followlinks=False):
        base = Path(dirpath)
        for name in sorted(dirnames) + sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if _is_generated_bytecode(rel):
                continue
            try:
                if path.is_symlink():
                    out[rel] = "symlink:" + os.readlink(path)
                    continue
                if name in dirnames or not path.is_file():
                    continue
                st = path.stat()
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                mode = "x" if (st.st_mode & 0o111) else "-"
                out[rel] = f"{h.hexdigest()}:{mode}"
            except OSError as exc:
                readable = False
                out[rel] = f"unreadable:{exc.__class__.__name__}"
                log.warning(
                    "[plugins] fingerprint: %s could not be read (%s) — cannot "
                    "prove the live tree matches, so the swap will not be skipped",
                    rel, exc.__class__.__name__,
                )
    return out, readable


def _changed_plugin_names(
    staged: dict[str, str], live: dict[str, str]
) -> list[str]:
    """Top-level plugin directories that differ between the two fingerprints."""
    names: set[str] = set()
    for rel in set(staged) | set(live):
        if staged.get(rel) != live.get(rel):
            names.add(rel.split("/", 1)[0])
    return sorted(names)


# How many changed paths to name before truncating. Enough to diagnose a
# repeating replacement, small enough that a legitimately large update does not
# bury the rest of the boot log.
_CHANGED_FILES_LOG_LIMIT = 12


def _changed_file_summary(
    staged: dict[str, str], live: dict[str, str]
) -> str:
    """``path (added|removed|modified)`` for each differing file, truncated.

    Naming only the PLUGIN is not enough to diagnose a replacement that repeats
    every boot: it says something differs, never what. Deployed to production
    this fired on each boot for one plugin and left the next step to guesswork —
    stale bytecode, a delivery marker, a carry-forward path — each costing a
    release cycle to test. The set is already computed to decide the swap, so
    reporting it is free.
    """
    rows: list[str] = []
    for rel in sorted(set(staged) | set(live)):
        s, l = staged.get(rel), live.get(rel)
        if s == l:
            continue
        if l is None:
            rows.append(f"{rel} (added)")
        elif s is None:
            rows.append(f"{rel} (removed)")
        else:
            rows.append(f"{rel} (modified)")
    if not rows:
        return "(no file-level difference — check permissions/symlinks)"
    shown = rows[:_CHANGED_FILES_LOG_LIMIT]
    if len(rows) > _CHANGED_FILES_LOG_LIMIT:
        shown.append(f"... and {len(rows) - _CHANGED_FILES_LOG_LIMIT} more")
    return "; ".join(shown)


def _atomic_swap_dir(staging_dir: Path, target_dir: Path) -> None:
    """Replace ``target_dir`` with ``staging_dir`` as atomically as the platform
    permits.

    ``staging_dir`` and ``target_dir`` MUST live on the same filesystem (the
    caller stages a sibling of ``target_dir``) so the renames are atomic and
    never fall back to a slow cross-device copy. Strategy:

      1. move any existing live tree aside (``os.replace`` — atomic rename);
      2. rename the freshly-built staging tree into place (atomic);
      3. delete the moved-aside copy.

    If step 2 fails, the moved-aside copy is restored so the live tree is never
    left missing. Raises ``OSError`` on an unrecoverable rename failure so the
    caller can record ``swapped=False`` and keep the staging cleanup in its
    ``finally``.
    """
    staging_dir = Path(staging_dir)
    target_dir = Path(target_dir)
    backup: Path | None = None
    if target_dir.exists():
        backup = target_dir.with_name(
            f".{target_dir.name}.old-{os.getpid()}-{time.time_ns()}"
        )
        os.replace(target_dir, backup)
    try:
        os.replace(staging_dir, target_dir)
    except OSError:
        # Putting staging in place failed — restore the previous tree so the
        # live plugins dir is never left missing.
        if backup is not None and not target_dir.exists():
            try:
                os.replace(backup, target_dir)
                backup = None
            except OSError:
                pass
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fetch core — one git-clone path shared by every provider.
# ---------------------------------------------------------------------------
# A git object id: 40 hex chars (sha1) or 64 (sha256, for repos on the newer
# object format). Anything else — `main`, `v0.5.1`, `release/x` — is a ref name
# git can resolve remotely, and takes the `clone --branch` path.
_COMMIT_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _is_commit_sha(ref: str) -> bool:
    """True when ``ref`` is a bare commit SHA (needs fetch-by-object-id, not
    ``clone --branch``, which only resolves branch/tag NAMES)."""
    return bool(_COMMIT_SHA_RE.match((ref or "").strip().lower()))


def _rmtree_force(path: Path) -> None:
    """``rmtree`` that also removes read-only entries.

    git marks objects in ``.git`` read-only. A plain
    ``rmtree(..., ignore_errors=True)`` cannot unlink those on platforms that
    enforce the read-only bit (Windows), so it silently gives up and the ``.git``
    dir SHIPS INSIDE the plugins tree — the strip looks like it worked because
    the errors were swallowed. Chmod-then-retry on each failure.
    """
    def _on_error(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass  # genuinely stuck — fail soft, as before

    shutil.rmtree(path, onerror=_on_error)


def _iter_dirs(parent: Path) -> "list[Path]":
    """Immediate sub-directories of ``parent`` ([] when it does not exist).

    Used to carry an already-installed plugins tree forward across a swap when
    some source failed this boot.
    """
    try:
        return [p for p in parent.iterdir() if p.is_dir()]
    except OSError:
        return []


def _host_cred_config_args(scheme: str, host: str) -> list[str]:
    """git ``-c`` args wiring a per-host inline credential helper.

    The helper reads the token from the ``MOLECULE_GIT_CRED_TOKEN`` child-env var
    (NOT argv, NOT the URL) and emits it on ``get``. Git invokes it ONLY after a
    ``401`` — so public repos clone anonymously (no token sent) and private repos
    are authenticated on the retry. We first RESET any inherited helper for this
    exact host (empty value) so the behaviour is deterministic, then append ours.
    """
    key = f"credential.{scheme}://{host}"
    helper = (
        "!f() { test \"$1\" = get && "
        "printf 'username=oauth2\\npassword=%s\\n' "
        "\"$" + _CRED_TOKEN_ENVVAR + "\"; }; f"
    )
    return ["-c", f"{key}.helper=", "-c", f"{key}.helper={helper}"]


def _git_fetch_tree(
    *,
    clone_url: str,
    host: str,
    scheme: str,
    ref: str,
    subpath: str,
    raw: str,
    token: str,
    git_binary: str,
    workdir: Path,
    timeout: float,
) -> Path | None:
    """Fetch ``clone_url`` at ``ref`` into ``workdir`` and return the dir whose
    contents become ``<plugins_dir>/<name>/`` (the clone root, or
    ``<clone_root>/<subpath>``), or None on a fetch/subpath failure.

    Anonymous by default: the token (if any) is supplied ONLY via a per-host
    credential helper that git consults on a 401, never on the URL/argv. Fail-
    soft: any error logs ``[plugins] fetch/extract failed`` and returns None so
    the caller continues with the next source.

    REF SHAPE — two paths, because git needs different plumbing for each:

    * a branch or tag name -> ``clone --depth 1 --branch <ref>``.
    * a bare commit SHA    -> ``init`` + ``fetch --depth 1 origin <sha>`` +
      ``checkout FETCH_HEAD``. ``--branch`` does NOT accept a raw SHA; git
      resolves it against the remote's refs and fails with "Remote branch <sha>
      not found in upstream origin".

    The SHA path is not hypothetical: the catalog pins declared plugins BY
    COMMIT (that is what a pin is for). When the fetch moved from archive-by-SHA
    to git-clone, the producer kept emitting SHA pins the consumer could no
    longer resolve, so every SHA-pinned plugin failed to install — and because a
    failed source aborts the tree swap, a concierge on a de-baked image lost its
    management MCP with it and fail-closed to `failed`. Ref: the test5 incident,
    2026-07-13.
    """
    if token and scheme.lower() != "https":
        log.warning(
            "[plugins] refusing credential over non-HTTPS source: %s",
            _source_log_label(raw),
        )
        return None

    clone_dir = workdir / "clone"

    child_env = dict(os.environ)
    child_env["GIT_TERMINAL_PROMPT"] = "0"  # never block boot on a cred prompt
    cred_args: list[str] = []
    if token and host:
        # Wire the 401-only per-host helper and hand git the token via env.
        cred_args = _host_cred_config_args(scheme, host)
        child_env[_CRED_TOKEN_ENVVAR] = token

    def _git(*args: str) -> list[str]:
        return [git_binary, *cred_args, *args]

    if _is_commit_sha(ref):
        # PROVIDER-AGNOSTIC by necessity: a plugin source can name ANY forge
        # (gitea://, or a full git URL to github / gitlab / a self-hosted box —
        # see _PROVIDERS), and forges do NOT agree on whether a bare object id
        # may be requested directly.
        #
        # Preferred: fetch the single commit by object id — cheap (--depth 1),
        # one round trip. This needs `uploadpack.allowReachableSHA1InWant` on the
        # REMOTE. Gitea, GitHub and GitLab all enable it; plain `git` ships with
        # it OFF, so a self-hosted remote answers "Server does not allow request
        # for unadvertised object" and this path fails.
        #
        # Fallback: clone the repo normally and check the pin out of local
        # history. Costs full history, but works against EVERY git server, so a
        # SHA-pinned plugin on a restrictive forge still resolves instead of
        # failing the boot. Correctness does not depend on the remote's config —
        # only speed does.
        cmds = [
            _git("init", "--quiet", str(clone_dir)),
            _git("-C", str(clone_dir), "remote", "add", "origin", clone_url),
            _git("-C", str(clone_dir), "fetch", "--depth", "1", "origin", ref),
            _git("-C", str(clone_dir), "checkout", "--quiet", "--detach", "FETCH_HEAD"),
        ]
        fallback_cmds = [
            _git("clone", "--quiet", clone_url, str(clone_dir)),
            _git("-C", str(clone_dir), "checkout", "--quiet", "--detach", ref),
        ]
    else:
        cmds = [
            _git(
                "clone",
                "--depth", "1",
                "--single-branch",
                "--branch", ref,
                clone_url,
                str(clone_dir),
            )
        ]
        fallback_cmds = []

    def _run_all(sequence: "list[list[str]]") -> None:
        for cmd in sequence:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env,
            )

    try:
        _run_all(cmds)
    except (subprocess.SubprocessError, OSError) as exc:
        stderr = (getattr(exc, "stderr", "") or "").strip()
        if not fallback_cmds:
            # The label is host/path only and the stderr is redacted of the
            # token, the ref, and any URL userinfo/query before it reaches the
            # log — a cred must never surface even if git echoes one.
            log.warning(
                "[plugins] fetch/extract failed: %s (%s) %s",
                _source_log_label(raw),
                exc.__class__.__name__,
                _redact_log_text(stderr, token, ref)[:500],
            )
            return None

        # SHA pin, and fetch-by-object-id was refused (most likely the remote
        # does not allow requesting an unadvertised object). Retry the
        # server-agnostic way: clone, then check the pin out of local history.
        log.info(
            "[plugins] fetch-by-object-id refused for %s (%s) — retrying via "
            "clone+checkout (works on any git server)",
            _source_log_label(raw),
            _redact_log_text(stderr, token, ref)[:200],
        )
        _rmtree_force(clone_dir)  # the half-initialized repo must not poison it
        try:
            _run_all(fallback_cmds)
        except (subprocess.SubprocessError, OSError) as exc2:
            stderr2 = (getattr(exc2, "stderr", "") or "").strip()
            log.warning(
                "[plugins] fetch/extract failed: %s (%s) %s",
                _source_log_label(raw),
                exc2.__class__.__name__,
                _redact_log_text(stderr2, token, ref)[:500],
            )
            return None

    # Record WHICH COMMIT we actually got, while ``.git`` still exists.
    #
    # core#5009 / core#5007: the runtime used to clone and never write down the
    # resolved commit, so when a box served unexpected behaviour there was no
    # way — from the box — to tell what it had installed. The control plane's
    # `installed_sha` is a claim by the control plane, not an observation of the
    # box, so the two can disagree silently and did. One line per plugin closes
    # that for a human reading `docker logs`, with no protocol change.
    #
    # A moving ref (``#main``) resolves to a different commit over time, which
    # is exactly why the ref alone is not enough to identify a tree.
    #
    # FAIL-OPEN: observability must never become a new way to fail a boot, so a
    # rev-parse that errors, times out, or prints nothing logs "unknown" and the
    # install proceeds.
    head_sha = "unknown"
    try:
        _rev = subprocess.run(
            _git("-C", str(clone_dir), "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True,
            timeout=timeout, env=child_env,
        )
        head_sha = (_rev.stdout or "").strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        pass
    log.info(
        "[plugins] fetched %s at commit %s (ref %s)",
        _source_log_label(raw), head_sha, _redact_log_text(ref, token),
    )

    # Strip VCS metadata so the installed tree matches the old archive semantics
    # (a tarball of the tree carried no ``.git``) and the plugins dir never holds
    # a nested repo.
    _rmtree_force(clone_dir / ".git")

    content_dir = clone_dir / subpath if subpath else clone_dir
    if not _is_within(clone_dir, content_dir):
        log.warning(
            "[plugins] subpath escapes repo: %s (%s)",
            _redact_log_text(subpath, token),
            _source_log_label(raw),
        )
        return None
    if not content_dir.is_dir():
        log.warning(
            "[plugins] subpath not in repo: %s (%s)",
            _redact_log_text(subpath, token),
            _source_log_label(raw),
        )
        return None
    return content_dir


# ---------------------------------------------------------------------------
# Provider seam — scheme -> fetch handler (uniform signature).
# ---------------------------------------------------------------------------
def _fetch_gitea(
    source: PluginSource,
    *,
    base_url: str,
    token: str,
    git_binary: str,
    workdir: Path,
    timeout: float,
) -> Path | None:
    """Resolve the gitea host from config and git-clone ``owner/repo``."""
    base = base_url.rstrip("/")
    base_parts = urlsplit(base)
    host = base_parts.netloc
    clone_scheme = base_parts.scheme.lower()
    clone_url = f"{base}/{source.owner}/{source.repo}.git"
    return _git_fetch_tree(
        clone_url=clone_url,
        host=host,
        scheme=clone_scheme,
        ref=source.ref,
        subpath=source.subpath,
        raw=source.raw,
        token=token,
        git_binary=git_binary,
        workdir=workdir,
        timeout=timeout,
    )


def _fetch_git_url(
    source: PluginSource,
    *,
    base_url: str,  # noqa: ARG001 — uniform provider signature; host is self-contained
    token: str,
    git_binary: str,
    workdir: Path,
    timeout: float,
) -> Path | None:
    """git-clone a full self-contained git URL source (any forge)."""
    clone_scheme = urlsplit(source.clone_url).scheme or "https"
    return _git_fetch_tree(
        clone_url=source.clone_url,
        host=source.host,
        scheme=clone_scheme,
        ref=source.ref,
        subpath=source.subpath,
        raw=source.raw,
        token=token,
        git_binary=git_binary,
        workdir=workdir,
        timeout=timeout,
    )


# Scheme -> fetch handler. Both funnel into ``_git_fetch_tree`` (one clone core).
# A future provider registers its scheme here and ``parse_declared_plugins``
# accepts it automatically (the source-provider-ecosystem seam).
#   * ``gitea``                              — host from config, box clones itself.
#   * ``https``/``git+https`` — full self-contained git URL.
_PROVIDERS: dict[str, Callable[..., "Path | None"]] = {
    "gitea": _fetch_gitea,
    "https": _fetch_git_url,
    "git+https": _fetch_git_url,
}


# ---------------------------------------------------------------------------
# Orchestrator — the public entry point called from main().
# ---------------------------------------------------------------------------
def _resolve_plugins_dir(env: Mapping[str, str], plugins_dir: str | Path | None) -> Path:
    if plugins_dir is not None:
        return Path(plugins_dir)
    config_path = env.get("WORKSPACE_CONFIG_PATH") or "/configs"
    return Path(config_path) / "plugins"


# Gitea base-host resolution is the SSOT owned by npm_auth (shared git+npm);
# _resolve_gitea_base is a back-compat private alias for the imported resolver.
_resolve_gitea_base = resolve_gitea_base


def _host_token_map(env: Mapping[str, str], base_url: str) -> dict[str, str]:
    """Map ``host -> token`` for every forge the box holds a credential for.

    The plugin-source layer is provider-agnostic BY DESIGN (``_PROVIDERS``): a
    source may name our gitea, github, gitlab, or a customer's self-hosted box.
    The credential layer must be too, or the abstraction is a lie — a PRIVATE
    plugin repo on any host other than the configured gitea would get no token,
    take a 401, and (with GIT_TERMINAL_PROMPT=0) fail the boot. So the map is
    built from config, not hardcoded to one forge:

      1. ``MOLECULE_GIT_TOKENS`` — a JSON object ``{"<host>": "<token>", ...}``.
         The general, N-provider form. Hosts are matched on netloc.
      2. ``MOLECULE_GIT_TOKEN__<HOST>`` — one var per host, with the host's
         ``.`` and ``-`` folded to ``_`` (e.g. ``MOLECULE_GIT_TOKEN__GITHUB_COM``).
         For operators who would rather not ship a JSON blob.
      3. the gitea read token (SSOT ``npm_auth.gitea_read_token``), keyed to the
         configured gitea host — the existing single-forge credential, kept as a
         SEED so nothing regresses.

    Later sources do not clobber earlier ones: an explicit per-host entry wins
    over the inherited gitea default for the same host. Tokens are only ever
    handed to git through the per-host 401-only credential helper — never in a
    URL, never on argv (see ``_host_cred_config_args``).
    """
    tokens: dict[str, str] = {}

    raw_json = (env.get("MOLECULE_GIT_TOKENS") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except ValueError as exc:
            # Never fail the boot on a malformed credential blob — a plugin that
            # needs the token will fail loudly on its own 401.
            log.warning("[plugins] MOLECULE_GIT_TOKENS is not valid JSON (%s) — ignored", exc)
            parsed = None
        if isinstance(parsed, Mapping):
            for host, token in parsed.items():
                host = _netloc(str(host))
                if host and isinstance(token, str) and token:
                    tokens[host] = token
        elif parsed is not None:
            log.warning("[plugins] MOLECULE_GIT_TOKENS must be a JSON object — ignored")

    for key, value in env.items():
        if not key.startswith(_PER_HOST_TOKEN_PREFIX) or not value:
            continue
        suffix = key[len(_PER_HOST_TOKEN_PREFIX):]
        if not suffix:
            continue
        # GITHUB_COM -> github.com. A host with a dash cannot be recovered from
        # the folded form, so an operator with one should use the JSON map.
        host = suffix.lower().replace("_", ".")
        tokens.setdefault(host, value)

    gitea_host = _netloc(base_url)
    gitea_tok = gitea_read_token(env)
    if gitea_host and gitea_tok:
        tokens.setdefault(gitea_host, gitea_tok)

    return tokens


def _netloc(url_or_host: str) -> str:
    """netloc of a URL, or the string itself when already a bare host."""
    value = (url_or_host or "").strip()
    if not value:
        return ""
    if "//" in value:
        return urlsplit(value).netloc
    return value.split("/", 1)[0]


def install_declared_plugins(
    plugins_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> InstallReport:
    """Install ``MOLECULE_DECLARED_PLUGINS`` into ``<plugins_dir>`` (boot-install).

    No-op (returns a ``declared=False`` report) when ``MOLECULE_DECLARED_PLUGINS``
    is unset/empty, so existing behaviour is byte-for-byte unchanged when the
    signal is absent.

    Fetch is a git clone, anonymous by default (see the module docstring); the
    atomic build-then-swap and manifest-SSOT gate below are unchanged — only the
    per-source FETCH mechanism changed. Fail-soft: never raises into the caller —
    the runtime starting matters more than any one plugin landing.
    """
    if env is None:
        env = os.environ
    report = InstallReport()

    raw = env.get("MOLECULE_DECLARED_PLUGINS") or ""
    sources = parse_declared_plugins(raw)
    # ``declared`` reflects the SIGNAL being present (mirrors the shell's
    # ``if [ -n "${MOLECULE_DECLARED_PLUGINS:-}" ]`` gate), so an all-valid
    # build still fully replaces the dir exactly like the shell does.
    report.declared = bool(raw.strip())
    if not report.declared:
        return report

    target_dir = _resolve_plugins_dir(env, plugins_dir)
    report.plugins_dir = str(target_dir)

    # Every source must own one distinct child directory. ``copytree`` with
    # ``dirs_exist_ok=True`` would otherwise merge two same-basename sources,
    # allowing later files to overwrite earlier ones and (for a bare-skill
    # source) inherit another source's plugin.yaml during manifest validation.
    # Reject the whole desired set before any fetch so the last promoted tree
    # remains intact.
    sources_by_name: dict[str, list[PluginSource]] = {}
    for source in sources:
        sources_by_name.setdefault(source.name, []).append(source)
    collisions = {
        name: grouped
        for name, grouped in sources_by_name.items()
        if len(grouped) > 1
    }
    if collisions:
        for name, grouped in collisions.items():
            log.warning(
                "[plugins] duplicate install destination %r from %s — "
                "keeping existing plugins tree intact",
                name,
                ", ".join(_source_log_label(source.raw) for source in grouped),
            )
            report.failed.extend(source.raw for source in grouped)
        return report

    base_url = _resolve_gitea_base(env)
    host_tokens = _host_token_map(env, base_url)
    git_binary = (env.get(_GIT_BINARY_ENV) or "").strip() or _DEFAULT_GIT_BINARY
    try:
        timeout = float(env.get(_FETCH_TIMEOUT_ENV) or _DEFAULT_FETCH_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = _DEFAULT_FETCH_TIMEOUT_SECONDS

    # Stage the new tree as a SIBLING of target_dir so the swap rename stays on
    # one filesystem (atomic, no EXDEV copy). The live tree is NOT touched until
    # the staging build fully succeeds.
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Can't even prepare the parent (e.g. a future template that skipped the
        # entrypoint chown -> EPERM). Fail-soft: log + return, never block boot.
        log.warning(
            "[plugins] cannot prepare parent of %s (%s) — skipping boot-install",
            target_dir, exc,
        )
        return report

    staging_dir = target_dir.with_name(
        f".{target_dir.name}.staging-{os.getpid()}-{time.time_ns()}"
    )
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "[plugins] cannot create staging dir %s (%s) — skipping boot-install",
            staging_dir, exc,
        )
        return report

    try:
        for source in sources:
            fetch = _PROVIDERS.get(source.scheme)
            if fetch is None:  # defensive — parse already filtered unknown schemes
                log.info(
                    "[plugins] skip unsupported source: %s",
                    _source_log_label(source.raw),
                )
                report.skipped.append(source.raw)
                continue
            try:
                dest = _install_destination(staging_dir, source.name)
            except ValueError as exc:
                log.warning(
                    "[plugins] unsafe destination: %s (%s)",
                    _source_log_label(source.raw),
                    exc,
                )
                report.failed.append(source.raw)
                continue
            # Resolve the per-host token for THIS source's host. gitea:// resolves
            # its host from base_url; a full URL carries its own host.
            source_host = source.host or urlsplit(base_url).netloc
            token = host_tokens.get(source_host, "")
            with tempfile.TemporaryDirectory(prefix="molecule-plugin-") as td:
                content_dir = fetch(
                    source,
                    base_url=base_url,
                    token=token,
                    git_binary=git_binary,
                    workdir=Path(td),
                    timeout=timeout,
                )
                if content_dir is None:
                    report.failed.append(source.raw)
                    continue
                try:
                    shutil.copytree(content_dir, dest, dirs_exist_ok=True)
                except OSError as exc:
                    log.warning(
                        "[plugins] copy failed: %s (%s)",
                        _source_log_label(source.raw),
                        exc,
                    )
                    report.failed.append(source.raw)
                    continue
            # molecule-core#3383 plugin-manifest SSOT gate. advisory_check
            # never raises; it logs the advisory line and returns the
            # violations. FAIL-CLOSED promotion (PR-4): when a plugin.yaml is
            # PRESENT but violating and enforcement is on, the source is
            # rejected exactly like a failed fetch — which blocks the swap
            # below, preserving the previous live tree. Carve-out: a MISSING
            # plugin.yaml (bare-SKILL.md plugins are common and legal) stays
            # advisory-only, as does everything when
            # MOLECULE_MANIFEST_SSOT_ENFORCE=off.
            manifest_file = dest / "plugin.yaml"
            violations = manifest_ssot.advisory_check(
                source.name, manifest_file, log=log, prefix="[plugins] ",
            )
            if (
                violations
                and manifest_file.is_file()
                and manifest_ssot.enforcement_enabled()
            ):
                log.warning(
                    "[plugins] SSOT manifest ENFORCEMENT: rejecting %s: "
                    "%d violation(s): %s",
                    _source_log_label(source.raw),
                    len(violations),
                    "; ".join(violations),
                )
                report.failed.append(source.raw)
                continue
            log.info(
                "[plugins] staged %s <- %s",
                source.name,
                _source_log_label(source.raw),
            )
            report.installed.append(source.raw)

        # A failed source fails THAT SOURCE — not the whole tree.
        #
        # This used to abort the swap entirely whenever any source failed, to
        # protect already-installed skills from a transient gitea blip. But the
        # veto is indiscriminate: it also discards the sources that fetched
        # FINE, and on a de-baked image one of those is the concierge's own
        # management MCP. So an unfetchable third-party plugin took the mgmt-MCP
        # down with it, `register` fail-closed on mcp_server_present=false, and
        # the agent parked in `failed` — permanently, since every retry hit the
        # same bad source. Worse, the "keep the existing tree" safety net is
        # VACUOUS on a first boot: there is no previous tree to keep, so the
        # workspace booted with an EMPTY plugins dir. A guard meant to preserve
        # state, applied where no state exists, destroyed the boot instead.
        # (Live: staging test5, 2026-07-13 — Lark pinned to an unfetchable SHA.)
        #
        # We keep the original intent WITHOUT the collateral damage. On a
        # partial failure, carry EVERY live dir the successful sources are not
        # replacing into staging, then swap. So:
        #
        #   * a source that fetched fine goes live regardless of its neighbours
        #     (the mgmt-MCP is no longer hostage to a third-party plugin);
        #   * a transient blip cannot delete an already-installed plugin — its
        #     previous copy is carried forward byte-for-byte;
        #   * anything else already live (e.g. a plugin added at runtime via
        #     install_plugin, absent from MOLECULE_DECLARED_PLUGINS) is carried
        #     forward too, exactly as the old no-swap path preserved it;
        #   * a plugin that never installed and has no previous copy is simply
        #     absent — not a half-written tree.
        #
        # A boot where EVERY source fails promotes nothing: there is nothing to
        # promote, so we skip the swap and leave the live tree untouched (the
        # cheapest correct thing, and it keeps the swap off the failure path).
        if report.failed and not report.installed:
            log.warning(
                "[plugins] all %d source(s) failed — keeping the existing plugins "
                "tree intact (no swap); will retry next boot", len(sources),
            )
            report.swapped = False
            return report

        if report.failed:
            carry_failed = False
            for previous in _iter_dirs(target_dir):
                if (staging_dir / previous.name).exists():
                    continue  # a successful source owns this name — it wins
                try:
                    shutil.copytree(previous, staging_dir / previous.name)
                    log.info(
                        "[plugins] carrying the already-installed %s forward "
                        "(a source failed this boot; not deleting it)",
                        previous.name,
                    )
                except OSError as exc:
                    # A transient copytree error (dangling symlink / unreadable /
                    # special file in the EXISTING plugin) must NOT delete a working
                    # plugin: promoting staging now would drop it, since it is absent
                    # from staging (review [1]). Abort the partial swap and keep the
                    # live tree intact — the successful new sources retry next boot,
                    # preserving the "a transient blip cannot delete an already-
                    # installed plugin" invariant. Discard any half-written copy.
                    carry_failed = True
                    shutil.rmtree(staging_dir / previous.name, ignore_errors=True)
                    log.warning(
                        "[plugins] could not carry %s forward (%s) — keeping the "
                        "existing plugins tree intact (no swap this boot)",
                        previous.name, exc,
                    )
            if carry_failed:
                report.swapped = False
                return report
            log.warning(
                "[plugins] %d of %d source(s) failed — promoting the %d that "
                "succeeded; failed sources retry next boot",
                len(report.failed), len(sources), len(report.installed),
            )

        # ------------------------------------------------------------------
        # core#5009: DO NOT swap a tree that did not change.
        #
        # _atomic_swap_dir is an os.replace, i.e. a directory RENAME: the live
        # path survives but points at a NEW inode and the old directory is
        # unlinked. A process that already imported modules out of the old
        # directory keeps executing that old code for its whole lifetime, and
        # nothing on disk reveals it — the live files hash equal to upstream
        # while the serving process answers from the replaced tree.
        #
        # boot-install runs TWICE per boot (the `prepare` pass that
        # pre-materializes config before the gateway launches, then the real
        # serve), and hermes spawns each plugin's MCP server BETWEEN them. The
        # second pass rebuilds an IDENTICAL tree, so its swap buys nothing and
        # costs every already-spawned MCP server its module identity. Measured
        # on a live tenant: gateway 22:53:53, MCP server 22:53:56, second-pass
        # swap 22:54:01.
        #
        # Comparing content first makes the no-change rebuild a true no-op.
        # `swapped` stays True because it answers "is the staged tree now
        # live" — which it is, precisely because it already was.
        staged_fp, staged_ok = _tree_fingerprint(staging_dir)
        if target_dir.exists():
            live_fp, live_ok = _tree_fingerprint(target_dir)
        else:
            live_fp, live_ok = None, True
        # Equality is only meaningful when BOTH trees were fully readable.
        # Skipping on an unproven match is worse than an unnecessary swap: the
        # update silently never lands and no future boot can repair it.
        if live_fp is not None and staged_ok and live_ok and staged_fp == live_fp:
            log.info(
                "[plugins] live tree already matches the staged build (%d file(s), "
                "%d plugin(s)) — skipping the swap so running MCP servers keep "
                "their imported modules (core#5009)",
                len(staged_fp), len(list(_iter_dirs(target_dir))),
            )
            report.swapped = True
            return report

        try:
            if live_fp is not None:
                # The genuinely dangerous case, and the one that was silent.
                # A code-only change does not alter the `mcp_servers` mapping,
                # so mcp-reconcile-watch.sh (a STRUCTURAL compare) will not
                # restart the gateway: any MCP server already running keeps
                # serving the previous code until something restarts it.
                log.warning(
                    "[plugins] live tree REPLACED: %s changed — an MCP server "
                    "already running from the previous tree will keep serving "
                    "the OLD code until the workspace restarts (core#5009); "
                    "config-only reconcilers do not detect a code-only change. "
                    "Changed: %s",
                    ", ".join(_changed_plugin_names(staged_fp, live_fp)) or "(content)",
                    _changed_file_summary(staged_fp, live_fp),
                )
            _atomic_swap_dir(staging_dir, target_dir)
            report.swapped = True
        except OSError as exc:
            log.warning(
                "[plugins] atomic swap into %s failed (%s) — existing tree left intact",
                target_dir, exc,
            )
            report.swapped = False
        return report
    finally:
        # Remove the staging tree if it wasn't consumed by the swap (failure
        # path, or any leftover after a partial rename). A successful swap
        # renamed it away, so this is a harmless no-op there.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
