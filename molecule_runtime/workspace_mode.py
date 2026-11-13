"""How this workspace process runs: env switches read live on every call.

``MOLECULE_CONTINUOUS`` — whether the workspace wakes itself (24/7) or runs only
when something calls it. Unset or truthy keeps today's behavior: the idle digest
re-wakes the agent ``idle_fire_after_seconds`` after every turn while its digest
has changed. Set explicitly falsy, the runtime arms no idle self-wake at all —
neither the idle digest nor the legacy static idle loop. The workspace still
runs every message it receives (user chat, a peer's delegation) and every
schedule its trigger daemon fires; it only stops waking itself. Without the
digest nothing re-surfaces the task queue (task_add) or the standing goal
(goal_set), so the system prompt tells the agent (prompt.ON_DEMAND_PROMPT).

``MOLECULE_RUN_MODE`` — Light runs (contract C1). Unset or blank: the normal
long-lived workspace. ``once``: this process is a Light run pod that runs one
prompt and exits (``molecule_runtime.run_once``). Any other value refuses to
boot. The switch is read from the environment, never from in-process state,
because the MCP server and the CLIs a template starts in the same container
inherit the env but share nothing else with the runtime.

``MOLECULE_LIGHT`` — set on every Light agent (its open Deployment and its run
pods): ``kind: trigger`` plugin daemons are never started, so the agent's own
scheduler stays off while its schedules run as Light runs.
"""
from __future__ import annotations

import os

from molecule_runtime.mailbox_dir import _FALSY

CONTINUOUS_ENV = "MOLECULE_CONTINUOUS"
RUN_MODE_ENV = "MOLECULE_RUN_MODE"
LIGHT_ENV = "MOLECULE_LIGHT"

RUN_MODE_SERVE = "serve"
RUN_MODE_ONCE = "once"

# Same truthy set as smoke_mode.is_smoke_mode.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class UnknownRunMode(ValueError):
    """``MOLECULE_RUN_MODE`` holds a value other than blank or ``once``."""


def continuous() -> bool:
    """True unless ``MOLECULE_CONTINUOUS`` is set explicitly falsy. Read live (not cached)."""
    return os.environ.get(CONTINUOUS_ENV, "").strip().lower() not in _FALSY


def resolve_run_mode() -> str:
    """``"serve"`` when ``MOLECULE_RUN_MODE`` is unset or blank, ``"once"`` for
    ``once`` (trimmed, any case). Raises :class:`UnknownRunMode` for anything else:
    a run pod with a value this runtime does not know must refuse to boot rather
    than guess."""
    raw = os.environ.get(RUN_MODE_ENV, "")
    value = raw.strip().lower()
    if not value:
        return RUN_MODE_SERVE
    if value == RUN_MODE_ONCE:
        return RUN_MODE_ONCE
    raise UnknownRunMode(
        f"{RUN_MODE_ENV}={raw.strip()[:40]!r} is not a run mode this runtime knows "
        f"(expected unset or {RUN_MODE_ONCE!r})"
    )


def in_run_pod() -> bool:
    """True when ``MOLECULE_RUN_MODE`` is set to any non-blank value.

    That is ``once``, or an unknown value that refuses to boot: either way this
    process belongs to a Light run pod, so it must never heartbeat, register, poll
    the agent's inbox or emit boot steps. Read live (not cached)."""
    return bool(os.environ.get(RUN_MODE_ENV, "").strip())


def is_light() -> bool:
    """True when ``MOLECULE_LIGHT`` is truthy (``1``, ``true``, ``yes``, ``on``).
    Read live (not cached)."""
    return os.environ.get(LIGHT_ENV, "").strip().lower() in _TRUTHY
