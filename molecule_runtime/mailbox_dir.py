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

Gating (native default — kernel ON)
-----------------------------------
``MOLECULE_MAILBOX_KERNEL`` unset / true (DEFAULT)
    The mailbox kernel is NATIVE runtime behavior (operator ruling 2026-07-13,
    task #219): :func:`resolve` returns ``/workspace/.molecule`` (overridable
    via ``MOLECULE_MAILBOX_DIR``), created ``0700`` so per-file ``0600`` perms
    are not undermined by a world-readable parent. First kernel-on boot runs
    :func:`migrate_legacy_state` so pre-existing workspaces carry their inbox
    cursor / delegation tombstones / queued results over — no inbound replay,
    no delegation re-harvest (design §7.2).

``MOLECULE_MAILBOX_KERNEL`` explicitly false (``0`` / ``false`` / ``no`` / ``off``)
    Emergency escape hatch: :func:`resolve` returns the LEGACY location
    (``configs_dir.resolve()``) and every kernel call site behaves exactly as
    the pre-kernel runtime did (static idle_prompt loop, legacy state paths).

Not cached: the flag + one ``mkdir`` are cheap and reading ``os.environ`` live
keeps tests that monkeypatch ``MOLECULE_MAILBOX_KERNEL`` between cases working
without a reset hook.
"""
from __future__ import annotations

import json
import logging
import os
from importlib import resources
from pathlib import Path

import molecule_runtime.configs_dir as configs_dir

logger = logging.getLogger(__name__)

#: Env flag for the mailbox kernel (durable-volume state). Default ON — the
#: kernel is native runtime behavior. Set explicitly falsy ("0"/"false"/"no"/
#: "off") as an emergency escape hatch back to the legacy paths + static idle
#: loop.
KERNEL_FLAG_ENV = "MOLECULE_MAILBOX_KERNEL"

#: Override for the durable base dir (kernel-on only). Defaults to the durable
#: workspace volume so state survives restart / auto-heal.
MAILBOX_DIR_ENV = "MOLECULE_MAILBOX_DIR"

_DEFAULT_MAILBOX_DIR = "/workspace/.molecule"

_FALSY = frozenset({"0", "false", "no", "off"})


def kernel_enabled() -> bool:
    """True unless ``MOLECULE_MAILBOX_KERNEL`` is set explicitly falsy.

    The mailbox kernel is NATIVE runtime behavior (default ON, operator ruling
    2026-07-13); the env var survives only as an emergency opt-out. Read live
    (not cached) so a test toggling the env var between cases sees the change
    without a reset hook. This is the SINGLE flag every kernel call site
    consults; when it returns False the runtime is byte-identical to the
    pre-kernel behavior (legacy paths, static idle loop).
    """
    return os.environ.get(KERNEL_FLAG_ENV, "").strip().lower() not in _FALSY


def _raw_base() -> Path:
    """The configured kernel base (env override or default) — NO usability
    fallback. :func:`verify_durability` probes THIS so an unusable substrate
    still surfaces as UNWRITABLE even though :func:`resolve` degrades."""
    return Path(os.environ.get(MAILBOX_DIR_ENV, "").strip() or _DEFAULT_MAILBOX_DIR)


#: Per-process usability cache, keyed by the raw base path. Live env reads
#: keep test toggling working (a different MOLECULE_MAILBOX_DIR is a different
#: key); within one key the probe result is stable for the process lifetime so
#: every caller (cursor, queue, memory, digest state) agrees on ONE base.
_usable_cache: dict[str, bool] = {}
_degraded_logged: set[str] = set()


def _base_usable(base: Path) -> bool:
    """True iff ``base`` can be created and written (cached per path)."""
    key = str(base)
    hit = _usable_cache.get(key)
    if hit is not None:
        return hit
    ok = _is_writable(base)
    _usable_cache[key] = ok
    return ok


def base_degraded() -> bool:
    """True when the kernel is ON but the mailbox base is unusable, so
    :func:`resolve` is returning the LEGACY configs dir instead. Callers with
    a legacy location that is NOT the configs dir (the /tmp delegation queue)
    consult this to fall all the way back to their own legacy path."""
    return kernel_enabled() and not _base_usable(_raw_base())


def resolve() -> Path:
    """Return the durable base directory for agent state.

    Kernel ON  -> ``/workspace/.molecule`` (or ``MOLECULE_MAILBOX_DIR``),
                  created ``0700`` — WHEN that base is actually usable.
                  On a substrate where it cannot be created/written (root:755
                  named volume — core#4295; an operator host running the
                  standalone ``molecule-mcp`` with no /workspace at all) the
                  kernel DEGRADES to the legacy configs dir instead of
                  scattering failed writes: cursors/tombstones keep their
                  pre-kernel home, nothing is lost, and
                  :func:`verify_durability` still reports the raw base
                  UNWRITABLE so the substrate gets fixed.
    Kernel OFF -> ``configs_dir.resolve()`` — the LEGACY location, unchanged.
    """
    if not kernel_enabled():
        # Byte-identical opt-out: durable state stays exactly where it lived
        # before the kernel.
        return configs_dir.resolve()

    base = _raw_base()
    if _base_usable(base):
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass
        return base
    legacy = configs_dir.resolve()
    key = str(base)
    if key not in _degraded_logged:
        _degraded_logged.add(key)
        logger.error(
            "mailbox: base %s is not writable — DEGRADING durable paths to the "
            "legacy configs dir (%s). Kernel state stays at its pre-kernel "
            "home; fix the volume ownership/mount to get durable-volume "
            "semantics (core#4295).",
            base,
            legacy,
        )
    return legacy


# --- Durability guard --------------------------------------------------------
#
# The kernel relocates durable state onto ``/workspace`` on the assumption that
# ``/workspace`` is durable. That is a DEPLOYMENT + PROVIDER property, not a
# given, and it holds in TWO provider-agnostic ways:
#   1. a distinct persistent MOUNT (AWS EBS data volume, Docker named volume) —
#      zero-loss across recreate; detected by ``st_dev`` != ``/``;
#   2. R2 SNAPSHOT/RESTORE (Hetzner/GCP boot disk + the workspace-data contract):
#      ``/workspace`` is on the ephemeral boot disk (same ``st_dev`` as ``/``)
#      but is tar+zstd'd to object storage on a timer and restored before the
#      container mounts it — durable across recreate, but PERIODIC (bounded loss).
# On a flavor where NEITHER holds, ``/workspace`` is the ephemeral root disk and
# kernel state is silently lost on the next recreate / auto-heal (cp#326).
# :func:`verify_durability` probes the resolved base once at kernel-on boot and
# classifies it so a non-durable deployment is caught and surfaced rather than
# losing data quietly. The snapshot signal is read from the vendored
# ``workspace-data`` contract SSOT (``molecule_runtime/contracts``).

#: Base sits on a distinct, persistent mounted volume (different ``st_dev`` from
#: ``/``) and is writable — durable across instance recreate / auto-heal.
DURABILITY_DURABLE = "durable"
#: Base is on the boot disk (same ``st_dev`` as ``/``) but durable via the R2
#: snapshot/restore mechanism (workspace-data contract) — durable across recreate
#: but PERIODIC: state since the last snapshot is lost on a hard recreate.
DURABILITY_SNAPSHOT = "snapshot-durable"
#: Base is writable but lives on the SAME device as ``/`` (the root filesystem),
#: i.e. it is a plain directory on the ephemeral root disk with NO snapshot
#: backing — state is lost on instance recreate / auto-heal. The case to catch.
DURABILITY_EPHEMERAL = "ephemeral"
#: Base cannot be written (read-only mount, permissions, missing parent) — the
#: kernel's durable writes will fail outright.
DURABILITY_UNWRITABLE = "unwritable"
#: Kernel off (or probe skipped) — the durability guard does not apply.
DURABILITY_NA = "n/a"

_PROBE_FILENAME = ".molecule-durability-probe"

#: Vendored workspace-data contract SSOT (byte-for-byte mirror of molecule-ai-sdk
#: contracts/workspace-data; see molecule_runtime/contracts/PROVENANCE.md). Read
#: for the snapshot-durability signal — the persisted-path set and the box env
#: var CP injects when it wires R2 snapshot/restore for a workspace.
_WORKSPACE_DATA_RESOURCE = "contracts/workspace-data.contract.json"
_workspace_data_cache: dict | None = None


def _workspace_data() -> dict:
    """Load the vendored workspace-data contract instance (cached). ``{}`` on any
    failure — a missing/corrupt mirror must never crash the guard; it just means
    no snapshot-durability credit (the conservative, LOUD-warn direction)."""
    global _workspace_data_cache
    if _workspace_data_cache is None:
        try:
            raw = (
                resources.files("molecule_runtime")
                .joinpath(_WORKSPACE_DATA_RESOURCE)
                .read_text(encoding="utf-8")
            )
            loaded = json.loads(raw)
            _workspace_data_cache = loaded if isinstance(loaded, dict) else {}
        except Exception:  # pragma: no cover - defensive I/O
            _workspace_data_cache = {}
    return _workspace_data_cache


def _path_under(base: Path, root: str) -> bool:
    """True iff ``base`` is at or under ``root`` (normalized string compare, no I/O)."""
    b = os.path.normpath(str(base))
    r = os.path.normpath(root)
    return b == r or b.startswith(r.rstrip(os.sep) + os.sep)


def _snapshot_durable_signal(base: Path) -> bool:
    """In-container signal that ``base`` is durable via R2 snapshot/restore.

    Per the workspace-data contract: the base lives under a ``persisted_paths``
    entry AND the CP-injected snapshot-PUT env var (``box_env.snapshot_uri``) is
    present — proving CP wired R2 snapshot for this workspace. The host-side
    ``ws-snapshot-disabled`` marker is NOT container-visible, so this credits
    *configured* snapshot-durability, not live health (a box whose restore failed
    still reads as snapshot-durable — an acceptable, rare miss vs. a false EPHEMERAL
    warning on every healthy boot-disk box). Contract-driven: no hardcoded paths.
    """
    wd = _workspace_data()
    snap_env = (wd.get("box_env") or {}).get("snapshot_uri")
    paths = wd.get("persisted_paths")
    if not snap_env or not isinstance(paths, list) or not paths:
        return False  # contract unavailable → no credit (stay conservative)
    if not os.environ.get(snap_env, "").strip():
        return False  # CP did not wire an R2 snapshot PUT for this workspace
    return any(_path_under(base, p) for p in paths if isinstance(p, str))

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
    if on_root is False:
        # Distinct persistent mount (EBS data volume / Docker named volume).
        return DURABILITY_DURABLE
    # on_root True (boot disk) or None (undeterminable) is NOT a live durable
    # mount — but R2 snapshot/restore may still make it durable (Hetzner/GCP).
    # Credit snapshot-durability when the workspace-data contract signals it,
    # else it is genuinely ephemeral (warn).
    if _snapshot_durable_signal(base):
        return DURABILITY_SNAPSHOT
    return DURABILITY_EPHEMERAL


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

    # Probe the RAW configured base, not resolve(): when the base is unusable
    # resolve() degrades to the legacy configs dir (which is usually writable)
    # and probing THAT would hide exactly the UNWRITABLE condition this guard
    # exists to surface.
    base = _raw_base()
    try:
        status = probe_durability(base)
    except Exception:  # pragma: no cover - defensive; probe is already OSError-safe
        logger.exception("mailbox durability: probe failed for %s", base)
        _last_durability_status = DURABILITY_EPHEMERAL
        return _last_durability_status

    _last_durability_status = status
    if status == DURABILITY_DURABLE:
        logger.info("mailbox durability: OK — %s is a distinct persistent mount", base)
    elif status == DURABILITY_SNAPSHOT:
        snap_env = (_workspace_data().get("box_env") or {}).get(
            "snapshot_uri", "MOLECULE_WORKSPACE_SNAPSHOT_URI"
        )
        logger.info(
            "mailbox durability: OK (snapshot) — %s is on the boot disk but is durable "
            "via R2 snapshot/restore (%s wired); state is restored on recreate. NOTE: "
            "snapshot durability is PERIODIC — state written since the last snapshot is "
            "lost on a hard recreate (unlike a live persistent mount).",
            base,
            snap_env,
        )
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


# --- Legacy-state migrator (design §7.2 — replay safety) -------------------
#
# The kernel default flipped ON 2026-07-13 (task #219 operator ruling: native
# ability, not flag-gated). For a workspace that previously ran kernel-OFF,
# durable state lives at the LEGACY locations: the inbox cursor + delegation
# tombstones under ``configs_dir.resolve()`` and the delegation-results queue
# on ``/tmp``. Resolving the mailbox root to ``/workspace/.molecule`` without
# carrying that state over orphans it: an empty inbox cursor ⇒ FULL INBOUND
# REPLAY of the historical backlog, missing tombstones ⇒ DELEGATION RE-HARVEST
# — exactly the 2026-06-29 re-narration incident class. This migrator runs on
# the first kernel-on boot (marker-gated, idempotent) BEFORE the inbox poller
# and heartbeat start, copying the legacy files into the mailbox root.
#
# Copy — never move: the legacy files stay in place so an emergency
# ``MOLECULE_MAILBOX_KERNEL=0`` opt-out still finds its state. Never clobber:
# a file already present at the mailbox root is newer kernel-authored state
# and always wins. The marker is committed LAST (atomic tmp+rename), so a
# crash mid-migration re-runs the whole (idempotent) pass next boot.

_MIGRATED_MARKER = ".legacy_state_migrated"

#: Legacy basenames migrated from the configs dir. ``.mcp_inbox_cursor`` also
#: covers the per-workspace suffixed variants (``.mcp_inbox_cursor_<wsid>``),
#: matched by prefix below.
_LEGACY_CONFIG_BASENAMES = (
    ".mcp_inbox_cursor",
    ".delegation_tombstones",
    ".agent_snapshot.json",
)

#: Legacy memory-snapshot files (kernel-off writers targeted the configs dir;
#: kernel-on readers ONLY consult ``<mailbox>/memory`` — prompt.py's stale-
#: shadow rule). Without carrying these over, a workspace's accumulated memory
#: silently vanishes from the system prompt on the first kernel-on boot. Kept
#: in lockstep with ``prompt.DEFAULT_MEMORY_SNAPSHOT_FILES`` (duplicated here
#: to avoid a prompt->mailbox_dir import cycle; the migrator test pins the two
#: tuples equal).
_LEGACY_MEMORY_BASENAMES = (
    "MEMORY.md",
    "USER.md",
    "CLAUDE.md",
    "AGENTS.md",
    "SOUL.md",
)


def _copy_0600(src: Path, dst: Path, migrated: list[str]) -> None:
    """Atomic 0600 copy of ``src`` to ``dst`` unless ``dst`` already exists.

    0600 via os.open (not umask-default write_bytes): the sources include
    relay-delivered 0600 files; migrated copies must not come out looser.
    """
    if not src.is_file() or dst.exists():
        return
    tmp = dst.with_name(dst.name + ".migrating")
    data = src.read_bytes()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, dst)
    migrated.append(src.name)


def _is_cursor_family(name: str) -> bool:
    """The prefix-matched cursor files, excluding crash leftovers."""
    return (
        name.startswith(".mcp_inbox_cursor")
        and not name.endswith(".tmp")
        and not name.endswith(".migrating")
    )


def _legacy_pairs(base: Path, legacy: Path) -> list[tuple[Path, Path]]:
    """(legacy source, mailbox destination) pairs for every migrated file."""
    pairs: list[tuple[Path, Path]] = []
    if legacy.is_dir():
        for entry in sorted(legacy.iterdir()):
            if _is_cursor_family(entry.name) or entry.name in _LEGACY_CONFIG_BASENAMES:
                pairs.append((entry, base / entry.name))
        for name in _LEGACY_MEMORY_BASENAMES:
            pairs.append((legacy / name, base / "memory" / name))
    # The pre-stop writer historically hardcoded /configs (lib/pre_stop.py),
    # which may differ from configs_dir.resolve() in nonstandard layouts.
    hardcoded_snap = Path("/configs/.agent_snapshot.json")
    if os.path.normpath(str(legacy)) != "/configs":
        pairs.append((hardcoded_snap, base / ".agent_snapshot.json"))
    # Legacy /tmp cursors + queue. Skipped when an env override pins the path:
    # an override is read verbatim in BOTH kernel modes, so the flip does not
    # move that file and a migrated copy would be dead data.
    if not os.environ.get("DELEGATION_RESULTS_FILE", "").strip():
        pairs.append(
            (Path(_DEFAULT_DELEGATION_RESULTS_FILE), base / "delegation_results.jsonl")
        )
    if not os.environ.get("DELEGATION_ACTIVITY_CURSOR_FILE", "").strip():
        pairs.append(
            (
                Path("/tmp/delegation_activity_cursor"),
                base / ".delegation_activity_cursor",
            )
        )
    return pairs


def migrate_legacy_state() -> bool:
    """Carry legacy kernel-off durable state into the mailbox root.

    First kernel-on boot (marker absent): copy every legacy file that has no
    mailbox counterpart — copy-not-move, never-clobber, marker committed last
    (atomic). Later boots (marker present): a light RECONCILE pass — any
    legacy file strictly NEWER (mtime) than its mailbox counterpart is copied
    over it. That heals the emergency flip-flop (kernel → =0 → kernel): during
    the opt-out window the legacy paths were the live writers, so their newer
    state must win on re-enable or stale mailbox copies silently shadow it.

    Returns True when any pass copied at least one file or committed the
    marker. Never raises — a migration failure must not crash boot. NOTE the
    failure mode honestly: boot continues and kernel writers start writing at
    the base, so a file the kernel touches before the next retry stays owned
    by the kernel copy (never-clobber) — the retry only heals files the
    kernel has not yet written.
    """
    if not kernel_enabled():
        return False
    base = resolve()
    legacy = configs_dir.resolve()
    if os.path.normpath(str(base)) == os.path.normpath(str(legacy)):
        return False  # degraded resolve or MOLECULE_MAILBOX_DIR at the legacy dir
    marker = base / _MIGRATED_MARKER
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        (base / "memory").mkdir(parents=True, exist_ok=True, mode=0o700)
        migrated: list[str] = []

        if marker.exists():
            # Reconcile: prefer strictly-newer legacy state (flip-flop heal).
            for src, dst in _legacy_pairs(base, legacy):
                try:
                    if not src.is_file():
                        continue
                    if dst.exists() and src.stat().st_mtime <= dst.stat().st_mtime:
                        continue
                    tmp = dst.with_name(dst.name + ".migrating")
                    data = src.read_bytes()
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)
                    os.replace(tmp, dst)
                    migrated.append(src.name)
                except OSError:
                    continue
            if migrated:
                logger.warning(
                    "mailbox reconcile: legacy copies were NEWER than their "
                    "mailbox counterparts (opt-out window writes?) — carried "
                    "over: %s",
                    ", ".join(migrated),
                )
            return bool(migrated)

        pairs = _legacy_pairs(base, legacy)
        # Marker rule keys on the CONFIGS-side sources only: /configs may not
        # be materialized yet THIS boot (asset-fetcher window; resolve()
        # re-creates it EMPTY), and burning the marker then would deny later-
        # arriving configs state its one migration. The /tmp legs (queue,
        # activity cursor) re-run harmlessly under no-clobber either way.
        legacy_str = os.path.normpath(str(legacy)) + os.sep
        configs_had_any = any(
            src.is_file()
            for src, _ in pairs
            if os.path.normpath(str(src)).startswith(legacy_str)
        )
        for src, dst in pairs:
            _copy_0600(src, dst, migrated)

        if not configs_had_any:
            # Nothing migratable existed — either a genuinely fresh workspace
            # or /configs not yet materialized THIS boot (asset-fetcher
            # window; configs_dir.resolve() re-creates the dir EMPTY, so
            # "absent" reads as "empty" here). Do not burn the one-shot
            # marker: legacy state appearing on a later boot must still get
            # its one migration. Re-running this empty pass each boot is a
            # few stat calls.
            return False

        tmp_marker = base / (_MIGRATED_MARKER + ".migrating")
        tmp_marker.write_text(json.dumps({"migrated": migrated}), encoding="utf-8")
        os.replace(tmp_marker, marker)
        if migrated:
            logger.info(
                "mailbox migrate: carried legacy durable state into %s: %s",
                base,
                ", ".join(migrated),
            )
        return True
    except OSError:
        logger.exception(
            "mailbox migrate: FAILED to carry legacy state into %s (marker not "
            "committed; a retry runs next boot, but files the kernel writes in "
            "the meantime keep their kernel copy — design §7.2 replay risk).",
            base,
        )
        return False


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
    if base_degraded():
        # The mailbox base is unusable, so resolve() is returning the configs
        # dir — but THIS file's legacy home is /tmp, not configs. Appending to
        # a possibly-unwritable configs dir would hard-break delegation-result
        # delivery (heartbeat append raises → self-wake never sent → tombstone
        # never committed). Fall back to the exact legacy queue instead: the
        # /tmp path always works and reader + writer resolve it identically.
        return _DEFAULT_DELEGATION_RESULTS_FILE
    return str(resolve() / "delegation_results.jsonl")
