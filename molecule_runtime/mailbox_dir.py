"""Resolve the durable *mailbox* directory for the workspace runtime.

Single gating point for the ``MOLECULE_MAILBOX_KERNEL`` migration. All durable
agent state — inbox cursor, pre-stop snapshot, delegation cursor + result
tombstones, consolidated agent memory — belongs on the PERSISTENT WORKSPACE
VOLUME so it survives a container restart / auto-heal. That volume is
``/workspace``; the runtime writes this state under ``/workspace/.molecule`` —
the same convention chat-uploads and inbox attachments already use (see
``executor_helpers.CHAT_UPLOADS_DIR`` and ``INBOX_ATTACHMENTS_DIR``).

Durability is a DEPLOYMENT PROPERTY, not a given
------------------------------------------------
``/workspace`` is durable only where it is mounted on a persistent volume — a
Docker named volume (local / staging) or a dedicated EBS data volume (SaaS
EC2). On a flavor where ``/workspace`` is merely a directory on the *ephemeral
root disk* — e.g. a production EC2 workspace with the per-org data volume
disabled, where ``/workspace`` is NOT in the cp#326 backup/restore set — the
kernel's ``mkdir`` still succeeds but the state is silently lost on the next
instance recreate / auto-heal. :func:`verify_durability` probes for exactly
this at kernel-on boot and warns LOUD + records an observable status, so a
non-durable flavor is caught before it drops agent state rather than after.

Why not ``$HOME`` or ``/configs``?
  - ``$HOME`` (``~/.claude``, ``~/.molecule-workspace``) is CONTAINER-LOCAL
    scratch. An auto-heal spins a fresh container and ``$HOME`` is empty, so
    cursor / tombstone state written there is silently lost — that off-box
    reset is exactly what let the 2026-06-29 re-narration loop re-read a
    historical backlog after a restart.
  - ``/configs`` is the PROVISIONER-OWNED config volume: param-rendered
    ``config.yaml`` + prompt files, rewritten fresh on every provision.
    Agent-authored durable state co-mingled there is (a) clobbered on
    re-provision and (b) the wrong layering — ``/configs`` is *read* for the
    param-rendered system prompt, never the *write* target for evolving memory.

Gating (byte-identical default)
-------------------------------
``MOLECULE_MAILBOX_KERNEL`` unset / false (DEFAULT)
    :func:`resolve` returns the LEGACY location (``configs_dir.resolve()``), so
    every migrated call site behaves EXACTLY as it did before. The proven
    push / hard-gate flow is unchanged — no new directory, no path move.

``MOLECULE_MAILBOX_KERNEL`` true
    :func:`resolve` returns ``/workspace/.molecule`` (overridable via
    ``MOLECULE_MAILBOX_DIR``), created ``0700`` so per-file ``0600`` perms are
    not undermined by a world-readable parent.

Not cached: the flag + one ``mkdir`` are cheap and reading ``os.environ`` live
keeps tests that monkeypatch ``MOLECULE_MAILBOX_KERNEL`` between cases working
without a reset hook.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import molecule_runtime.configs_dir as configs_dir

logger = logging.getLogger(__name__)

#: Env flag that turns the mailbox kernel (durable-volume state) on. Default
#: OFF — when unset the runtime keeps every durable path at its legacy location
#: so the proven push flow is byte-identical.
KERNEL_FLAG_ENV = "MOLECULE_MAILBOX_KERNEL"

#: Override for the durable base dir (kernel-on only). Defaults to the durable
#: workspace volume so state survives restart / auto-heal.
MAILBOX_DIR_ENV = "MOLECULE_MAILBOX_DIR"

_DEFAULT_MAILBOX_DIR = "/workspace/.molecule"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def kernel_enabled() -> bool:
    """True iff the mailbox kernel is switched on via ``MOLECULE_MAILBOX_KERNEL``.

    Read live (not cached) so a test toggling the env var between cases sees
    the change without a reset hook. This is the SINGLE flag every migrated
    call site consults; when it returns False the runtime is byte-identical to
    the pre-migration behavior.
    """
    return os.environ.get(KERNEL_FLAG_ENV, "").strip().lower() in _TRUTHY


def resolve() -> Path:
    """Return the durable base directory for agent state.

    Kernel ON  -> ``/workspace/.molecule`` (or ``MOLECULE_MAILBOX_DIR``),
                  created ``0700``.
    Kernel OFF -> ``configs_dir.resolve()`` — the LEGACY location, unchanged.
    """
    if not kernel_enabled():
        # Byte-identical default: durable state stays exactly where it lived
        # before the migration.
        return configs_dir.resolve()

    base = Path(os.environ.get(MAILBOX_DIR_ENV, "").strip() or _DEFAULT_MAILBOX_DIR)
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        # Best-effort: a read-only or already-existing parent must not crash
        # boot. Callers that actually write will surface a clear error — and
        # :func:`verify_durability` turns the *silent* case (writes land on an
        # ephemeral root disk) into a LOUD, observable warning at kernel-on boot.
        pass
    return base


# --- Durability guard --------------------------------------------------------
#
# The kernel relocates durable state onto ``/workspace`` on the assumption that
# ``/workspace`` is a persistent volume. On a flavor where it is only a
# directory on the ephemeral root disk that assumption is silently false — the
# ``mkdir`` above succeeds, writes succeed, and the state is lost on the next
# instance recreate / auto-heal (cp#326). :func:`verify_durability` probes the
# resolved base once at kernel-on boot and classifies it so a non-durable
# deployment is caught and surfaced rather than losing data quietly.

#: Base sits on a distinct, persistent mounted volume (different ``st_dev`` from
#: ``/``) and is writable — durable across instance recreate / auto-heal.
DURABILITY_DURABLE = "durable"
#: Base is writable but lives on the SAME device as ``/`` (the root filesystem),
#: i.e. it is a plain directory on the ephemeral root disk — state is lost on
#: instance recreate / auto-heal. This is the case the guard exists to catch.
DURABILITY_EPHEMERAL = "ephemeral"
#: Base cannot be written (read-only mount, permissions, missing parent) — the
#: kernel's durable writes will fail outright.
DURABILITY_UNWRITABLE = "unwritable"
#: Kernel off (or probe skipped) — the durability guard does not apply.
DURABILITY_NA = "n/a"

_PROBE_FILENAME = ".molecule-durability-probe"

#: Last durability status computed by :func:`verify_durability`, exposed for
#: observability (e.g. a heartbeat field) without re-probing the filesystem.
_last_durability_status: str = DURABILITY_NA


def _nearest_existing(path: Path) -> Path | None:
    """The path itself if it exists, else its nearest existing ancestor."""
    p = path
    seen = 0
    while seen < 64:
        if p.exists():
            return p
        if p.parent == p:
            return None
        p = p.parent
        seen += 1
    return None


def _is_on_root_device(base: Path) -> bool | None:
    """True iff ``base`` is on the same device as ``/`` (ephemeral root disk).

    Compares ``st_dev`` of the nearest existing path at/under ``base`` against
    ``/``. A persistent mount (Docker named volume, EBS data volume) is a
    distinct device -> returns False (durable). ``None`` when it cannot be
    determined (stat failure). Portable across Linux and macOS (both give
    distinct ``st_dev`` per mount); a tmpfs override would read as a distinct
    device — acceptable, since the only real deployment path is the default
    ``/workspace`` (never a tmpfs).
    """
    try:
        probe = _nearest_existing(base)
        if probe is None:
            return None
        return os.stat(probe).st_dev == os.stat(os.sep).st_dev
    except OSError:
        return None


def _is_writable(base: Path) -> bool:
    """Write + read back + delete a sentinel under ``base`` — proves real I/O."""
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        sentinel = base / _PROBE_FILENAME
        sentinel.write_text("probe", encoding="utf-8")
        ok = sentinel.read_text(encoding="utf-8") == "probe"
        sentinel.unlink()
        return ok
    except OSError:
        return False


def probe_durability(base: Path | None = None) -> str:
    """Classify the durability of the mailbox base — one of ``DURABILITY_*``.

    Pure probe (no logging, no state). ``base`` defaults to :func:`resolve`.
    """
    base = resolve() if base is None else base
    if not _is_writable(base):
        return DURABILITY_UNWRITABLE
    on_root = _is_on_root_device(base)
    if on_root is None:
        # Undeterminable device — treat as non-durable so the operator is warned
        # rather than lulled. Writable but unverifiable is not "durable".
        return DURABILITY_EPHEMERAL
    return DURABILITY_EPHEMERAL if on_root else DURABILITY_DURABLE


def last_durability_status() -> str:
    """Last status from :func:`verify_durability` — for observability surfacing."""
    return _last_durability_status


def verify_durability() -> str:
    """Probe the durable substrate at kernel-on boot; LOUD-warn if not durable.

    Idempotent, side-effecting only in that it logs and records
    :func:`last_durability_status`. No-op returning :data:`DURABILITY_NA` when
    the kernel is off (the legacy path is unaffected). Never raises — a probe
    failure must not crash boot; the whole point is to make a silent failure
    observable, not to introduce a new one.
    """
    global _last_durability_status
    if not kernel_enabled():
        _last_durability_status = DURABILITY_NA
        return _last_durability_status

    base = resolve()
    try:
        status = probe_durability(base)
    except Exception:  # pragma: no cover - defensive; probe is already OSError-safe
        logger.exception("mailbox durability: probe failed for %s", base)
        _last_durability_status = DURABILITY_EPHEMERAL
        return _last_durability_status

    _last_durability_status = status
    if status == DURABILITY_DURABLE:
        logger.info("mailbox durability: OK — %s is a distinct persistent mount", base)
    elif status == DURABILITY_EPHEMERAL:
        logger.error(
            "mailbox durability: EPHEMERAL — %s is on the root filesystem, NOT a "
            "persistent volume. Kernel-on durable state (memory, goal-state, "
            "delegation tombstones, task-queue ledger) WILL BE LOST on instance "
            "recreate / auto-heal. Mount a persistent volume at %s (enable the "
            "per-org data volume). See cp#326.",
            base,
            base,
        )
    else:  # DURABILITY_UNWRITABLE
        logger.error(
            "mailbox durability: UNWRITABLE — cannot write under %s. Kernel-on "
            "durable state cannot persist; check the mount and permissions.",
            base,
        )
    return _last_durability_status


def memory_dir() -> Path:
    """Return the durable *memory* directory (``<base>/memory``).

    This is the WRITE target the memory-write-path reconciliation points every
    writer at (agents_md, append-to-memory hook, consolidation snapshot) and
    the READ source ``prompt.py`` layers on top of the param-rendered
    ``/configs`` system prompt. Created ``0700`` when the kernel is on.

    Kernel OFF still returns ``<legacy>/memory`` but callers gate on
    :func:`kernel_enabled` and keep their legacy targets, so this is only
    materialized on the kernel-on path.
    """
    path = resolve() / "memory"
    if kernel_enabled():
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass
    return path


def memory_file(filename: str) -> Path:
    """Path to a single durable memory file under :func:`memory_dir`."""
    return memory_dir() / filename


def snapshot_path() -> Path:
    """Durable pre-stop snapshot path under the mailbox base (kernel-on)."""
    return resolve() / ".agent_snapshot.json"


#: Legacy (kernel-off) default for the delegation-results queue. Kept on tmpfs
#: ``/tmp`` so kernel-off behavior is byte-identical to the pre-migration
#: default (``heartbeat.DELEGATION_RESULTS_FILE`` historically defaulted here).
_DEFAULT_DELEGATION_RESULTS_FILE = "/tmp/delegation_results.jsonl"


def delegation_results_file() -> str:
    """Resolve the delegation-results queue path — writer and reader must AGREE.

    RC #203 durability. The heartbeat appends a harvested delegation result to
    this queue and only THEN commits a DURABLE ``(id, status)`` harvest tombstone
    (see ``heartbeat._commit_harvested`` / ``_harvest_tombstone_file``). If the
    queue lived on tmpfs ``/tmp`` while the tombstone lives on the durable mailbox
    volume, a container restart AFTER the append but BEFORE the executor consumes
    the queue would lose the queued result while the tombstone survives — so the
    restart re-harvest sees the tombstone and PERMANENTLY suppresses the result.
    Kernel-ON the queue therefore lives on the durable mailbox volume, right
    beside the tombstone, so ``commit tombstone after append`` is genuinely
    durable across restart.

    Resolution (mirrors :func:`executor_helpers.tool_activity_file`):
      - Explicit ``DELEGATION_RESULTS_FILE`` env always wins (adapters + tests).
      - Kernel OFF: the legacy ``/tmp`` default — byte-identical.
      - Kernel ON:  ``<mailbox>/delegation_results.jsonl`` on the durable volume.
    """
    if not kernel_enabled():
        # Byte-identical legacy behavior: env override or the /tmp default.
        return os.environ.get(
            "DELEGATION_RESULTS_FILE", _DEFAULT_DELEGATION_RESULTS_FILE
        )
    # Kernel ON: an explicit override still wins; else the durable mailbox queue.
    override = os.environ.get("DELEGATION_RESULTS_FILE", "").strip()
    if override:
        return override
    return str(resolve() / "delegation_results.jsonl")
