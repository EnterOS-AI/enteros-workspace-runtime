"""Once mode: run one scheduled prompt through the engine, report it, exit.

A Light agent is a parked agent whose schedules run as isolated, short-lived
copies of it, one Kubernetes Job per fire (contracts C1-C3). The run pod boots
the image's normal start script and then ``molecule-runtime`` with
``MOLECULE_RUN_MODE=once``. ``main()`` boots the adapter and executor exactly as
a workspace does (minus the steps that download, install or only matter to a
long-lived server) and hands the executor to :func:`run_once`, which:

1. reports ``started`` to core's run ledger (``POST /workspaces/{id}/light/runs``);
   a 401, 403 or 409 answer means core will not record the run, so the task
   does not run (exit 1, no ``finished``);
2. sends ``MOLECULE_RUN_PROMPT`` through ``executor.execute()`` as a scheduled
   self-turn (envelope metadata ``source_type=self-scheduler`` and
   ``schedule_name=MOLECULE_RUN_SCHEDULE``, the shape of a trigger-daemon fire);
3. waits until the turn has settled: ``execute()`` returned and the executor's
   busy count (``heartbeat.active_tasks``) is back to zero;
4. reports ``finished`` with the final reply text, flushes traces, and returns
   the exit code: 0 succeeded, 1 failed, 124 timed out (2 = refused to boot,
   raised from :func:`boot_gate`). ``main()`` then arms :func:`arm_exit_bound`
   and exits.

Deadline: the run stops waiting at ``boot start + D - R``, where D is
``MOLECULE_RUN_DEADLINE_SECONDS`` (the Job's ``activeDeadlineSeconds``) and
R = ``min(120 s, D // 4)``. Ending the run then takes up to 58 s: the turn gets
5 s to stop after it is cancelled, the ``finished`` report up to 33 s (three
10 s attempts, 1 s and 2 s apart), the Langfuse and OpenTelemetry flushes up to
10 s each; the exit bound ends the process at most 5 s later. R is at least
58 s only from D = 232 s, and it must also absorb what Kubernetes counts before
this process starts (pod start, image pull, the copy step and the start
script's boot: about 10-15 s warm for hermes), so at the 3600 s default about
57 s are left for that. A pod the Job deadline ends first (a short deadline, a
cold image pull) is recorded by core from the Job's status.

A run never registers, never serves A2A, never heartbeats and never polls the
agent's inbox: :func:`workspace_mode.in_run_pod` switches those paths off
everywhere, including in the MCP server the template runs beside it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
)

from molecule_runtime.platform_auth import auth_headers, validate_workspace_id
from molecule_runtime.workspace_mode import (
    RUN_MODE_ONCE,
    UnknownRunMode,
    resolve_run_mode,
)

logger = logging.getLogger(__name__)

EXIT_SUCCEEDED = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_TIMED_OUT = 124

DEFAULT_DEADLINE_SECONDS = 3600
MAX_RESERVE_SECONDS = 120
OUTPUT_LIMIT_BYTES = 64 * 1024
ERROR_LIMIT_BYTES = 4 * 1024
REPORT_TIMEOUT_SECONDS = 10.0
TRACE_FLUSH_SECONDS = 10.0
EXIT_GRACE_SECONDS = 5.0  # arm_exit_bound: the normal exit's grace

# Answers to the `started` report after which the task must not run (exit 1, no
# `finished`): core will not record the run, and every call the task made to
# core with this token would be refused too.
_STARTED_REFUSALS = {
    401: "the run token is invalid or revoked",
    403: "this token may not report the run",
    409: "not a Light agent, or this run already finished",
}

PROMPT_ENV = "MOLECULE_RUN_PROMPT"
SCHEDULE_ENV = "MOLECULE_RUN_SCHEDULE"
RUN_ID_ENV = "MOLECULE_RUN_ID"
DEADLINE_ENV = "MOLECULE_RUN_DEADLINE_SECONDS"

# a2a_executor.A2A_SOURCE_SELF_SCHEDULER: the marker a trigger-daemon fire carries.
SOURCE_TYPE = "self-scheduler"

# Task states a run counts as failed (contract C6). INPUT_REQUIRED and
# AUTH_REQUIRED are not terminal in A2A, but nobody answers a scheduled run.
_FAILED_STATES = frozenset({
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
})

Report = Callable[["RunSpec", dict], Awaitable["int | None"]]


class RunRefused(ValueError):
    """The run pod's environment cannot start a run (exit 2, no report)."""


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    schedule: str
    prompt: str  # byte-exact, never trimmed
    deadline_seconds: int
    workspace_id: str
    platform_url: str
    started_at: float  # wall clock when main() began
    started_monotonic: float  # monotonic clock when main() began


@dataclass(frozen=True)
class TurnResult:
    status: str  # succeeded | failed | timed_out
    output: str
    error: str
    exit_code: int


def boot_gate(
    env: Mapping[str, str], *, prepare_only: bool, started_at: float, started_monotonic: float
) -> RunSpec | None:
    """Decide, before any boot step, whether this process is a run and with what.

    Returns None for a normal workspace (``MOLECULE_RUN_MODE`` unset or blank)
    and for ``molecule-runtime-prepare`` in once mode (prepare only needs the
    quiet boot). Raises :class:`RunRefused` for an unknown run mode (also in
    prepare) and, in once mode, for a missing or malformed run variable.
    """
    try:
        mode = resolve_run_mode(env)
    except UnknownRunMode as exc:
        raise RunRefused(str(exc)) from exc
    if mode != RUN_MODE_ONCE or prepare_only:
        return None

    def required(name: str) -> str:
        value = env.get(name, "")
        if not value.strip():
            raise RunRefused(f"{name} is unset or blank")
        return value

    prompt = required(PROMPT_ENV)  # used verbatim
    schedule = required(SCHEDULE_ENV).strip()
    run_id = required(RUN_ID_ENV).strip()
    raw_workspace_id = required("WORKSPACE_ID")
    try:
        workspace_id = validate_workspace_id(raw_workspace_id)
    except ValueError as exc:
        raise RunRefused(f"WORKSPACE_ID is not a valid workspace id: {exc}") from exc
    platform_url = required("PLATFORM_URL").strip().rstrip("/")
    raw_deadline = env.get(DEADLINE_ENV, "").strip()
    if not raw_deadline:
        deadline = DEFAULT_DEADLINE_SECONDS
    else:
        try:
            deadline = int(raw_deadline)
        except ValueError:
            deadline = 0
        if deadline <= 0:
            raise RunRefused(f"{DEADLINE_ENV}={raw_deadline[:20]!r} is not a positive whole number of seconds")
    return RunSpec(
        run_id=run_id,
        schedule=schedule,
        prompt=prompt,
        deadline_seconds=deadline,
        workspace_id=workspace_id,
        platform_url=platform_url,
        started_at=started_at,
        started_monotonic=started_monotonic,
    )


def deadline_reserve(deadline_seconds: int) -> int:
    """Seconds kept back from the run deadline: ``min(120, deadline // 4)``."""
    return min(MAX_RESERVE_SECONDS, deadline_seconds // 4)


def turn_deadline(spec: RunSpec) -> float:
    """The monotonic instant once mode stops waiting for the turn."""
    return spec.started_monotonic + spec.deadline_seconds - deadline_reserve(spec.deadline_seconds)


def build_run_context(spec: RunSpec) -> RequestContext:
    """The scheduled self-turn: one user-role text part holding the prompt, with
    the trigger-fire metadata on the request envelope (where the runtime's own
    self-sends and ``channel_sdk.build_trigger_message_send_request`` put it)."""
    message = Message(
        role=Role.ROLE_USER,
        message_id=f"light-run-{spec.run_id}",
        parts=[Part(text=spec.prompt)],
    )
    request = SendMessageRequest(message=message)
    request.metadata.update({"source_type": SOURCE_TYPE, "schedule_name": spec.schedule})
    return RequestContext(ServerCallContext(), request=request)


class RunEventQueue(EventQueue):
    """Collects every event the executor enqueues (a2a-sdk 1.3 deprecates
    instantiating the bare EventQueue interface)."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def tail_utf8(text: str, limit: int) -> str:
    """The last ``limit`` bytes of ``text`` as UTF-8, never splitting a character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[-limit:].decode("utf-8", errors="ignore")


def head_utf8(text: str, limit: int) -> str:
    """The first ``limit`` bytes of ``text`` as UTF-8, never splitting a character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _parts_text(parts: Any) -> str:
    return "".join(getattr(part, "text", "") or "" for part in parts)


def _status_of(event: Any) -> Any:
    if isinstance(event, (TaskStatusUpdateEvent, Task)):
        return event.status
    return None


def classify_turn(
    events: Sequence[Any],
    *,
    raised: BaseException | None = None,
    timed_out: bool = False,
    wedge_reason: str | None = None,
) -> TurnResult:
    """The run's outcome from what the executor enqueued and how the turn ended.

    The output is the text of the last Message, or of the message inside the
    last task status; artifact chunks are ignored (a streaming executor ends with
    the full text in its terminal status)."""
    final_text = ""
    last_state: str | None = None
    for event in events:
        if isinstance(event, Message):
            final_text = _parts_text(event.parts)
            continue
        status = _status_of(event)
        if status is None:
            continue
        last_state = TaskState.Name(status.state)
        if status.HasField("message"):
            final_text = _parts_text(status.message.parts)

    if timed_out:
        return TurnResult(
            "timed_out",
            tail_utf8(final_text, OUTPUT_LIMIT_BYTES),
            "timed out: the turn did not finish before the run deadline",
            EXIT_TIMED_OUT,
        )
    if raised is not None:
        return TurnResult(
            "failed",
            tail_utf8(final_text, OUTPUT_LIMIT_BYTES),
            head_utf8(f"{type(raised).__name__}: {raised}", ERROR_LIMIT_BYTES),
            EXIT_FAILED,
        )
    if last_state in _FAILED_STATES:
        return TurnResult("failed", "", head_utf8(final_text or last_state, ERROR_LIMIT_BYTES), EXIT_FAILED)
    if wedge_reason:
        return TurnResult(
            "failed",
            tail_utf8(final_text, OUTPUT_LIMIT_BYTES),
            head_utf8(f"runtime wedged: {wedge_reason}", ERROR_LIMIT_BYTES),
            EXIT_FAILED,
        )
    return TurnResult("succeeded", tail_utf8(final_text, OUTPUT_LIMIT_BYTES), "", EXIT_SUCCEEDED)


def _rfc3339(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_body(
    spec: RunSpec, *, phase: str, result: TurnResult | None = None, finished_at: float | None = None
) -> dict:
    """A C3 ledger write. Every key is always present; unknown values are null."""
    return {
        "run_id": spec.run_id,
        "schedule": spec.schedule,
        "phase": phase,
        "status": "running" if result is None else result.status,
        "started_at": _rfc3339(spec.started_at),
        "finished_at": None if finished_at is None else _rfc3339(finished_at),
        "exit_code": None if result is None else result.exit_code,
        "output": "" if result is None else result.output,
        "error": "" if result is None else result.error,
    }


async def report_run(
    spec: RunSpec,
    body: dict,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    backoff_seconds: tuple[float, ...] = (1.0, 2.0),
) -> int | None:
    """POST one ledger write to core; return the final HTTP status, or None when
    core was never reached. Transport errors and 5xx (Cloudflare 520-530
    included) are retried after each backoff; any other answer is final."""
    # The workspace id was validated by boot_gate (CWE-20: it is interpolated
    # into the path); auth_headers() carries the run token and, from a run pod,
    # X-Molecule-Run-Id (C3b).
    url = f"{spec.platform_url}/workspaces/{spec.workspace_id}/light/runs"
    final: int | None = None
    attempts = len(backoff_seconds) + 1
    async with httpx.AsyncClient(timeout=REPORT_TIMEOUT_SECONDS, transport=transport) as client:
        for attempt in range(attempts):
            try:
                resp = await client.post(url, json=body, headers=auth_headers())
                final = resp.status_code
                if final < 500:
                    break
                detail = f"HTTP {final}"
            except httpx.HTTPError as exc:
                final = None
                detail = type(exc).__name__
            if attempt < attempts - 1:
                logger.warning(
                    "run ledger %s write failed (%s); retry %d/%d",
                    body.get("phase"), detail, attempt + 1, attempts - 1,
                )
                await asyncio.sleep(backoff_seconds[attempt])
    if final is None or not 200 <= final < 300:
        logger.warning("run ledger %s write: %s", body.get("phase"), final or "core unreachable")
    return final


def flush_traces(timeout_seconds: float = TRACE_FLUSH_SECONDS) -> bool:
    """Wait for the Langfuse trace worker, then force-flush the OTel provider.
    Never raises; True when both had nothing left."""
    flushed = True
    try:
        from molecule_runtime import tracing

        flushed = tracing.flush(timeout_seconds)
    except Exception:  # noqa: BLE001 - tracing is fail-open
        flushed = False
    try:
        from opentelemetry import trace

        force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
        if callable(force_flush):
            flushed = bool(force_flush(int(timeout_seconds * 1000))) and flushed
    except Exception as exc:  # noqa: BLE001 - tracing is fail-open
        logger.debug("OTel force_flush skipped: %s", type(exc).__name__)
    return flushed


def _wedge_reason() -> str | None:
    try:
        from molecule_runtime import runtime_wedge

        return runtime_wedge.wedge_reason() if runtime_wedge.is_wedged() else None
    except Exception:  # noqa: BLE001
        return None


async def _report(report: Report, spec: RunSpec, body: dict) -> int | None:
    try:
        return await report(spec, body)
    except Exception as exc:  # noqa: BLE001 - a ledger bug must not stop the run
        logger.warning("run ledger %s write raised %s", body.get("phase"), type(exc).__name__)
        return None


def _summary_line(spec: RunSpec, result: TurnResult, flushed: bool) -> str:
    line = (
        f"[run-once] run={spec.run_id} schedule={spec.schedule} status={result.status} "
        f"exit={result.exit_code} output_bytes={len(result.output.encode('utf-8'))}"
    )
    return line if flushed else line + " traces=unflushed"


async def _finish(report: Report, spec: RunSpec, result: TurnResult) -> int:
    """Report the run's one ``finished``, flush traces, print the summary line."""
    await _report(report, spec, ledger_body(spec, phase="finished", result=result, finished_at=time.time()))
    # The run now has its one `finished`: nothing below may report it again,
    # and nothing below may raise into the caller, which could take it for a
    # setup failure.
    try:
        flushed = flush_traces(TRACE_FLUSH_SECONDS)
        print(_summary_line(spec, result, flushed), flush=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run %s: %s after the finished report: %s", spec.run_id, type(exc).__name__, exc)
    return result.exit_code


async def run_once(
    spec: RunSpec,
    executor: Any,
    *,
    heartbeat: Any,
    report: Report = report_run,
    monotonic: Callable[[], float] = time.monotonic,
    settle_poll_seconds: float = 0.2,
    cancel_grace_seconds: float = 5.0,
) -> int:
    """Run the scheduled prompt once (see the module docstring); return the exit code.

    Never raises an ``Exception``: one raised while the run is under way (by
    once mode itself, or by an input the engine's types refuse) is the run's
    failure, reported as its one ``finished`` with ``error`` naming it."""
    status = await _report(report, spec, ledger_body(spec, phase="started"))
    refusal = _STARTED_REFUSALS.get(status)
    if refusal is not None:
        print(
            f"[run-once] core refused run {spec.run_id} ({status}: {refusal}); not running the task",
            flush=True,
        )
        return EXIT_FAILED

    try:
        result = await _run_turn(
            spec,
            executor,
            heartbeat=heartbeat,
            monotonic=monotonic,
            settle_poll_seconds=settle_poll_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - reported below as the run's failure
        logger.exception("run %s: once mode raised", spec.run_id)
        result = TurnResult(
            "failed", "", head_utf8(f"once mode raised {type(exc).__name__}: {exc}", ERROR_LIMIT_BYTES), EXIT_FAILED
        )
    return await _finish(report, spec, result)


async def _run_turn(
    spec: RunSpec,
    executor: Any,
    *,
    heartbeat: Any,
    monotonic: Callable[[], float],
    settle_poll_seconds: float,
    cancel_grace_seconds: float,
) -> TurnResult:
    """Send the prompt through the executor, wait for the turn to settle or for
    the deadline, and classify what happened."""
    queue = RunEventQueue()
    context = build_run_context(spec)

    async def turn() -> None:
        await executor.execute(context, queue)
        while getattr(heartbeat, "active_tasks", 0) > 0:
            await asyncio.sleep(settle_poll_seconds)

    raised: BaseException | None = None
    timed_out = False
    budget = turn_deadline(spec) - monotonic()
    if budget <= 0:
        timed_out = True
    else:
        task = asyncio.ensure_future(turn())
        done, _pending = await asyncio.wait({task}, timeout=budget)
        if task in done:
            if task.cancelled():
                raised = asyncio.CancelledError("the turn was cancelled")
            else:
                raised = task.exception()
        else:
            timed_out = True
            task.cancel()
            # The turn gets this long to stop. One that keeps swallowing the
            # cancel is left running: the run reports now, and the exit bound
            # (arm_exit_bound, armed by main()) ends the process soon after.
            await asyncio.wait({task}, timeout=cancel_grace_seconds)
            if task.done() and not task.cancelled():
                task.exception()  # retrieved: the run is timed_out whatever it raised

    result = classify_turn(queue.events, raised=raised, timed_out=timed_out, wedge_reason=_wedge_reason())
    if timed_out:
        reserve = deadline_reserve(spec.deadline_seconds)
        result = replace(
            result,
            error=(
                f"timed out: the turn did not finish within the run deadline "
                f"({spec.deadline_seconds}s, less a {reserve}s reserve to report)"
            ),
        )
    return result


async def report_boot_failure(spec: RunSpec, error: str, *, report: Report = report_run) -> int:
    """The adapter could not be set up: record one failed ``finished`` and exit 1.

    The run never reaches the serve path's registration, which is where a normal
    workspace goes when its setup fails."""
    result = TurnResult("failed", "", head_utf8(f"adapter setup failed: {error}", ERROR_LIMIT_BYTES), EXIT_FAILED)
    return await _finish(report, spec, result)


def arm_exit_bound(
    code: int,
    *,
    grace_seconds: float = EXIT_GRACE_SECONDS,
    hard_exit: Callable[[int], Any] = os._exit,
) -> threading.Timer:
    """Make sure a run's process is gone ``grace_seconds`` from now.

    ``main()`` calls this in once mode only, after the run's last report, then
    raises ``SystemExit(code)`` for the normal exit. That exit can hang: on a
    turn that keeps swallowing cancellation (``asyncio.run`` waits for every
    task it cancels), on a worker thread still blocked in the turn, or on a
    trace export to an unreachable backend at interpreter exit. A run pod held
    that way would live until the Job deadline, and with ``concurrencyPolicy:
    Forbid`` the schedule's next fire would wait for it. The daemon timer ends
    the process with ``code`` after the grace; a normal exit before then ends
    the timer with it. stdout and stderr are flushed now, from this thread."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - a closed stream must not stop the exit
            pass

    def _end() -> None:
        # os.write, not print: the main thread may hold the stdio locks.
        try:
            os.write(2, f"[run-once] still running {grace_seconds:g}s after the run ended; exiting {code}\n".encode())
        except OSError:
            pass
        hard_exit(code)

    timer = threading.Timer(grace_seconds, _end)
    timer.name = "run-once-exit-bound"
    timer.daemon = True
    timer.start()
    return timer
