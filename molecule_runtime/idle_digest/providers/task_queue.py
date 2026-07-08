"""The task-queue digest provider — the durable work ledger (tier 1).

The one place the agent's accepted work is written down so idle can never forget
it: the current task, user-origin requests (D3 pivots), blocked/paused work,
queued next tasks, lifecycle resume rows (§5.2), and agent→user asks awaiting a
reply. It emits two envelopes (contract ``task_queue`` policy):

  * **E1 urgent** — open user-origin rows + open lifecycle resume rows, which
    must outrank everything (an idle agent resumes the half-done user ask
    instead of drifting to the backlog). "Open" = queued/paused/blocked; the
    ``current`` row is excluded (it renders in E2 only — no double-surface).
  * **E2 base tier 1** — current/paused/blocked/queued summary + awaiting-user
    asks.

The store is **runtime-owned** (not provider-owned): it survives provider
removal, so user-ask durability (operator-ruled D3) is never downgraded by
removing a rendering lego. ``add_task`` / ``set_current`` / ``update_task`` /
``complete_task`` / ``pivot_to_user`` / ``list_tasks`` carry the logic the
future ``task_*`` MCP tools and the D3 message-path hook will wrap; the hook
itself and the requests-indexer that populates ``awaiting_user`` rows are
core-lane follow-ups (they need the platform user-origin marker + a requests
read API). Nothing invokes the provider until the idle controller lands.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..contract import AgeBand, Band, Contribution, PullInstruction, Urgency

TASK_QUEUE_PROVIDER_ID = "task-queue"
TASK_TIER = 1

# status vocabulary (contract task_queue.status_enum)
STATUS_CURRENT = "current"
STATUS_QUEUED = "queued"
STATUS_PAUSED = "paused"
STATUS_BLOCKED = "blocked"
STATUS_AWAITING_USER = "awaiting_user"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"
_STATUSES = frozenset(
    {STATUS_CURRENT, STATUS_QUEUED, STATUS_PAUSED, STATUS_BLOCKED,
     STATUS_AWAITING_USER, STATUS_DONE, STATUS_DROPPED}
)
_ACTIVE = frozenset({STATUS_CURRENT, STATUS_QUEUED, STATUS_PAUSED, STATUS_BLOCKED})
# E1-"open" = the open backlog states, current excluded (renders in E2 only)
_OPEN = frozenset({STATUS_QUEUED, STATUS_PAUSED, STATUS_BLOCKED})

# origin vocabulary (contract task_queue.origin_enum); 'a2a'/'scheduler' reserved
ORIGIN_USER = "user"
ORIGIN_AGENT = "agent"
ORIGIN_LIFECYCLE = "lifecycle"
_ORIGINS = frozenset({ORIGIN_USER, ORIGIN_AGENT, "a2a", "scheduler", ORIGIN_LIFECYCLE})

TOMBSTONE_RETENTION_DAYS = 14
_MAX_PREVIEW = 5
_SUMMARY_CAP = 180


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def _age_band(age_seconds: float) -> AgeBand:
    if age_seconds < 3600:
        return AgeBand.UNDER_1H
    if age_seconds < 86400:
        return AgeBand.ONE_H_TO_1D
    return AgeBand.OVER_1D


@dataclass
class TaskRow:
    id: str
    origin: str
    status: str
    title: str
    next_action: Optional[str]
    request_id: Optional[str]
    resume_payload: Optional[str]
    created_at: float
    updated_at: float
    done_at: Optional[float]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    origin         TEXT NOT NULL,
    status         TEXT NOT NULL,
    title          TEXT NOT NULL,
    next_action    TEXT,
    request_id     TEXT,
    delegation_id  TEXT,
    correlation_id TEXT,
    resume_payload TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    done_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_request ON tasks(request_id);
"""


@dataclass
class TaskQueueProvider:
    """Reads the durable work ledger and emits the tier-1 E1/E2 envelopes.
    Owns the reserved ``task-queue`` id (official)."""

    provider_id: str = field(default=TASK_QUEUE_PROVIDER_ID, init=False)
    official: bool = field(default=True, init=False)

    # injected seams (testability)
    db_path: Optional[Path] = None
    now_fn: Callable[[], float] = time.time

    # ---- store ----------------------------------------------------------

    def _resolve_db(self) -> Path:
        if self.db_path is not None:
            p = Path(self.db_path)
        else:
            from molecule_runtime import mailbox_dir

            base = mailbox_dir.resolve() / "idle-prompt" / "providers" / "task-queue"
            try:
                base.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:
                pass
            p = base / "state.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._resolve_db()))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    @staticmethod
    def _row(r: sqlite3.Row) -> TaskRow:
        return TaskRow(
            id=r["id"], origin=r["origin"], status=r["status"], title=r["title"],
            next_action=r["next_action"], request_id=r["request_id"],
            resume_payload=r["resume_payload"], created_at=r["created_at"],
            updated_at=r["updated_at"], done_at=r["done_at"],
        )

    # ---- writer library (the MCP tools + D3 hook wrap these) ------------

    def add_task(
        self,
        title: str,
        *,
        origin: str = ORIGIN_AGENT,
        status: str = STATUS_QUEUED,
        next_action: Optional[str] = None,
        request_id: Optional[str] = None,
        resume_payload: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> str:
        if origin not in _ORIGINS:
            raise ValueError(f"invalid origin {origin!r}")
        if status not in _STATUSES:
            raise ValueError(f"invalid status {status!r}")
        tid = task_id or f"task-{uuid.uuid4().hex[:12]}"
        now = self.now_fn()
        with self._conn() as conn:
            if status == STATUS_CURRENT:
                self._demote_current(conn, now)
            conn.execute(
                "INSERT INTO tasks (id, origin, status, title, next_action, "
                "request_id, resume_payload, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, origin, status, title.strip(), next_action, request_id,
                 resume_payload, now, now),
            )
        return tid

    @staticmethod
    def _demote_current(conn: sqlite3.Connection, now: float) -> None:
        """Single-current invariant: any existing current row is paused."""
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE status=?",
            (STATUS_PAUSED, now, STATUS_CURRENT),
        )

    @staticmethod
    def _require(conn: sqlite3.Connection, task_id: str) -> None:
        """Fail loudly on an unknown id BEFORE any mutation, so a stale/mistyped
        id can never demote the real current task and strand the queue."""
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
            raise ValueError(f"unknown task id {task_id!r}")

    def set_current(self, task_id: str) -> None:
        now = self.now_fn()
        with self._conn() as conn:
            self._require(conn, task_id)  # verify target exists before demoting
            self._demote_current(conn, now)
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (STATUS_CURRENT, now, task_id),
            )

    def update_task(
        self, task_id: str, *, status: Optional[str] = None,
        next_action: Optional[str] = None,
    ) -> None:
        if status is not None and status not in _STATUSES:
            raise ValueError(f"invalid status {status!r}")
        now = self.now_fn()
        with self._conn() as conn:
            self._require(conn, task_id)  # verify target exists before demoting
            if status == STATUS_CURRENT:
                self._demote_current(conn, now)
            sets, params = ["updated_at=?"], [now]
            if status is not None:
                sets.append("status=?")
                params.append(status)
            if next_action is not None:
                sets.append("next_action=?")
                params.append(next_action)
            params.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)

    def complete_task(self, task_id: str) -> None:
        now = self.now_fn()
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, done_at=?, updated_at=? WHERE id=?",
                (STATUS_DONE, now, now, task_id),
            )

    def pivot_to_user(self, title: str, *, request_id: Optional[str] = None) -> str:
        """D3: a user message arrives — pause the interrupted current task and
        make the new user-origin row current (the runtime message path wraps
        this; the interrupted task survives as ``paused``)."""
        return self.add_task(
            title, origin=ORIGIN_USER, status=STATUS_CURRENT, request_id=request_id
        )

    def upsert_awaiting_user(self, request_id: str, title: str) -> None:
        """Requests-indexer entry point (populated by the future core requests
        read-API poll): an agent→user ask awaiting a reply."""
        now = self.now_fn()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE request_id=? AND status=?",
                (request_id, STATUS_AWAITING_USER),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tasks (id, origin, status, title, request_id, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (f"req-{uuid.uuid4().hex[:12]}", ORIGIN_AGENT,
                     STATUS_AWAITING_USER, title.strip(), request_id, now, now),
                )

    def resolve_awaiting_user(self, request_id: str) -> None:
        now = self.now_fn()
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, done_at=?, updated_at=? "
                "WHERE request_id=? AND status=?",
                (STATUS_DONE, now, now, request_id, STATUS_AWAITING_USER),
            )

    def list_tasks(
        self, *, status: Optional[str] = None, limit: int = 20
    ) -> list[TaskRow]:
        """The ``task_list`` tool body — bounded, active tasks by default."""
        with self._conn() as conn:
            if status is not None:
                cur = conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY updated_at ASC "
                    "LIMIT ?", (status, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM tasks WHERE status IN "
                    "('current','queued','paused','blocked','awaiting_user') "
                    "ORDER BY updated_at ASC LIMIT ?", (limit,),
                )
            return [self._row(r) for r in cur.fetchall()]

    def prune_tombstones(self) -> int:
        """Drop done/dropped rows older than the retention window. Kept for
        idempotent re-indexing (no zombie re-upsert) + task_list history."""
        cutoff = self.now_fn() - TOMBSTONE_RETENTION_DAYS * 86400
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM tasks WHERE status IN ('done','dropped') "
                "AND COALESCE(done_at, updated_at) < ?", (cutoff,),
            )
            return cur.rowcount

    def _active(self) -> list[TaskRow]:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT * FROM tasks WHERE status IN "
                "('current','queued','paused','blocked','awaiting_user') "
                "ORDER BY updated_at ASC"
            )
            return [self._row(r) for r in cur.fetchall()]

    # ---- digest provider protocol ---------------------------------------

    def _item_id(self, row: TaskRow) -> str:
        # status serialized into the item id so a status change re-fires
        return f"{row.id}:{row.status}"

    def _envelope_age_band(self, rows: list[TaskRow], now: float) -> AgeBand:
        if not rows:
            return AgeBand.NONE
        oldest = min(r.updated_at for r in rows)
        return _age_band(now - oldest)

    async def contribute(self) -> list[Contribution]:
        try:
            rows = self._active()
        except sqlite3.Error:
            return []  # a corrupt store must never crash/stall the tick
        if not rows:
            return []  # no work -> no envelope

        now = self.now_fn()
        by_status: dict[str, list[TaskRow]] = {}
        for r in rows:
            by_status.setdefault(r.status, []).append(r)

        current = by_status.get(STATUS_CURRENT, [])
        queued = by_status.get(STATUS_QUEUED, [])
        paused = by_status.get(STATUS_PAUSED, [])
        blocked = by_status.get(STATUS_BLOCKED, [])
        awaiting = by_status.get(STATUS_AWAITING_USER, [])

        envelopes: list[Contribution] = []

        # E1 urgent: open user-origin rows + open lifecycle resume rows
        # (open = queued/paused/blocked; current excluded — renders in E2 only)
        e1_rows = [
            r for r in rows
            if r.status in _OPEN and r.origin in (ORIGIN_USER, ORIGIN_LIFECYCLE)
        ]
        if e1_rows:
            previews = "; ".join(
                f"{_cap(r.title, 60)}"
                + (f" (next: {_cap(r.next_action, 40)})" if r.next_action else "")
                for r in e1_rows[:_MAX_PREVIEW]
            )
            envelopes.append(
                Contribution(
                    provider_id=self.provider_id,
                    band=Band.URGENT,
                    tier=TASK_TIER,
                    urgency=Urgency.URGENT,
                    count=len(e1_rows),
                    summary=_cap(
                        f"{len(e1_rows)} open user/lifecycle ask(s): {previews}",
                        _SUMMARY_CAP,
                    ),
                    age_band=self._envelope_age_band(e1_rows, now),
                    item_ids=tuple(self._item_id(r) for r in e1_rows),
                    pull=PullInstruction(
                        tool="task_list",
                        instruction="Pull bounded task detail before resuming the user ask.",
                        max_items=5,
                    ),
                )
            )

        # E2 base tier 1: current + counts + awaiting-user
        parts = []
        if current:
            c = current[0]
            parts.append(f"current: {_cap(c.title, 60)}")
        if queued:
            parts.append(f"{len(queued)} queued")
        if blocked:
            parts.append(f"{len(blocked)} blocked")
        if paused and not current:
            parts.append(f"{len(paused)} paused")
        if awaiting:
            parts.append(f"{len(awaiting)} awaiting user")
        e2_rows = current + queued + blocked + paused + awaiting
        envelopes.append(
            Contribution(
                provider_id=self.provider_id,
                band=Band.BASE,
                tier=TASK_TIER,
                urgency=Urgency.NORMAL,
                count=len(e2_rows),
                summary=_cap("Tasks — " + ("; ".join(parts) or "in flight"), _SUMMARY_CAP),
                age_band=self._envelope_age_band(e2_rows, now),
                item_ids=tuple(self._item_id(r) for r in e2_rows),
                pull=PullInstruction(
                    tool="task_list",
                    instruction="Pull bounded current/queued task detail before deciding what to resume.",
                    max_items=5,
                ),
            )
        )
        return envelopes

    def on_included(self, fired_at: float) -> None:  # event-driven, no cadence
        return None
