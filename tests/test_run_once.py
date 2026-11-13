"""Once mode: run one scheduled prompt through the engine, report it, exit.

A Light run pod (``MOLECULE_RUN_MODE=once``, contract C1) boots the adapter and
executor like a normal workspace, then ``run_once`` sends the schedule's prompt
through ``executor.execute()`` as a scheduled self-turn, waits for the turn to
settle, reports ``started``/``finished`` to core's run ledger (C3) and returns
the process exit code (0 succeeded, 1 failed, 124 timed out; 2 refused to boot).

Real a2a-sdk types throughout: the executors under test receive the same
RequestContext/EventQueue shapes a production turn gets.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from a2a.helpers import new_text_message
from a2a.types import Role, Task, TaskState, TaskStatus, TaskStatusUpdateEvent

import molecule_runtime.platform_auth as platform_auth
from molecule_runtime import run_once as ro
from molecule_runtime.run_once import (
    RunRefused,
    RunSpec,
    TurnResult,
    boot_gate,
    build_run_context,
    classify_turn,
    deadline_reserve,
    head_utf8,
    ledger_body,
    report_boot_failure,
    report_run,
    run_once,
    tail_utf8,
    turn_deadline,
)

WS = "11111111-2222-3333-4444-555555555555"
P = "http://platform.test"
RUN_ID = "light-ws-abc-123"
T_START = datetime(2026, 11, 13, 4, 39, 52, tzinfo=timezone.utc).timestamp()
T_0441 = datetime(2026, 11, 13, 4, 41, 0, tzinfo=timezone.utc).timestamp()
T0 = {"started_at": T_START, "started_monotonic": 0.0}


def run_env(**over):
    """A complete once-mode env; an override of None removes the key."""
    env = {
        "MOLECULE_RUN_MODE": "once",
        "MOLECULE_RUN_PROMPT": "do the thing",
        "MOLECULE_RUN_SCHEDULE": "daily-report",
        "MOLECULE_RUN_ID": RUN_ID,
        "MOLECULE_RUN_DEADLINE_SECONDS": "3600",
        "WORKSPACE_ID": WS,
        "PLATFORM_URL": P,
    }
    for key, value in over.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _spec(**over) -> RunSpec:
    fields = dict(
        run_id=RUN_ID,
        schedule="daily-report",
        prompt="do the thing",
        deadline_seconds=3600,
        workspace_id=WS,
        platform_url=P,
        started_at=T_START,
        started_monotonic=time.monotonic(),
    )
    fields.update(over)
    return RunSpec(**fields)


def _spec_with_seconds_left(seconds: float, **over) -> RunSpec:
    """A spec whose turn deadline is ``seconds`` from now."""
    deadline = over.pop("deadline_seconds", 3600)
    started = time.monotonic() - (deadline - deadline_reserve(deadline)) + seconds
    return _spec(deadline_seconds=deadline, started_monotonic=started, **over)


def _hb(active: int = 0):
    return SimpleNamespace(active_tasks=active, current_task="")


class _Recorder:
    """Stands in for report_run: records bodies, answers scripted statuses."""

    def __init__(self, statuses=(), heartbeat=None, order=None):
        self.statuses = list(statuses)
        self.bodies: list[dict] = []
        self.heartbeat = heartbeat
        self.active_tasks_at: list[int] = []
        self.order = order

    async def __call__(self, spec, body):
        self.bodies.append(body)
        if self.heartbeat is not None:
            self.active_tasks_at.append(self.heartbeat.active_tasks)
        if self.order is not None:
            self.order.append(f"report:{body['phase']}")
        return self.statuses.pop(0) if self.statuses else 200


class Replying:
    def __init__(self, text):
        self.text = text
        self.called = False

    async def execute(self, context, queue):
        self.called = True
        await queue.enqueue_event(new_text_message(self.text))


class Recording:
    async def execute(self, context, queue):
        self.text = context.message.parts[0].text
        self.metadata = context.metadata
        await queue.enqueue_event(new_text_message("ok"))


class Raising:
    async def execute(self, context, queue):
        raise RuntimeError("boom")


class Sleeping:
    async def execute(self, context, queue):
        await queue.enqueue_event(new_text_message("partial"))
        await asyncio.sleep(60)


class Stubborn:
    """Swallows every cancellation until released (or for 10 s at most), so only
    run_once's cancel grace can bound how long run_once waits for it."""

    def __init__(self):
        self.release = asyncio.Event()
        self.cancels = 0
        self.task = None

    async def execute(self, context, queue):
        self.task = asyncio.current_task()
        await queue.enqueue_event(new_text_message("partial"))
        give_up = time.monotonic() + 10
        while not self.release.is_set() and time.monotonic() < give_up:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancels += 1

    async def let_go(self):
        """Release the turn and wait for it, so the test's loop can close."""
        self.release.set()
        if self.task is not None:
            await asyncio.wait({self.task}, timeout=5)


def msg(text):
    return new_text_message(text)


def status(state_name, text=None):
    state = TaskState.Value(state_name)
    st = TaskStatus(state=state, message=new_text_message(text)) if text is not None else TaskStatus(state=state)
    return TaskStatusUpdateEvent(task_id="t1", context_id="c1", status=st)


def task(state_name):
    return Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.Value(state_name)))


# --- boot_gate -----------------------------------------------------------------


def test_boot_gate_serves_when_mode_unset_or_blank():
    assert boot_gate({}, prepare_only=False, **T0) is None
    assert boot_gate({"MOLECULE_RUN_MODE": " "}, prepare_only=False, **T0) is None


@pytest.mark.parametrize("prepare_only", [False, True])
def test_boot_gate_refuses_unknown_mode(prepare_only):
    with pytest.raises(RunRefused, match="MOLECULE_RUN_MODE"):
        boot_gate({"MOLECULE_RUN_MODE": "twice"}, prepare_only=prepare_only, **T0)


def test_boot_gate_prepare_in_once_mode_needs_no_run_env():
    assert boot_gate({"MOLECULE_RUN_MODE": "once"}, prepare_only=True, **T0) is None


def test_boot_gate_builds_the_run_spec():
    spec = boot_gate(run_env(PLATFORM_URL=P + "/"), prepare_only=False, **T0)
    assert spec == RunSpec(
        run_id=RUN_ID, schedule="daily-report", prompt="do the thing", deadline_seconds=3600,
        workspace_id=WS, platform_url=P, started_at=T_START, started_monotonic=0.0,
    )


@pytest.mark.parametrize(
    "name", ["MOLECULE_RUN_PROMPT", "MOLECULE_RUN_SCHEDULE", "MOLECULE_RUN_ID", "WORKSPACE_ID", "PLATFORM_URL"]
)
@pytest.mark.parametrize("value", [None, "  "])
def test_boot_gate_refuses_missing_run_env(name, value):
    with pytest.raises(RunRefused, match=name):
        boot_gate(run_env(**{name: value}), prepare_only=False, **T0)


def test_boot_gate_refuses_an_unsafe_workspace_id():
    with pytest.raises(RunRefused, match="WORKSPACE_ID"):
        boot_gate(run_env(WORKSPACE_ID="../etc"), prepare_only=False, **T0)


@pytest.mark.parametrize("raw,want", [(None, 3600), ("", 3600), ("60", 60)])
def test_boot_gate_deadline(raw, want):
    spec = boot_gate(run_env(MOLECULE_RUN_DEADLINE_SECONDS=raw), prepare_only=False, **T0)
    assert spec.deadline_seconds == want


@pytest.mark.parametrize("raw", ["0", "-5", "1.5", "abc"])
def test_boot_gate_refuses_bad_deadline(raw):
    with pytest.raises(RunRefused, match="MOLECULE_RUN_DEADLINE_SECONDS"):
        boot_gate(run_env(MOLECULE_RUN_DEADLINE_SECONDS=raw), prepare_only=False, **T0)


def test_boot_gate_keeps_prompt_byte_exact():
    prompt = "  line 1\n\tline 2 ✓ 中文\n" + "x" * 65536 + "\n"
    assert boot_gate(run_env(MOLECULE_RUN_PROMPT=prompt), prepare_only=False, **T0).prompt == prompt


# --- deadline, context, text helpers --------------------------------------------


def test_deadline_reserve_and_turn_deadline():
    assert [deadline_reserve(d) for d in (3600, 300, 8)] == [120, 75, 2]
    assert turn_deadline(_spec(started_monotonic=100.0)) == 3580.0


def test_build_run_context_matches_a_trigger_fire():
    ctx = build_run_context(_spec(prompt="a\nb ✓"))
    assert ctx.message.role == Role.ROLE_USER
    assert [p.text for p in ctx.message.parts] == ["a\nb ✓"]
    assert ctx.metadata == {"source_type": "self-scheduler", "schedule_name": "daily-report"}


def test_tail_and_head_utf8_never_split_a_character():
    assert tail_utf8("中" * 30000, 65536) == "中" * 21845
    assert head_utf8("中" * 2000, 4096) == "中" * 1365
    assert tail_utf8("short", 65536) == "short" and head_utf8("short", 4096) == "short"


def test_tail_and_head_utf8_keep_a_lone_surrogate_visible():
    # Text decoded with surrogateescape (a file name, a tool's output) holds lone
    # surrogates, which UTF-8 cannot encode; the report must still carry it.
    assert head_utf8("caf\udce9.txt", 4096) == "caf\\udce9.txt"
    assert tail_utf8("caf\udce9.txt", 65536) == "caf\\udce9.txt"
    assert head_utf8("\udce9" * 1000, 16) == "\\udce9\\udce9\\udc"


# --- classify_turn ----------------------------------------------------------------


@pytest.mark.parametrize(
    "events,kw,want",
    [
        ([msg("done")], {}, ("succeeded", "done", "", 0)),
        ([task("TASK_STATE_SUBMITTED"), status("TASK_STATE_COMPLETED", "final")], {}, ("succeeded", "final", "", 0)),
        ([status("TASK_STATE_FAILED", "quota exceeded")], {}, ("failed", "", "quota exceeded", 1)),
        ([status("TASK_STATE_INPUT_REQUIRED", "approve?")], {}, ("failed", "", "approve?", 1)),
        ([status("TASK_STATE_CANCELED")], {}, ("failed", "", "TASK_STATE_CANCELED", 1)),
        ([], {"raised": RuntimeError("boom")}, ("failed", "", "RuntimeError: boom", 1)),
        ([msg("ok")], {"wedge_reason": "sdk init"}, ("failed", "ok", "runtime wedged: sdk init", 1)),
        ([], {}, ("succeeded", "", "", 0)),
    ],
)
def test_classify_turn(events, kw, want):
    r = classify_turn(events, **kw)
    assert (r.status, r.output, r.error, r.exit_code) == want


def test_classify_turn_timed_out_keeps_partial_output():
    r = classify_turn([msg("partial")], timed_out=True)
    assert (r.status, r.output, r.exit_code) == ("timed_out", "partial", 124)
    assert "deadline" in r.error


def test_classify_turn_truncates_output_tail_and_error_head():
    r = classify_turn([msg("a" * 70000 + "END")])
    assert len(r.output.encode()) == 64 * 1024 and r.output.endswith("END")
    r = classify_turn([status("TASK_STATE_FAILED", "BEGIN" + "e" * 9000)])
    assert len(r.error.encode()) == 4 * 1024 and r.error.startswith("BEGIN")


# --- ledger_body / report_run ---------------------------------------------------------


def test_ledger_body_carries_every_c3_key():
    assert ledger_body(_spec(), phase="started") == {
        "run_id": RUN_ID, "schedule": "daily-report", "phase": "started", "status": "running",
        "started_at": "2026-11-13T04:39:52Z", "finished_at": None, "exit_code": None,
        "output": "", "error": "",
    }
    fin = ledger_body(_spec(), phase="finished", result=TurnResult("succeeded", "done", "", 0), finished_at=T_0441)
    assert (fin["phase"], fin["status"], fin["finished_at"], fin["exit_code"], fin["output"], fin["error"]) == (
        "finished", "succeeded", "2026-11-13T04:41:00Z", 0, "done", "",
    )


@pytest.fixture
def run_pod_token(monkeypatch):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    monkeypatch.setenv("MOLECULE_RUN_ID", RUN_ID)
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "run-token")
    monkeypatch.setenv("PLATFORM_URL", P)
    platform_auth.clear_cache()
    yield
    platform_auth.clear_cache()


@pytest.mark.asyncio
async def test_report_run_posts_ledger_with_workspace_token(run_pod_token):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    body = ledger_body(_spec(), phase="started")
    assert await report_run(_spec(), body, transport=httpx.MockTransport(handler)) == 200
    (req,) = seen
    assert (req.method, str(req.url)) == ("POST", f"{P}/workspaces/{WS}/light/runs")
    assert req.headers["Authorization"] == "Bearer run-token"
    assert req.headers["X-Molecule-Run-Id"] == RUN_ID  # C3b
    assert json.loads(req.content) == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script,calls,want",
    [([503, 530, 200], 3, 200), ([401], 1, 401), ([404], 1, 404), (["connect-error"] * 3, 3, None)],
)
async def test_report_run_retries_transient_only(run_pod_token, script, calls, want):
    seen = []

    def handler(request):
        seen.append(request)
        step = script[len(seen) - 1]
        if step == "connect-error":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(step)

    got = await report_run(_spec(), {"phase": "started"}, transport=httpx.MockTransport(handler), backoff_seconds=(0, 0))
    assert got == want and len(seen) == calls


# --- run_once -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_flush(monkeypatch):
    monkeypatch.setattr(ro, "flush_traces", lambda timeout_seconds=10.0: True)


@pytest.mark.asyncio
async def test_run_once_success_reports_started_then_finished():
    rec = _Recorder()
    assert await run_once(_spec(), Replying("all done"), heartbeat=_hb(), report=rec) == 0
    assert [(b["phase"], b["status"]) for b in rec.bodies] == [("started", "running"), ("finished", "succeeded")]
    assert rec.bodies[1]["output"] == "all done" and rec.bodies[1]["exit_code"] == 0
    assert rec.bodies[0]["started_at"] == rec.bodies[1]["started_at"] == "2026-11-13T04:39:52Z"
    assert rec.bodies[1]["finished_at"] is not None


@pytest.mark.asyncio
async def test_run_once_hands_prompt_and_schedule_to_executor_unchanged():
    prompt = "  line 1\n\tline 2 ✓ 中文\n" + "x" * 65536 + "\n"
    ex = Recording()
    assert await run_once(_spec(prompt=prompt), ex, heartbeat=_hb(), report=_Recorder()) == 0
    assert ex.text == prompt
    assert ex.metadata == {"source_type": "self-scheduler", "schedule_name": "daily-report"}


@pytest.mark.asyncio
async def test_run_once_executor_exception_is_failed():
    rec = _Recorder()
    assert await run_once(_spec(), Raising(), heartbeat=_hb(), report=rec) == 1
    assert rec.bodies[-1]["status"] == "failed" and "RuntimeError: boom" in rec.bodies[-1]["error"]


@pytest.mark.asyncio
async def test_run_once_failed_task_state_is_failed():   # contract C6
    class GaveUp:
        async def execute(self, context, queue):
            await queue.enqueue_event(status("TASK_STATE_FAILED", "turn stalled: no sign of life for 900s"))

    rec = _Recorder()
    assert await run_once(_spec(), GaveUp(), heartbeat=_hb(), report=rec) == 1
    assert (rec.bodies[-1]["status"], rec.bodies[-1]["error"]) == ("failed", "turn stalled: no sign of life for 900s")


@pytest.mark.asyncio
async def test_run_once_deadline_cancels_and_reports_timed_out():
    rec = _Recorder()
    started = time.monotonic()
    rc = await run_once(_spec_with_seconds_left(0.3), Sleeping(), heartbeat=_hb(), report=rec)
    assert rc == 124 and time.monotonic() - started < 5
    assert rec.bodies[-1]["status"] == "timed_out" and rec.bodies[-1]["output"] == "partial"
    assert rec.bodies[-1]["exit_code"] == 124 and "deadline" in rec.bodies[-1]["error"]


@pytest.mark.asyncio
async def test_run_once_cancel_grace_bounds_a_turn_that_keeps_ignoring_cancel():
    ex, rec = Stubborn(), _Recorder()
    started = time.monotonic()
    try:
        rc = await run_once(
            _spec_with_seconds_left(0.2), ex, heartbeat=_hb(), report=rec, cancel_grace_seconds=0.1
        )
        elapsed = time.monotonic() - started
        # Without the grace, run_once would wait the executor's full 10 s.
        assert rc == 124 and elapsed < 1.0
        assert ex.cancels >= 1 and not ex.task.done()  # still swallowing: only the grace ended the wait
        assert (rec.bodies[-1]["status"], rec.bodies[-1]["output"]) == ("timed_out", "partial")
    finally:
        await ex.let_go()


@pytest.mark.asyncio
async def test_run_once_waits_for_turn_to_settle():
    hb = _hb()

    class SettlesLate:   # the hermes shape: busy released by a task spawned after execute() returns
        async def execute(self, context, queue):
            hb.active_tasks += 1
            await queue.enqueue_event(new_text_message("done"))

            async def release():
                await asyncio.sleep(0.3)
                hb.active_tasks -= 1

            self.release = asyncio.ensure_future(release())

    rec = _Recorder(heartbeat=hb)
    assert await run_once(_spec(), SettlesLate(), heartbeat=hb, report=rec, settle_poll_seconds=0.05) == 0
    assert rec.active_tasks_at[-1] == 0  # the finished report waited for the release


class SlowToStop:
    """Needs a short await to stop once cancelled: cleanup that enqueues the
    turn's last status. Classifying the run before that cleanup is done would
    report a turn that has not stopped yet."""

    def __init__(self):
        self.task = None

    async def execute(self, context, queue):
        self.task = asyncio.current_task()
        await queue.enqueue_event(new_text_message("partial"))
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.1)
            await queue.enqueue_event(new_text_message("stopped cleanly"))
            raise


@pytest.mark.asyncio
async def test_run_once_cancel_grace_waits_for_a_turn_that_stops_soon_after_the_cancel():
    ex, rec = SlowToStop(), _Recorder()
    try:
        rc = await run_once(_spec_with_seconds_left(0.2), ex, heartbeat=_hb(), report=rec)
        assert ex.task.done()  # run_once waited for the turn to stop
        assert rc == 124
        assert (rec.bodies[-1]["status"], rec.bodies[-1]["output"]) == ("timed_out", "stopped cleanly")
    finally:
        if ex.task is not None:
            await asyncio.wait({ex.task}, timeout=5)


@pytest.mark.asyncio
async def test_run_once_unsettled_turn_times_out():
    hb = _hb()

    class NeverSettles:
        async def execute(self, context, queue):
            hb.active_tasks = 1
            await queue.enqueue_event(new_text_message("interim"))

    rec = _Recorder()
    rc = await run_once(_spec_with_seconds_left(0.4), NeverSettles(), heartbeat=hb, report=rec, settle_poll_seconds=0.05)
    assert rc == 124 and rec.bodies[-1]["status"] == "timed_out"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403, 409])
async def test_run_once_core_refusing_started_does_not_run(code, capsys):
    # 409: not a Light agent (or the run already finished); 401/403: the run
    # token is revoked or not this run's. Core would record nothing, and every
    # call the task made with that token would be refused too.
    rec, ex = _Recorder(statuses=[code]), Replying("x")
    assert await run_once(_spec(), ex, heartbeat=_hb(), report=rec) == 1
    assert ex.called is False
    assert [b["phase"] for b in rec.bodies] == ["started"]  # no `finished`
    assert f"core refused run {RUN_ID} ({code}:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_once_core_404_still_runs():   # core without the ledger route (before Task 3)
    rec, ex = _Recorder(statuses=[404, 404]), Replying("x")
    assert await run_once(_spec(), ex, heartbeat=_hb(), report=rec) == 0
    assert ex.called is True and len(rec.bodies) == 2


@pytest.mark.asyncio
async def test_run_once_report_crash_does_not_stop_the_run():
    async def broken(spec, body):
        raise RuntimeError("ledger client bug")

    ex = Replying("x")
    assert await run_once(_spec(), ex, heartbeat=_hb(), report=broken) == 0
    assert ex.called is True


@pytest.mark.asyncio
async def test_run_once_wedged_runtime_is_failed():
    from molecule_runtime import runtime_wedge

    class Wedges:
        async def execute(self, context, queue):
            runtime_wedge.mark_wedged("sdk init wedge")
            await queue.enqueue_event(new_text_message("ok"))

    rec = _Recorder()
    try:
        assert await run_once(_spec(), Wedges(), heartbeat=_hb(), report=rec) == 1
        assert rec.bodies[-1]["error"] == "runtime wedged: sdk init wedge"
    finally:
        runtime_wedge.reset_for_test()


@pytest.mark.asyncio
async def test_run_once_flushes_traces_after_finished(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(ro, "flush_traces", lambda timeout_seconds=10.0: order.append("flush") or True)
    await run_once(_spec(), Replying("x"), heartbeat=_hb(), report=_Recorder(order=order))
    assert order == ["report:started", "report:finished", "flush"]


# --- an executor exception whose text UTF-8 cannot encode -------------------------------


@pytest.mark.asyncio
async def test_run_once_reports_the_executors_own_exception_when_its_text_is_not_utf8(run_pod_token):
    class RaisesOnAFileName:
        async def execute(self, context, queue):
            raise RuntimeError("tool failed on caf\udce9.txt")  # a name decoded with surrogateescape

    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def report(spec, body):  # the real report_run: httpx must be able to encode the body
        return await report_run(spec, body, transport=transport)

    assert await run_once(_spec(), RaisesOnAFileName(), heartbeat=_hb(), report=report) == 1

    assert [(b["phase"], b["status"]) for b in sent] == [("started", "running"), ("finished", "failed")]
    assert sent[-1]["error"] == "RuntimeError: tool failed on caf\\udce9.txt"


class UnknownState:
    """An executor bug: a task status whose state the a2a enum does not name."""

    async def execute(self, context, queue):
        await queue.enqueue_event(
            TaskStatusUpdateEvent(task_id="t1", context_id="c1", status=TaskStatus(state=99))
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,want",
    [
        # MOLECULE_RUN_PROMPT holding a byte that is not UTF-8 (the env decodes
        # it as a lone surrogate): the turn's protobuf message refuses it.
        ("non-utf8-prompt", "UnicodeEncodeError"),
        # classify_turn cannot name the state.
        ("unknown-task-state", "ValueError"),
    ],
)
async def test_run_once_exception_inside_is_the_runs_failure(case, want):
    if case == "non-utf8-prompt":
        spec, ex = _spec(prompt="report \udcff"), Replying("x")
    else:
        spec, ex = _spec(), UnknownState()
    rec = _Recorder()

    assert await run_once(spec, ex, heartbeat=_hb(), report=rec) == 1

    assert [(b["phase"], b["status"], b["exit_code"]) for b in rec.bodies] == [
        ("started", "running", None),
        ("finished", "failed", 1),
    ]
    error = rec.bodies[-1]["error"]
    assert want in error and "adapter setup failed" not in error


@pytest.mark.asyncio
async def test_run_once_exception_after_finished_never_reports_again(monkeypatch):
    def broken_flush(timeout_seconds=10.0):
        raise RuntimeError("flush bug")

    monkeypatch.setattr(ro, "flush_traces", broken_flush)
    rec = _Recorder()

    assert await run_once(_spec(), Replying("done"), heartbeat=_hb(), report=rec) == 0

    assert [(b["phase"], b["status"]) for b in rec.bodies] == [("started", "running"), ("finished", "succeeded")]


@pytest.mark.asyncio
async def test_report_boot_failure_reports_one_failed_finish():
    rec = _Recorder()
    assert await report_boot_failure(_spec(), "RuntimeError: gateway down", report=rec) == 1
    assert [(b["phase"], b["status"], b["exit_code"]) for b in rec.bodies] == [("finished", "failed", 1)]
    assert rec.bodies[0]["error"].startswith("adapter setup failed:")


# --- the exit bound (main.py arms it after the run's last report) -------------------------


def test_arm_exit_bound_ends_a_process_that_outlives_the_grace():
    ended: list[int] = []
    timer = ro.arm_exit_bound(124, grace_seconds=0.05, hard_exit=ended.append)
    timer.join(2)
    assert ended == [124]
    # A daemon timer: a process that exits normally first is never held by it.
    assert timer.daemon and timer.name == "run-once-exit-bound"


def test_arm_exit_bound_never_raises_when_no_thread_can_start(monkeypatch, capsys):
    # Timer.start() raises this when the pod's pids or memory limit refuses a
    # thread. main() arms the bound inside the adapter-setup `try`, so a raise
    # here would be reported as a second `finished`, "adapter setup failed".
    class NoThreadTimer(threading.Timer):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(ro, "threading", SimpleNamespace(Timer=NoThreadTimer))
    ended: list[int] = []

    assert ro.arm_exit_bound(0, grace_seconds=0.05, hard_exit=ended.append) is None

    assert "could not arm the exit bound (RuntimeError: can't start new thread)" in capsys.readouterr().err
    assert ended == []


def test_post_deadline_accounting_matches_the_readme():
    """README "Light runs": after the turn deadline, ending the run takes about
    88 s: the cancel grace, the finished report with its retries, the Langfuse
    and OTel flushes. httpx's 10 s timeout applies to each phase of a request,
    so one report attempt can take about 20 s (10 s to connect, 10 s waiting
    for the answer). The exit bound ends the process at most 5 s after that.
    The reserve min(120, D // 4) covers the 88 s only from D = 352 s, and at the
    3600 s default leaves about 27 s for what Kubernetes counts before boot."""
    grace = inspect.signature(run_once).parameters["cancel_grace_seconds"].default
    backoff = inspect.signature(report_run).parameters["backoff_seconds"].default
    attempt = 2 * ro.REPORT_TIMEOUT_SECONDS  # connect, then the answer
    report_worst = attempt * (len(backoff) + 1) + sum(backoff)
    total = grace + report_worst + 2 * ro.TRACE_FLUSH_SECONDS
    assert (grace, attempt, report_worst, total, ro.EXIT_GRACE_SECONDS) == (5.0, 20.0, 63.0, 88.0, 5.0)
    assert min(d for d in range(1, 4000) if deadline_reserve(d) >= total) == 352
    assert deadline_reserve(3600) - total - ro.EXIT_GRACE_SECONDS == 27
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for claim in ("about 20 s", "about 63 s", "about 88 s", "352 s", "about 27 s"):
        assert claim in readme, claim
    for stale in ("58 s", "33 s", "232 s", "57 s"):
        assert stale not in readme, stale
