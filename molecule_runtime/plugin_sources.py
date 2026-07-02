"""Declared-plugins boot-install — Python SSOT for the proven shell fetcher.

Background — agent-skills must survive a SaaS "restart"
======================================================
A workspace's DECLARED plugins (the DB desired-set, passed as
``MOLECULE_DECLARED_PLUGINS`` — a comma-separated list of ``gitea://`` sources)
are installed into ``<config_path>/plugins`` BEFORE the runtime reads that
directory, so agent-skills survive the full ephemeral re-provision that a SaaS
"restart" performs (fresh instance + disk). This was proven first as a shell
block in the template entrypoint (``wt-claude-code/entrypoint.sh`` — runs as
ROOT pre-gosu) and ported to ``_oc-template`` (openclaw #130).

This module is the **base-runtime** Python equivalent of that shell block, so
EVERY runtime gets the boot-install uniformly — no per-template fork. It is
called from ``main.main()`` (step 0.4) right after the npm-registry auth and
BEFORE ``load_config`` / ``adapter.setup``, so the plugins land on disk before
``adapter_base._common_setup`` reads ``<config_path>/plugins`` and
``install_plugins_via_registry`` wires their MCP/skills.

Faithful to the shell block (``entrypoint.sh`` lines 214-284):
  * split ``MOLECULE_DECLARED_PLUGINS`` on commas, strip all whitespace;
  * accept only ``gitea://owner/repo[/subpath][#ref]`` (default ref ``main``,
    name = last path segment of subpath else repo);
  * fetch ``<base>/api/v1/repos/<owner>/<repo>/archive/<ref>.tar.gz`` with
    ``Authorization: token <MOLECULE_TEMPLATE_REPO_TOKEN>`` (unauth fallback);
  * untar, copy ``<subpath|top>/.`` into a STAGING tree, then atomically swap
    the staging tree into ``<plugins_dir>`` only on a fully-successful build;
  * fail-soft — a fetch/extract failure logs and never blocks boot; an empty
    ``MOLECULE_DECLARED_PLUGINS`` is a no-op (existing behaviour preserved).

Atomic build-then-swap (hardening win #2 over the shell)
========================================================
The shell — and the first Python port — ``rm -rf``'d ``<plugins_dir>`` BEFORE
re-fetching, so a single transient fetch failure wiped skills a prior boot had
already materialized. This module instead builds the WHOLE declared set into a
sibling ``staging`` dir and only ``os.replace``-swaps it into place when every
source succeeds; any failure leaves the existing live tree untouched (retried
next boot). Full-replace semantics (a de-declared plugin doesn't linger) are
preserved — the swap just never leaves the live dir half-built/empty.

Hardening win #1 over the shell: ``tarfile`` extraction is path-traversal-guarded
(the shell ``cp -a`` had no such guard), so a malicious archive member whose
resolved path escapes the extraction dir is rejected.

Provider seam (source-provider-ecosystem): ``_PROVIDERS`` maps a URL scheme to
a fetch handler. v1 ships ``gitea`` only; a future github/gitlab/local provider
registers its scheme here and ``parse_declared_plugins`` accepts it
automatically. An unknown scheme is skipped + logged (matches the shell).

Idempotency / cutover: this rebuilds ``<plugins_dir>`` from the same source
list the shell block uses, so during the template cutover BOTH run (shell first
as root pre-gosu, this second as the agent uid in ``main``) and the second run
simply rebuilds the identical tree via staging+swap — harmless. Templates drop
their shell copies LATER, gated by a runtime-capability floor; this base change
must never regress the proven flow.
"""
from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from molecule_runtime import manifest_ssot

log = logging.getLogger(__name__)

# Default gitea base — overridable via MOLECULE_GITEA_BASE_URL (mirrors the
# shell ``${MOLECULE_GITEA_BASE_URL:-https://git.moleculesai.app}``).
_DEFAULT_GITEA_BASE = "https://git.moleculesai.app"

# Read token for the gitea archive fetch — the SAME token the box already holds
# for git/npm fetches (npm_auth.py SSOT). Unauth fallback when absent, exactly
# like the shell's ``if [ -n "${MOLECULE_TEMPLATE_REPO_TOKEN:-}" ]`` branch.
_TOKEN_ENV = "MOLECULE_TEMPLATE_REPO_TOKEN"

# Fetch timeout — mirrors the shell ``curl --max-time 120``. Overridable so an
# operator on a slow link can widen it without a code change.
_DEFAULT_FETCH_TIMEOUT_SECONDS = 120.0
_FETCH_TIMEOUT_ENV = "MOLECULE_PLUGIN_FETCH_TIMEOUT"


@dataclass(frozen=True)
class PluginSource:
    """One parsed ``gitea://owner/repo[/subpath][#ref]`` declared-plugin source.

    ``name`` is the on-disk directory created under ``<plugins_dir>`` — the last
    path segment of ``subpath`` when a subpath is given, else ``repo`` (mirrors
    ``entrypoint.sh`` lines 248-256). ``raw`` keeps the original token for the
    skip/installed/failed log lines so they read like the shell's.
    """

    scheme: str
    owner: str
    repo: str
    subpath: str
    ref: str
    name: str
    raw: str

    def archive_path(self) -> str:
        """Gitea archive REST path: ``/api/v1/repos/<owner>/<repo>/archive/<ref>.tar.gz``."""
        return f"/api/v1/repos/{self.owner}/{self.repo}/archive/{self.ref}.tar.gz"


@dataclass
class InstallReport:
    """Outcome of a boot-install run — for the one-line boot summary + tests."""

    declared: bool = False
    plugins_dir: str | None = None
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # True once the freshly-built staging tree was atomically swapped into the
    # live ``plugins_dir``. False means the existing tree was left intact — either
    # because a source failed (we never promote a partial build) or the swap
    # rename itself failed. ``installed`` lists sources that materialized into
    # staging; they only went LIVE when ``swapped`` is True.
    swapped: bool = False

    def summary(self) -> str:
        if not self.declared:
            return "[plugins] no MOLECULE_DECLARED_PLUGINS declared — boot-install skipped"
        return (
            "[plugins] boot-install complete: "
            f"installed={len(self.installed)} skipped={len(self.skipped)} "
            f"failed={len(self.failed)} swapped={self.swapped} -> {self.plugins_dir}"
        )


class PluginExtractError(Exception):
    """Raised when an archive member would extract outside the destination
    (path-traversal guard) — caught per-source so one bad archive can't abort
    the others."""


# ---------------------------------------------------------------------------
# Parsing — pure, side-effect-light (logs skips like the shell), unit-testable.
# ---------------------------------------------------------------------------
def _parse_one(token: str) -> PluginSource | None:
    """Parse one comma-token into a :class:`PluginSource`, or None to skip.

    Mirrors ``entrypoint.sh`` lines 241-256. A token with an unknown scheme
    (not registered in ``_PROVIDERS``) or a structurally-invalid spec is
    skipped + logged, exactly like the shell's ``skip unsupported source`` /
    ``bad source`` branches.
    """
    if "://" not in token:
        log.info("[plugins] skip unsupported source: %s", token)
        return None
    scheme, spec = token.split("://", 1)
    if scheme not in _PROVIDERS:
        log.info("[plugins] skip unsupported source: %s", token)
        return None

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

    if not owner or not repo or not name:
        log.info("[plugins] bad source: %s", token)
        return None

    return PluginSource(
        scheme=scheme,
        owner=owner,
        repo=repo,
        subpath=subpath,
        ref=ref,
        name=name,
        raw=token,
    )


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
# Extraction — path-traversal-guarded (the hardening win over the shell cp -a).
# ---------------------------------------------------------------------------
def _is_within(base: Path, target: Path) -> bool:
    """True iff ``target`` is ``base`` or nested under it (resolved)."""
    base_r = base.resolve()
    target_r = target.resolve()
    return target_r == base_r or base_r in target_r.parents


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract every member, rejecting any whose resolved path — or symlink/
    hardlink target — escapes ``dest``. The shell ``cp -a`` had no such guard."""
    dest = dest.resolve()
    members = tar.getmembers()
    for member in members:
        member_path = dest / member.name
        if not _is_within(dest, member_path):
            raise PluginExtractError(
                f"archive member escapes destination: {member.name!r}"
            )
        # Link targets must also stay inside dest (symlink/hardlink traversal).
        if member.issym() or member.islnk():
            link_target = dest / member.name
            resolved_link = (link_target.parent / member.linkname)
            if not _is_within(dest, resolved_link):
                raise PluginExtractError(
                    f"archive link target escapes destination: "
                    f"{member.name!r} -> {member.linkname!r}"
                )
    tar.extractall(dest)  # noqa: S202 — members validated above


def _find_archive_top(extract_dir: Path) -> Path | None:
    """Return the single top-level directory of a gitea archive (``<repo>/``).

    Mirrors the shell ``find -mindepth 1 -maxdepth 1 -type d | head -n1``.
    """
    for child in sorted(extract_dir.iterdir()):
        if child.is_dir():
            return child
    return None


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
# Provider seam — scheme -> fetch handler. v1: gitea only.
# ---------------------------------------------------------------------------
def _fetch_gitea(
    source: PluginSource,
    *,
    base_url: str,
    token: str,
    workdir: Path,
    timeout: float,
) -> Path | None:
    """Fetch + extract a gitea archive into ``workdir``; return the content dir
    whose contents become ``<plugins_dir>/<name>/`` (``<top>`` or
    ``<top>/<subpath>``), or None on a fetch/extract/subpath failure.

    Fail-soft: any error logs ``[plugins] fetch/extract failed`` and returns
    None so the caller continues with the next source.
    """
    url = base_url.rstrip("/") + source.archive_path()
    headers = {"Authorization": f"token {token}"} if token else {}
    archive_file = workdir / "archive.tar.gz"
    extract_dir = workdir / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.stream(
            "GET", url, headers=headers, timeout=timeout, follow_redirects=True
        ) as resp:
            resp.raise_for_status()
            with open(archive_file, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    except Exception as exc:  # noqa: BLE001 — fail-soft per source
        log.warning("[plugins] fetch/extract failed: %s (%s)", source.raw, exc)
        return None

    try:
        with tarfile.open(archive_file, "r:gz") as tar:
            _safe_extract_tar(tar, extract_dir)
    except (tarfile.TarError, PluginExtractError, OSError) as exc:
        log.warning("[plugins] fetch/extract failed: %s (%s)", source.raw, exc)
        return None

    top = _find_archive_top(extract_dir)
    if top is None:
        log.warning("[plugins] fetch/extract failed: %s (empty archive)", source.raw)
        return None

    content_dir = top / source.subpath if source.subpath else top
    if not content_dir.is_dir():
        log.warning(
            "[plugins] subpath not in archive: %s (%s)", source.subpath, source.raw
        )
        return None
    return content_dir


# Scheme -> fetch handler. A future github/gitlab/local provider registers here;
# ``parse_declared_plugins`` then accepts that scheme automatically (the
# source-provider-ecosystem seam — subsumes the providers-SSOT proposal).
_PROVIDERS: dict[str, Callable[..., "Path | None"]] = {
    "gitea": _fetch_gitea,
}


# ---------------------------------------------------------------------------
# Orchestrator — the public entry point called from main().
# ---------------------------------------------------------------------------
def _resolve_plugins_dir(env: Mapping[str, str], plugins_dir: str | Path | None) -> Path:
    if plugins_dir is not None:
        return Path(plugins_dir)
    config_path = env.get("WORKSPACE_CONFIG_PATH") or "/configs"
    return Path(config_path) / "plugins"


def install_declared_plugins(
    plugins_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> InstallReport:
    """Install ``MOLECULE_DECLARED_PLUGINS`` into ``<plugins_dir>`` (boot-install).

    No-op (returns a ``declared=False`` report) when ``MOLECULE_DECLARED_PLUGINS``
    is unset/empty, so existing behaviour is byte-for-byte unchanged when the
    signal is absent.

    Atomic build-then-swap (the fix for F1's wipe-on-transient-failure)
    -----------------------------------------------------------------
    The previous implementation ``rm -rf``'d the live ``plugins_dir`` UP FRONT
    and then re-fetched — so a single transient fetch failure wiped skills a
    prior boot had already materialized, leaving the workspace skill-less for
    that boot. Instead we now fetch the WHOLE declared set into a sibling
    ``staging`` directory first, and only ``os.replace``-swap it into place when
    EVERY source materialized. On any fetch/copy failure (or a swap-rename
    failure) the existing live tree is left untouched — the build is discarded
    and retried next boot. The swap still drops plugins no longer in the declared
    set (full-replace semantics preserved), it just never leaves the live dir in
    a half-built/empty state. Fail-soft: never raises into the caller — the
    runtime starting matters more than any one plugin landing.
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

    base_url = (env.get("MOLECULE_GITEA_BASE_URL") or _DEFAULT_GITEA_BASE).strip()
    token = (env.get(_TOKEN_ENV) or "").strip()
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
                log.info("[plugins] skip unsupported source: %s", source.raw)
                report.skipped.append(source.raw)
                continue
            with tempfile.TemporaryDirectory(prefix="molecule-plugin-") as td:
                content_dir = fetch(
                    source,
                    base_url=base_url,
                    token=token,
                    workdir=Path(td),
                    timeout=timeout,
                )
                if content_dir is None:
                    report.failed.append(source.raw)
                    continue
                dest = staging_dir / source.name
                try:
                    shutil.copytree(content_dir, dest, dirs_exist_ok=True)
                except OSError as exc:
                    log.warning("[plugins] copy failed: %s (%s)", source.raw, exc)
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
                    source.raw, len(violations), "; ".join(violations),
                )
                report.failed.append(source.raw)
                continue
            log.info("[plugins] staged %s <- %s", source.name, source.raw)
            report.installed.append(source.raw)

        # Atomic swap — ONLY when every declared source materialized. On any
        # failure keep the existing live tree (no swap), so a transient gitea
        # blip never deletes already-installed skills for this boot.
        if report.failed:
            log.warning(
                "[plugins] %d of %d source(s) failed — keeping existing plugins "
                "tree intact (no swap); will retry next boot",
                len(report.failed), len(sources),
            )
            report.swapped = False
            return report

        try:
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
