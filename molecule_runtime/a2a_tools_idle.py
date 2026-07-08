"""Idle-digest agent tools — goal + task-queue management (task #219).

The agent-facing MCP tools that wrap the goal-state and task-queue provider
logic: the agent sets/reads its own background goal and manages its work queue,
and the idle digest surfaces both. Each tool follows the module dispatch
contract (an ``async def tool_*`` returning a JSON string); state is durable on
the mailbox volume via the providers (kernel-ON) and the tools are also the
``pull`` targets the digest points at (``goal_get`` / ``task_list``).
"""
from __future__ import annotations

import json
from typing import Optional

from molecule_runtime.idle_digest.providers import GoalStateProvider, TaskQueueProvider


def _goal() -> GoalStateProvider:
    return GoalStateProvider()


def _tasks() -> TaskQueueProvider:
    return TaskQueueProvider()


# ---- goal-state -----------------------------------------------------------


async def tool_goal_get() -> str:
    """Return this workspace's standing background goal + its cadence/source."""
    doc = _goal().get()
    if doc is None:
        return json.dumps({"goal": None, "note": "no background goal set"}, indent=2)
    return json.dumps({"goal": doc}, indent=2)


async def tool_goal_set(goal: str, cadence_seconds: Optional[int] = None) -> str:
    """Set/replace this workspace's standing background goal — the objective the
    idle digest surfaces when nothing else is pending."""
    if not goal or not goal.strip():
        return json.dumps({"set": False, "error": "goal text is required"}, indent=2)
    doc = _goal().set(
        goal, cadence_seconds=cadence_seconds, set_by="agent", source="workspace-mcp"
    )
    return json.dumps(
        {"set": True, "goal": doc.goal, "cadence_seconds": doc.cadence_seconds}, indent=2
    )


async def tool_goal_clear() -> str:
    """Clear the standing background goal (the workspace then goes quiet when
    idle instead of pulling backlog)."""
    _goal().clear(set_by="agent")
    return json.dumps({"cleared": True}, indent=2)


# ---- task-queue -----------------------------------------------------------


async def tool_task_list(status: Optional[str] = None, limit: int = 20) -> str:
    """List this workspace's active tasks (current/queued/paused/blocked/
    awaiting-user), or one status. The idle digest points its task section here."""
    rows = _tasks().list_tasks(status=status, limit=limit)
    return json.dumps(
        {
            "tasks": [
                {
                    "id": r.id, "status": r.status, "origin": r.origin,
                    "title": r.title, "next_action": r.next_action,
                }
                for r in rows
            ]
        },
        indent=2,
    )


async def tool_task_add(title: str, next_action: Optional[str] = None) -> str:
    """Queue a task on this workspace's own work ledger so idle never forgets
    accepted work."""
    if not title or not title.strip():
        return json.dumps({"added": False, "error": "title is required"}, indent=2)
    tid = _tasks().add_task(
        title, origin="agent", status="queued", next_action=next_action
    )
    return json.dumps({"added": True, "id": tid}, indent=2)


async def tool_task_update(
    task_id: str, status: Optional[str] = None, next_action: Optional[str] = None
) -> str:
    """Update a task's status (current/queued/paused/blocked/done/dropped) or
    next action. Setting status='current' pauses the previous current task."""
    try:
        _tasks().update_task(task_id, status=status, next_action=next_action)
    except ValueError as e:
        return json.dumps({"updated": False, "error": str(e)}, indent=2)
    return json.dumps({"updated": True, "id": task_id}, indent=2)


async def tool_task_complete(task_id: str) -> str:
    """Mark a task done — it drops out of the idle digest on the next change."""
    _tasks().complete_task(task_id)
    return json.dumps({"completed": True, "id": task_id}, indent=2)
