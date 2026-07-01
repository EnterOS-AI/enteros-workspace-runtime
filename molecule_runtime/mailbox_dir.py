"""Resolve the durable *mailbox* directory for the workspace runtime.

Single gating point for the ``MOLECULE_MAILBOX_KERNEL`` migration. All durable
agent state — inbox cursor, pre-stop snapshot, delegation cursor + result
tombstones, consolidated agent memory — belongs on the PERSISTENT WORKSPACE
VOLUME so it survives a container restart / auto-heal. That volume is
``/workspace`` (mounted durable in every deployment); the runtime writes this
state under ``/workspace/.molecule`` — the same convention chat-uploads and
inbox attachments already use (see ``executor_helpers.CHAT_UPLOADS_DIR`` and
``INBOX_ATTACHMENTS_DIR``).

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

import os
from pathlib import Path

import molecule_runtime.configs_dir as configs_dir

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
        # boot. Callers that actually write will surface a clear error.
        pass
    return base


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
