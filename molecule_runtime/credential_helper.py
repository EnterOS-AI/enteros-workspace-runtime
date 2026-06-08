"""Inline GitHub credential-helper installer.

Lifts the per-template wiring (Dockerfile COPY + entrypoint.sh git config +
nohup daemon launch) into the Python runtime. Templates that depend on
``molecule-ai-workspace-runtime`` get the behavior automatically — they no
longer need to maintain their own copy of the helper scripts or remember to
write the right git config in their entrypoint.

Background — fix for the #1933 cascade
======================================

GitHub App installation tokens (``ghs_…``) expire ~60 min after issue.
Workspaces inject the token at provision time as ``GH_TOKEN`` /
``GITHUB_TOKEN`` env vars; once the container has been alive >60 min,
every git push and gh CLI call returns 401. The platform exposes
``GET /admin/github-installation-token`` for live refresh, but the
workspace side has to (a) install a credential helper that hits that
endpoint, (b) configure git to call it, and (c) run a periodic refresh
daemon to keep ``gh auth login --with-token`` warm.

Before this module the wiring lived in each template's ``entrypoint.sh``
+ ``Dockerfile``. The ``claude-code-default`` template shipped without
it (cycle 62-66 incident: 39 workspaces lost their tokens, three PMs'
A2A queues filled with retry-status messages, manual fleet restart
required). Now any template that ``pip install molecule-ai-workspace-runtime``
+ calls :func:`install_credential_helper` early in startup gets the
behavior — the bug becomes structurally impossible.

What it does
============

On import / call:

1. Extracts the bundled helper scripts from package data to
   ``~/.molecule-runtime/scripts/`` (writable by agent user).
2. ``git config --global credential.https://github.com.helper`` → the
   extracted helper script. Idempotent.
3. Creates ``~/.molecule-token-cache/`` with mode 0700 (helper writes
   token cache files there).
4. Spawns the refresh daemon as a detached subprocess under a respawn
   loop. PID written to ``~/.molecule-runtime/refresh-daemon.pid`` so a
   restart of the runtime can detect + skip if already alive.
5. Runs initial ``gh auth login --with-token`` using whatever ``GH_TOKEN``
   env was injected at provision so commands work in the ~60s window
   before the daemon's first refresh fires.

Failures fail-soft (log + continue). The runtime starting is more
important than the credential helper being perfect — without it agents
still work for the first ~50 minutes, which is enough for the operator
to notice a log warning and restart.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

# Where extracted helper scripts live. Under HOME so the agent user can
# write to it without root. Templates that mount /tmp tmpfs are fine —
# this is per-process, not per-container, scope.
_INSTALL_DIR = Path(os.environ.get("HOME", "/home/agent")) / ".molecule-runtime" / "scripts"
_TOKEN_CACHE_DIR = Path(os.environ.get("HOME", "/home/agent")) / ".molecule-token-cache"
_DAEMON_PID_FILE = Path(os.environ.get("HOME", "/home/agent")) / ".molecule-runtime" / "refresh-daemon.pid"
_DAEMON_LOG_FILE = Path(os.environ.get("HOME", "/home/agent")) / ".molecule-runtime" / "refresh-daemon.log"

_HELPER_SCRIPT = "molecule-git-token-helper.sh"
_DAEMON_SCRIPT = "molecule-gh-token-refresh.sh"


def _extract_scripts() -> Path:
    """Copy bundled .sh files from package data to a writable dir.

    Returns the install directory containing the extracted scripts. Idempotent —
    if the files already exist with the same content, no-ops.
    """
    _INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    # importlib.resources.files() returns a Traversable that handles both
    # zipped wheels and editable installs. Iterate the bundled scripts/
    # subdir of this package.
    pkg_scripts = resources.files("molecule_runtime").joinpath("scripts")
    for entry in pkg_scripts.iterdir():
        if not entry.name.endswith(".sh"):
            continue
        target = _INSTALL_DIR / entry.name
        # Read source bytes via the Traversable interface (works for zips).
        src_bytes = entry.read_bytes()
        if target.exists() and target.read_bytes() == src_bytes:
            continue
        target.write_bytes(src_bytes)
        # chmod +x so the kernel can exec the script directly.
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return _INSTALL_DIR


def _configure_git_credential_helper(helper_path: Path) -> None:
    """Point git's credential helper for github.com at the extracted script."""
    # The leading `!` tells git the value is a shell command, not a builtin.
    helper_value = f"!{helper_path}"
    subprocess.run(
        ["git", "config", "--global",
         "credential.https://github.com.helper", helper_value],
        check=True, capture_output=True,
    )
    # useHttpPath=true so the cache key includes the repo path — relevant
    # if a workspace ever fetches multiple repos under different scopes.
    subprocess.run(
        ["git", "config", "--global",
         "credential.https://github.com.useHttpPath", "true"],
        check=True, capture_output=True,
    )


def _start_refresh_daemon(daemon_path: Path) -> None:
    """Spawn the refresh daemon as a detached child if not already running."""
    # Skip if a previous run's daemon is still alive (PID file + /proc check).
    if _DAEMON_PID_FILE.exists():
        try:
            old_pid = int(_DAEMON_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # signal 0 = check existence, no actual signal
            log.info("credential_helper: refresh daemon already running pid=%d", old_pid)
            return
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            # Stale PID file or process gone. Fall through to respawn.
            pass

    _DAEMON_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Wrap the daemon in a respawn loop so a single crash doesn't leave
    # the workspace stuck on an expired token (which is exactly how #1933
    # was discovered).
    wrapper = (
        f"while true; do "
        f"bash {daemon_path}; "
        f"echo \"[molecule-gh-token-refresh] daemon exited rc=$? — respawning in 30s\" >&2; "
        f"sleep 30; "
        f"done"
    )
    with open(_DAEMON_LOG_FILE, "ab") as log_handle:
        proc = subprocess.Popen(
            ["bash", "-c", wrapper],
            stdout=log_handle, stderr=log_handle,
            # Detach: new session so the daemon survives the runtime exiting.
            start_new_session=True,
            env={**os.environ, "TOKEN_HELPER_SCRIPT": str(_INSTALL_DIR / _HELPER_SCRIPT)},
        )
    _DAEMON_PID_FILE.write_text(str(proc.pid))
    log.info("credential_helper: refresh daemon spawned pid=%d", proc.pid)


# SCM-provider selector. Set to ``github`` or ``gitea`` to pin a single flow
# deterministically; unset means "use whichever single flow is configured,
# else fail-loud if both are present" (see runtime#104 RC). This replaces
# the prior implicit / order-dependent rule (presence of GH_TOKEN/GITHUB_TOKEN
# shadowed a working Gitea flow — incident 2026-06-08).
_VALID_GIT_PROVIDERS = frozenset({"github", "gitea"})


def _git_provider() -> str:
    """Resolve the configured SCM provider.

    Returns ``"github"``, ``"gitea"``, or ``""`` (unset — caller must decide
    based on which creds are present, and warn if BOTH are).
    """
    explicit = (os.environ.get("GIT_PROVIDER") or "").strip().lower()
    if explicit in _VALID_GIT_PROVIDERS:
        return explicit
    if explicit:
        # Unknown value — treat as unset, but flag it so a misconfig is loud.
        # SECURITY: do NOT log the raw env value (it could be a misplaced
        # token/credential — see RC 9680). Log only a constant marker.
        # Wording kept "not a recognized value" so the gating test can
        # match the substring without needing the raw value.
        log.warning(
            "credential_helper: GIT_PROVIDER is set, but the value is not a "
            "recognized value (expected 'github' or 'gitea') — treating as "
            "unset. Set the GIT_PROVIDER env var to a recognized value to "
            "silence this."
        )
    return ""


def _has_gitea_creds() -> bool:
    """True if any Gitea-style env var is set on this process.

    Covers the two ways Gitea auth is injected: (a) explicit
    GIT_HTTP_USERNAME/PASSWORD for the Gitea HTTPS endpoint, and
    (b) GITEA_TOKEN as a bearer-style fallback. Either is enough to
    signal "this workspace is on the Gitea SCM."
    """
    return bool(
        os.environ.get("GIT_HTTP_USERNAME")
        and os.environ.get("GIT_HTTP_PASSWORD")
    ) or bool(os.environ.get("GITEA_TOKEN"))


def _has_github_creds() -> bool:
    """True if any GitHub-style env var is set on this process."""
    return bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))


def _warn_stacked_flows(detected_github: bool, detected_gitea: bool) -> str:
    """Log a LOUD warning if both flows are present. Returns the chosen
    provider (preferring gitea — this org's canonical SCM).
    """
    if detected_github and detected_gitea:
        log.warning(
            "credential_helper: STACKED git-credential flows detected "
            "(both Gitea and GitHub creds present). Defaulting to gitea "
            "(org canonical). Set GIT_PROVIDER=github explicitly to "
            "opt into the GitHub flow. Per-agent credentials must be "
            "exactly ONE flow; stacking is almost always a remediation "
            "mistake (see runtime#104)."
        )
        return "gitea"
    if detected_github:
        return "github"
    if detected_gitea:
        return "gitea"
    return ""  # neither; caller will skip helper install entirely


def _initial_gh_auth() -> None:
    """Prime gh CLI with the provision-time token so commands work immediately.

    Honor ``GIT_PROVIDER=gitea``: if explicitly set to ``gitea``, skip the
    GitHub flow entirely (this workspace is on the canonical Gitea SCM).
    See runtime#104 for the RC.
    """
    if _git_provider() == "gitea":
        log.info("credential_helper: GIT_PROVIDER=gitea — skip initial gh auth")
        return
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log.info("credential_helper: no GH_TOKEN/GITHUB_TOKEN at startup — skip initial gh auth")
        return
    if not shutil.which("gh"):
        log.info("credential_helper: no gh CLI on PATH — skip initial gh auth (workspace will rely on git credential helper only)")
        return
    try:
        subprocess.run(
            ["gh", "auth", "login", "--hostname", "github.com", "--with-token"],
            input=token, text=True,
            check=True, capture_output=True, timeout=10,
        )
        log.info("credential_helper: initial gh auth login succeeded")
    except subprocess.SubprocessError as exc:
        # Non-fatal — refresh daemon will retry within ~60s.
        log.warning("credential_helper: initial gh auth login failed (non-fatal): %s", exc)


def install_credential_helper() -> None:
    """Install + configure + start the GitHub credential helper machinery.

    Safe to call multiple times. Each step is independently fault-tolerant:
    a failure in one (e.g. no git binary) doesn't prevent the others from
    trying.

    Intended to be called once early in the workspace runtime startup,
    before any code path that might invoke git or gh.

    Provider selection (see runtime#104):
    - ``GIT_PROVIDER=github`` → install + run the GitHub helper machinery.
    - ``GIT_PROVIDER=gitea`` → skip the GitHub machinery entirely; the
      workspace uses Gitea auth from ``GIT_HTTP_USERNAME/PASSWORD`` (or
      ``GITEA_TOKEN``) directly.
    - Unset, single flow present → use that flow (current behavior).
    - Unset, BOTH flows present → LOUD warning + default to gitea (the
      org canonical). Set ``GIT_PROVIDER`` explicitly to override.
    - Unset, no flows present → no-op (nothing to install).
    """
    # Resolve the provider up-front so every later step can short-circuit
    # cleanly if we're a gitea-only workspace.
    explicit = _git_provider()
    detected_github = _has_github_creds()
    detected_gitea = _has_gitea_creds()

    if explicit:
        provider = explicit
    else:
        provider = _warn_stacked_flows(detected_github, detected_gitea)

    if not provider:
        # No provider at all — nothing to do. (Workspace will rely on
        # whatever env-based auth git picks up, which is the existing
        # pre-helper behavior.)
        log.info(
            "credential_helper: no SCM provider configured (GIT_PROVIDER unset "
            "and neither Gitea nor GitHub creds present) — skipping all setup"
        )
        return

    if provider == "gitea":
        log.info(
            "credential_helper: provider=gitea (GIT_PROVIDER=%s, detected_gitea=%s, "
            "detected_github=%s) — skipping GitHub helper install; using Gitea "
            "env auth (GIT_HTTP_USERNAME/PASSWORD or GITEA_TOKEN) directly",
            explicit or "<unset>", detected_gitea, detected_github,
        )
        return

    # provider == "github": install the GitHub helper machinery.
    log.info(
        "credential_helper: provider=github (GIT_PROVIDER=%s, detected_github=%s, "
        "detected_gitea=%s)",
        explicit or "<unset>", detected_github, detected_gitea,
    )
    try:
        helper_dir = _extract_scripts()
    except (OSError, ModuleNotFoundError) as exc:
        log.warning("credential_helper: cannot extract scripts (%s) — skipping all setup", exc)
        return

    try:
        _TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_DIR.chmod(0o700)
    except OSError as exc:
        log.warning("credential_helper: cannot create token cache dir (%s) — helper will fail-open to env", exc)

    if shutil.which("git"):
        try:
            _configure_git_credential_helper(helper_dir / _HELPER_SCRIPT)
        except subprocess.SubprocessError as exc:
            log.warning("credential_helper: git config failed (%s) — git will use env-based auth only", exc)
    else:
        log.info("credential_helper: no git binary on PATH — skipping git config")

    try:
        _start_refresh_daemon(helper_dir / _DAEMON_SCRIPT)
    except OSError as exc:
        log.warning("credential_helper: refresh daemon failed to start (%s) — token will go stale after ~60min", exc)

    _initial_gh_auth()
