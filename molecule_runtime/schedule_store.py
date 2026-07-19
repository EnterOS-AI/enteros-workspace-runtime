"""Validated, volume-authoritative schedule store (Option A).

The store owns the schedule grid a ``kind: trigger`` scheduler plugin fires
from.  It is the *write* side of the runtime schedule API: Canvas / admin / the
webhook poke go through here, and the trigger daemon reads the same grid file.
Every write is validated against the SDK ``schedule`` contract (the vendored
``contracts/schedule.schema.json``) plus the byte-level caps the schema can only
approximate, and each cron is checked against the ``cron`` contract via
:func:`molecule_runtime.cronspec.validate` so an unschedulable expression is
rejected at write time, never at fire time.

The grid holds *definition only*.  Firing bookkeeping (last run, next run, run
count, ...) is the daemon's, in a separate state file on the same volume, so an
edit here never races the daemon's fire state.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft7Validator

from molecule_runtime import cronspec


MAX_ENTRIES = 100
MAX_CRON_LEN = 128
MAX_PROMPT_BYTES = 16384

# Definition fields the grid persists (engine-state bookkeeping is excluded).
_ENTRY_FIELDS = ("name", "cron", "timezone", "prompt", "enabled", "source")


class ScheduleError(ValueError):
    """A schedule entry or grid violated the contract. Safe to surface to callers."""


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    text = (
        resources.files("molecule_runtime")
        .joinpath("contracts/schedule.schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


@lru_cache(maxsize=1)
def _entry_validator() -> Draft7Validator:
    schema = _schema()
    entry_schema = dict(schema["$defs"]["scheduleEntry"])
    # Resolve against the same base so any future internal $ref keeps working.
    entry_schema["$defs"] = schema.get("$defs", {})
    return Draft7Validator(entry_schema)


def validate_entry(entry: Any) -> dict[str, Any]:
    """Validate one entry against the contract + caps + cron grammar.

    Returns a normalized copy (only contract fields, defaults applied). Raises
    :class:`ScheduleError` with a stable, caller-safe message on any violation.
    """
    if not isinstance(entry, dict):
        raise ScheduleError("schedule entry must be an object")

    errors = sorted(_entry_validator().iter_errors(entry), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = ".".join(str(p) for p in first.path) or "entry"
        raise ScheduleError(f"invalid schedule ({where}): {first.message}")

    # Byte cap: JSON-Schema maxLength counts code points, the contract is bytes.
    prompt = entry["prompt"]
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ScheduleError(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")

    timezone = entry.get("timezone") or "UTC"
    # Cron must be a schedulable expression in this timezone (cron contract SSOT).
    try:
        cronspec.validate(entry["cron"], timezone)
    except cronspec.CronError as exc:
        raise ScheduleError(f"invalid cron: {exc}") from exc

    normalized: dict[str, Any] = {
        "name": entry["name"],
        "cron": entry["cron"],
        "timezone": timezone,
        "prompt": prompt,
        "enabled": bool(entry.get("enabled", True)),
    }
    if "source" in entry:
        normalized["source"] = entry["source"]
    return normalized


def validate_grid(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Validate a whole grid: every entry valid, unique names, within the count cap."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        norm = validate_entry(entry)
        if norm["name"] in seen:
            raise ScheduleError(f"duplicate schedule name: {norm['name']!r}")
        seen.add(norm["name"])
        normalized.append(norm)
    if len(normalized) > MAX_ENTRIES:
        raise ScheduleError(f"too many schedules: {len(normalized)} > {MAX_ENTRIES}")
    return normalized


class ScheduleStore:
    """CRUD over the grid file on the persisted volume. Not concurrency-safe across
    processes; the runtime API is the single writer, the daemon is a reader."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    # --- read ---------------------------------------------------------------
    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = self._parse(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ScheduleError(f"unreadable schedule grid: {exc}") from exc
        if isinstance(raw, dict):
            raw = raw.get("schedules", [])
        if not isinstance(raw, list):
            raise ScheduleError("schedule grid must be a list or {schedules: [...]}")
        return validate_grid(raw)

    def list(self) -> list[dict[str, Any]]:
        return self.load()

    def get(self, name: str) -> dict[str, Any] | None:
        for entry in self.load():
            if entry["name"] == name:
                return entry
        return None

    # --- write --------------------------------------------------------------
    def create(self, entry: Any) -> dict[str, Any]:
        norm = validate_entry(entry)
        # A schedule created through this store (the API) is user-owned. Stamping
        # source='runtime' is what lets reconcile-on-boot seeding (upsert_template)
        # tell it apart from a template-seeded entry and never clobber it.
        norm["source"] = "runtime"
        entries = self.load()
        if any(e["name"] == norm["name"] for e in entries):
            raise ScheduleError(f"schedule already exists: {norm['name']!r}")
        if len(entries) + 1 > MAX_ENTRIES:
            raise ScheduleError(f"too many schedules: {MAX_ENTRIES} max")
        entries.append(norm)
        self._write(entries)
        # Explicit (re)create clears any prior tombstone for this name.
        self._remove_tombstone(norm["name"])
        return norm

    def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        entries = self.load()
        for index, entry in enumerate(entries):
            if entry["name"] != name:
                continue
            merged = {k: entry[k] for k in _ENTRY_FIELDS if k in entry}
            for key, value in patch.items():
                if key == "name" and value != name:
                    raise ScheduleError("renaming a schedule is not supported")
                if value is not None:
                    merged[key] = value
            norm = validate_entry(merged)
            # A user edit takes ownership: a previously template-seeded entry the
            # user changes becomes source='runtime' so the next reconcile-on-boot
            # seeding preserves the edit instead of reverting it to the template
            # definition (the entry's prior source in `merged` is overridden here).
            norm["source"] = "runtime"
            entries[index] = norm
            self._write(entries)
            return norm
        raise ScheduleError(f"no such schedule: {name!r}")

    def set_enabled(self, name: str, value: bool) -> dict[str, Any]:
        """Flip ``enabled`` WITHOUT restamping ``source`` (RFC invariant #8).

        This is the auto-disable engine's single writer. It must NOT reuse
        :meth:`update`, which force-stamps ``source='runtime'`` to mark a user
        edit — doing so on an engine-driven disable would corrupt a
        template-seeded schedule's provenance and defeat reconcile-on-boot
        (the entry would stop matching its template on the next restart). A
        health-driven disable is not a user edit, so the source is preserved
        verbatim. No-op (still returns the entry) if the value already matches.
        """
        entries = self.load()
        for index, entry in enumerate(entries):
            if entry["name"] != name:
                continue
            if bool(entry.get("enabled", True)) == bool(value):
                return entry
            merged = {k: entry[k] for k in _ENTRY_FIELDS if k in entry}
            merged["enabled"] = bool(value)
            norm = validate_entry(merged)  # preserves source (never restamped)
            entries[index] = norm
            self._write(entries)
            return norm
        raise ScheduleError(f"no such schedule: {name!r}")

    def delete(self, name: str) -> bool:
        entries = self.load()
        remaining = [e for e in entries if e["name"] != name]
        if len(remaining) == len(entries):
            return False
        self._write(remaining)
        # Tombstone the name so reconcile-on-boot seeding does not resurrect a
        # template schedule the user deliberately deleted (it would otherwise be
        # re-added, and re-fire, on every restart). Harmless for purely-user
        # schedules — no template ships their name. A later create() of the same
        # name clears the tombstone (the user explicitly wants it back).
        self._add_tombstone(name)
        return True

    def replace_all(self, entries: Iterable[Any]) -> list[dict[str, Any]]:
        """Validate and atomically replace the whole grid (org/template seeding)."""
        normalized = validate_grid(list(entries))
        self._write(normalized)
        return normalized

    def upsert_template(self, template_entries: Iterable[Any]) -> list[dict[str, Any]]:
        """Additive template seeding that preserves user edits (reconcile-on-boot).

        The runtime seeds its own grid from the template-delivered ``schedules.yaml``
        on boot, replacing core's old ``workspace_schedules`` seeding (P4). RFC
        invariant: a template re-provision must never clobber a user's own edits.
        So each delivered entry is upserted keyed on ``name`` and stamped
        ``source='template'``; any *live* entry whose source is not ``'template'``
        (a runtime/API-created or untagged user entry) is preserved untouched, even
        when it shares a name with a template entry.

        Additive-only: a template entry that was *removed* from the delivered file
        is NOT pruned here (mirrors the old core additive-upsert seeding). A
        template entry the user *deleted* is tombstoned (see :meth:`delete`) and is
        NOT re-seeded. Returns the merged grid; a validation failure on any
        delivered entry — including the combined grid exceeding ``MAX_ENTRIES`` —
        raises :class:`ScheduleError` and leaves the prior grid intact
        (``replace_all`` is atomic). Callers that must not fail boot handle that.
        """
        tombstoned = self._load_tombstones()
        by_name: dict[str, dict[str, Any]] = {e["name"]: e for e in self.load()}
        for raw in template_entries:
            norm = validate_entry(raw)
            norm["source"] = "template"
            if norm["name"] in tombstoned:
                # The user deleted this template schedule — do not resurrect it.
                continue
            existing = by_name.get(norm["name"])
            if existing is None or existing.get("source") == "template":
                # New template entry, or refresh of a template-owned one.
                by_name[norm["name"]] = norm
            # else: a runtime/user-owned entry with this name — preserve it.
        return self.replace_all(list(by_name.values()))

    # --- internals ----------------------------------------------------------
    def _write(self, entries: list[dict[str, Any]]) -> None:
        payload = {"schedules": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                self._dump(payload, handle)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _parse(self, text: str) -> Any:
        if self.path.suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError:
                yaml = None  # type: ignore
            if yaml is not None:
                try:
                    return yaml.safe_load(text)
                except yaml.YAMLError as exc:
                    # Re-raise as ValueError so load()'s `except (ValueError,
                    # OSError)` wraps it into ScheduleError — a malformed grid is
                    # a contract violation, not an escape hatch past callers'
                    # (ScheduleError, OSError) handling (e.g. the boot seeder).
                    raise ValueError(f"invalid YAML: {exc}") from exc
        return json.loads(text)

    def _dump(self, payload: Any, handle: Any) -> None:
        if self.path.suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore

                yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
                return
            except ImportError:
                pass
        json.dump(payload, handle, indent=2, sort_keys=False)

    # --- tombstones (user-deleted template schedules) -----------------------
    # A small JSON set of names beside the grid. Only reconcile-on-boot seeding
    # consults it; ordinary CRUD ignores it. Kept separate from the grid so a
    # tombstone write never touches the definition file the daemon reads.
    def _tombstones_path(self) -> Path:
        return self.path.parent / "schedule-tombstones.json"

    def _load_tombstones(self) -> set[str]:
        path = self._tombstones_path()
        if not path.is_file():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return set()  # a corrupt tombstone file must never break seeding
        names = raw.get("deleted", []) if isinstance(raw, dict) else raw
        return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()

    def _write_tombstones(self, names: set[str]) -> None:
        path = self._tombstones_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"deleted": sorted(names)}, handle, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _add_tombstone(self, name: str) -> None:
        names = self._load_tombstones()
        if name not in names:
            names.add(name)
            self._write_tombstones(names)

    def _remove_tombstone(self, name: str) -> None:
        names = self._load_tombstones()
        if name in names:
            names.discard(name)
            self._write_tombstones(names)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


__all__ = [
    "MAX_ENTRIES",
    "MAX_CRON_LEN",
    "MAX_PROMPT_BYTES",
    "ScheduleError",
    "ScheduleStore",
    "validate_entry",
    "validate_grid",
]
