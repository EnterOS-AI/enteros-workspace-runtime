"""Tests for the idle-digest agent tools (goal + task management, task #219).

The tools construct providers with the default mailbox path, so the mailbox
kernel is pointed at a tmp dir (the standard mailbox test convention).
"""
from __future__ import annotations

import json

import pytest

from molecule_runtime import a2a_tools_idle as idle
from molecule_runtime import mailbox_dir


@pytest.fixture(autouse=True)
def _mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / ".molecule"))
    yield


# ---- goal ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_set_get_clear():
    assert json.loads(await idle.tool_goal_get())["goal"] is None
    r = json.loads(await idle.tool_goal_set("pull the backlog", cadence_seconds=1800))
    assert r["set"] is True and r["cadence_seconds"] == 1800
    got = json.loads(await idle.tool_goal_get())["goal"]
    assert got["goal"] == "pull the backlog" and got["source"] == "workspace-mcp"
    assert json.loads(await idle.tool_goal_clear())["cleared"] is True
    assert json.loads(await idle.tool_goal_get())["goal"] is None


@pytest.mark.asyncio
async def test_goal_set_requires_text():
    r = json.loads(await idle.tool_goal_set("   "))
    assert r["set"] is False


@pytest.mark.asyncio
async def test_goal_cadence_floor():
    r = json.loads(await idle.tool_goal_set("x", cadence_seconds=60))
    assert r["cadence_seconds"] == 300  # clamped


# ---- tasks ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_add_list_update_complete():
    added = json.loads(await idle.tool_task_add("rotate token", next_action="mint it"))
    tid = added["id"]
    assert added["added"] is True
    rows = json.loads(await idle.tool_task_list())["tasks"]
    assert any(t["id"] == tid and t["status"] == "queued" for t in rows)

    upd = json.loads(await idle.tool_task_update(tid, status="current"))
    assert upd["updated"] is True
    rows = json.loads(await idle.tool_task_list(status="current"))["tasks"]
    assert rows[0]["id"] == tid

    done = json.loads(await idle.tool_task_complete(tid))
    assert done["completed"] is True
    assert not json.loads(await idle.tool_task_list())["tasks"]  # gone from active


@pytest.mark.asyncio
async def test_task_add_requires_title():
    r = json.loads(await idle.tool_task_add(""))
    assert r["added"] is False


@pytest.mark.asyncio
async def test_task_update_unknown_id_reports_error():
    r = json.loads(await idle.tool_task_update("task-nope", status="done"))
    assert r["updated"] is False and "unknown" in r["error"]
