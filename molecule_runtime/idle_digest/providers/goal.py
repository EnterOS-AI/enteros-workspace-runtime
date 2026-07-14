"""The goal-state digest provider — the lowest-tier background objective (the loop).

The goal is the standing directive an idle agent works when nothing above it is
pending. Its one non-obvious job is staying alive under the delta gate: a static
goal never changes, so a naive provider would fire once and go silent. Instead
the provider reports a **cadence band** in the envelope's ``age_band`` slot —
``due`` when the goal has not been surfaced within its cadence, ``just-included``
right after it was — so the digest re-fires at the goal's cadence (default 1 h),
never every tick and never never. To the assembler this is indistinguishable
from a sent-folder item crossing an age threshold; zero kernel special-casing.

Durable state lives in ``goal.yaml`` under the provider's mailbox folder
(kernel-ON: ``/workspace/.molecule/idle-prompt/providers/goal-state/``), with an
append-only ``goal_history.jsonl`` audit and a one-shot ``.migrated`` marker.

``set`` / ``clear`` / ``get`` are the substance the future ``goal_set`` /
``goal_clear`` / ``goal_get`` MCP tools will wrap; ``migrate_from_config`` is the
first-boot legacy migration (a controller/boot hook — not wired here). Nothing
invokes the provider until the idle controller lands; it is dormant like its
siblings.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

import yaml

from ..contract import AgeBand, Band, Contribution, PullInstruction, Urgency

GOAL_STATE_PROVIDER_ID = "goal-state"
GOAL_TIER = 7  # lowest base tier — surfaced only when nothing above is pending

# Cadence: the floor is the idle-fire threshold (a goal can't re-fire faster
# than the digest itself); the default is one hour.
CADENCE_MIN_SECONDS = 300
CADENCE_DEFAULT_SECONDS = 3600

# The fleet-persona backlog-pull directive (CTO 2026-06-22). A migrated value
# equal to this is labelled ``fleet-persona-default`` rather than a legacy pin.
FLEET_PERSONA_DEFAULT = (
    "Pull the next real backlog item: open-PR RC fixes first, then the smallest "
    "labeled-ready issue. Never fabricate work."
)

# Goal source provenance + precedence rank (higher wins; the migrator may
# overwrite a lower-ranked goal, never a higher one).
SOURCE_WORKSPACE_MCP = "workspace-mcp"
SOURCE_LEGACY_MIGRATION = "legacy-idle-prompt-migration"
SOURCE_FLEET_DEFAULT = "fleet-persona-default"
#: Provision-time bootstrap via the MOLECULE_IDLE_GOAL env var (workspace
#: secret / CP-injected). Same rank as a legacy config migration: it seeds an
#: initial objective but may never clobber an agent-set (workspace-mcp) goal.
SOURCE_ENV_BOOTSTRAP = "env-bootstrap"
_SOURCE_RANK = {
    SOURCE_FLEET_DEFAULT: 0,
    SOURCE_LEGACY_MIGRATION: 1,
    SOURCE_ENV_BOOTSTRAP: 1,
    SOURCE_WORKSPACE_MCP: 2,
}

#: Env var carrying a provision-time initial goal. Deterministic seeding
#: surface: a tenant/CP/e2e can hand a workspace its starting objective
#: WITHOUT an org-template import or an LLM tool-call round-trip.
IDLE_GOAL_ENV = "MOLECULE_IDLE_GOAL"

_SUMMARY_MAX_CHARS = 200


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


@dataclass
class GoalDoc:
    """The parsed ``goal.yaml``."""

    goal: str
    source: str
    set_by: str
    set_at: str  # ISO-8601 UTC
    cadence_seconds: int
    last_included_at: Optional[str] = None  # ISO-8601 UTC; None until first fire


@dataclass
class GoalStateProvider:
    """Reads/writes the durable goal and produces the tier-7 cadence-banded
    contribution. Owns the reserved ``goal-state`` id (official)."""

    provider_id: str = field(default=GOAL_STATE_PROVIDER_ID, init=False)
    official: bool = field(default=True, init=False)

    # injected seams (testability): the provider state dir (default resolves via
    # mailbox_dir) and the clock (epoch seconds).
    state_dir: Optional[Path] = None
    now_fn: Callable[[], float] = time.time

    # ---- paths -----------------------------------------------------------

    def _dir(self) -> Path:
        if self.state_dir is not None:
            base = Path(self.state_dir)
        else:
            from molecule_runtime import mailbox_dir

            base = mailbox_dir.resolve() / "idle-prompt" / "providers" / "goal-state"
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass  # best-effort — a read-only parent must never crash boot
        return base

    def _goal_file(self) -> Path:
        return self._dir() / "goal.yaml"

    def _history_file(self) -> Path:
        return self._dir() / "goal_history.jsonl"

    def _migrated_marker(self) -> Path:
        return self._dir() / ".migrated"

    # ---- time helpers ----------------------------------------------------

    @staticmethod
    def _iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    @staticmethod
    def _parse_iso(s: str) -> Optional[float]:
        try:
            return datetime.fromisoformat(s).timestamp()
        except (ValueError, TypeError):
            return None

    # ---- io --------------------------------------------------------------

    def _read(self) -> Optional[GoalDoc]:
        f = self._goal_file()
        if not f.exists():
            return None
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(data, dict):
            # goal.yaml is a hand-edit surface; a valid-but-non-mapping file
            # (bare string / list / scalar) is treated as corrupt = no goal,
            # never a crash (contribute must keep the idle loop alive).
            return None
        goal = str(data.get("goal", "") or "").strip()
        if not goal:
            return None
        try:
            cadence = int(data.get("cadence_seconds", CADENCE_DEFAULT_SECONDS))
        except (TypeError, ValueError):
            cadence = CADENCE_DEFAULT_SECONDS
        lia = data.get("last_included_at")
        return GoalDoc(
            goal=goal,
            source=str(data.get("source", SOURCE_WORKSPACE_MCP)),
            set_by=str(data.get("set_by", "unknown")),
            set_at=str(data.get("set_at", "")),
            cadence_seconds=max(CADENCE_MIN_SECONDS, cadence),
            last_included_at=str(lia) if lia else None,
        )

    def _write(self, doc: GoalDoc) -> None:
        f = self._goal_file()
        data: dict = {
            "goal": doc.goal,
            "source": doc.source,
            "set_by": doc.set_by,
            "set_at": doc.set_at,
            "cadence_seconds": doc.cadence_seconds,
        }
        if doc.last_included_at:
            data["last_included_at"] = doc.last_included_at
        # atomic: write temp in the same dir, then os.replace (durable state a
        # restart mid-write must not corrupt). The repo has no existing safe_dump
        # call; this introduces the idiom.
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        os.replace(tmp, f)

    def _append_history(self, action: str, info: dict) -> None:
        try:
            with self._history_file().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"action": action, **info}) + "\n")
        except OSError:
            pass  # the audit log is best-effort; never fail the operation on it

    def _touch_marker(self) -> None:
        try:
            self._migrated_marker().write_text(
                self._iso(self.now_fn()), encoding="utf-8"
            )
        except OSError:
            pass

    # ---- public logic (the MCP tools wrap these) -------------------------

    def get(self) -> Optional[dict]:
        """Full goal detail + recent history (the ``goal_get`` tool body)."""
        doc = self._read()
        if doc is None:
            return None
        return {
            "goal": doc.goal,
            "source": doc.source,
            "set_by": doc.set_by,
            "set_at": doc.set_at,
            "cadence_seconds": doc.cadence_seconds,
            "last_included_at": doc.last_included_at,
        }

    def set(
        self,
        text: str,
        *,
        cadence_seconds: Optional[int] = None,
        set_by: str = "agent",
        source: str = SOURCE_WORKSPACE_MCP,
    ) -> GoalDoc:
        """Set/replace the goal (the ``goal_set`` tool body). Resets
        ``last_included_at`` so a freshly-set goal is immediately ``due``."""
        try:
            cad = int(cadence_seconds) if cadence_seconds is not None else CADENCE_DEFAULT_SECONDS
        except (TypeError, ValueError):
            cad = CADENCE_DEFAULT_SECONDS
        doc = GoalDoc(
            goal=text.strip(),
            source=source,
            set_by=set_by,
            set_at=self._iso(self.now_fn()),
            cadence_seconds=max(CADENCE_MIN_SECONDS, cad),
            last_included_at=None,
        )
        self._write(doc)
        self._append_history(
            "set",
            {"source": source, "set_by": set_by, "at": doc.set_at, "goal": doc.goal[:_SUMMARY_MAX_CHARS]},
        )
        return doc

    def clear(self, *, set_by: str = "agent") -> None:
        """Clear the goal (the ``goal_clear`` tool body). An otherwise-quiet
        workspace then contributes nothing and the controller sleeps."""
        f = self._goal_file()
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass
        self._append_history("clear", {"set_by": set_by, "at": self._iso(self.now_fn())})

    def migrate_from_config(
        self, idle_prompt_value: Optional[str], *, set_by: str = "provision"
    ) -> Optional[GoalDoc]:
        """First-boot migration of a legacy ``config.idle_prompt`` value into
        ``goal.yaml`` — the SINGLE migration path (org-template values arrive
        pre-materialized as ``config.idle_prompt`` too). Idempotent via the
        ``.migrated`` marker; obeys the source-rank overwrite rule (may overwrite
        a ``fleet-persona-default`` goal, never a ``workspace-mcp`` one)."""
        if self._migrated_marker().exists():
            return None  # one-shot
        val = (idle_prompt_value or "").strip()
        if not val:
            # Nothing to migrate — do NOT burn the one-shot marker. If the
            # migrator runs before config.idle_prompt is materialized (or on a
            # volume-reusing reprovision), a later boot with a materialized
            # persona-default value can still perform the one-and-only migration.
            return None
        source = SOURCE_FLEET_DEFAULT if val == FLEET_PERSONA_DEFAULT else SOURCE_LEGACY_MIGRATION
        existing = self._read()
        if existing is not None and _SOURCE_RANK.get(existing.source, 99) >= _SOURCE_RANK[source]:
            # an equal-or-higher-ranked goal already exists (e.g. workspace-mcp) —
            # never clobber it.
            self._touch_marker()
            return None
        doc = self.set(val, set_by=set_by, source=source)
        self._touch_marker()
        return doc

    def bootstrap_from_env(
        self, env: Optional[Mapping[str, str]] = None
    ) -> Optional[GoalDoc]:
        """Seed the goal from ``MOLECULE_IDLE_GOAL`` (provision-time env).

        The deterministic provision-time surface: a tenant/CP/e2e can hand a
        workspace its starting objective without an org-template import or an
        LLM tool-call round-trip. Runs EVERY boot (env is provision state, not
        a one-shot file): idempotent by same-value check, and it obeys the
        source-rank rule — an ``env-bootstrap`` goal may replace a
        fleet-default but NEVER an agent-set (``workspace-mcp``) goal, so an
        agent that has since chosen its own objective keeps it across restarts.
        """
        e = os.environ if env is None else env
        val = (e.get(IDLE_GOAL_ENV) or "").strip()
        if not val:
            return None
        existing = self._read()
        if existing is not None:
            if existing.source == SOURCE_ENV_BOOTSTRAP and existing.goal == val:
                return None  # unchanged env re-seed — nothing to do
            if _SOURCE_RANK.get(existing.source, 99) > _SOURCE_RANK[SOURCE_ENV_BOOTSTRAP]:
                return None  # never clobber a higher-ranked (agent-set) goal
        return self.set(val, set_by="env-bootstrap", source=SOURCE_ENV_BOOTSTRAP)

    # ---- digest provider protocol ----------------------------------------

    def _cadence_band(self, doc: GoalDoc, now: float) -> AgeBand:
        if not doc.last_included_at:
            return AgeBand.DUE  # never surfaced -> due
        last = self._parse_iso(doc.last_included_at)
        if last is None:
            return AgeBand.DUE
        return AgeBand.DUE if (now - last) >= doc.cadence_seconds else AgeBand.JUST_INCLUDED

    async def contribute(self) -> list[Contribution]:
        doc = self._read()
        if doc is None:
            return []  # no goal -> no envelope -> (if nothing else) sleep
        now = self.now_fn()
        first_line = doc.goal.splitlines()[0] if doc.goal else doc.goal
        summary = (
            f"Background objective — work it only if nothing above is pending: "
            f"{_cap(first_line, _SUMMARY_MAX_CHARS)}"
        )
        version = hashlib.sha256(f"{doc.goal}\x00{doc.source}".encode()).hexdigest()[:12]
        return [
            Contribution(
                provider_id=self.provider_id,
                band=Band.BASE,
                tier=GOAL_TIER,
                urgency=Urgency.NORMAL,  # a background objective never jumps the queue
                count=1,
                summary=summary,
                age_band=self._cadence_band(doc, now),  # the loop driver
                item_ids=(f"goal:{version}",),
                pull=PullInstruction(
                    tool="goal_get",
                    instruction="Pull the full objective text + source/cadence before starting background work.",
                    max_items=1,
                ),
            )
        ]

    def on_included(self, fired_at: float) -> None:
        """Fire-outcome callback: stamp ``last_included_at`` so the next
        contribution reports ``just-included`` until the cadence elapses — the
        post-inclusion state the assembler baselines against (prevents the
        one-tick-later double fire)."""
        doc = self._read()
        if doc is None:
            return
        doc.last_included_at = self._iso(fired_at)
        try:
            self._write(doc)
        except OSError:
            pass  # a failed stamp just risks one extra fire, never a crash
