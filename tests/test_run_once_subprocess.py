"""Once mode on a real boot: ``molecule-runtime``'s own entry point in a child.

``main()`` is ``# pragma: no cover``, so this is what proves its once-mode
wiring end to end: a child process runs ``main_sync()`` with a stub adapter
(``ADAPTER_MODULE``) against a recording fake platform. A run must make exactly
two requests, the ``started`` and ``finished`` ledger writes (contract C3):
no config-relay fetch, no ``/registry/register`` or ``/registry/heartbeat``, no
boot-step or plugin-install report, and it must never start serving A2A (a
child that did would hang until the timeout).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="boots main() in a child; the boot touches /workspace paths"
)

WS = "11111111-2222-3333-4444-555555555555"
RUNS = f"/workspaces/{WS}/light/runs"

STUB_ADAPTER = textwrap.dedent(
    """
    import asyncio
    import os
    import time

    from a2a.helpers import new_text_message

    from molecule_runtime.adapter_base import BaseAdapter

    MODE = os.environ.get("STUB_MODE", "reply")


    async def _swallow_cancel_for(seconds):
        give_up = time.monotonic() + seconds
        while time.monotonic() < give_up:
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                pass


    if MODE == "escape":
        # Stands in for a bug that lets an exception escape run_once after it
        # has reported `finished`: main() must not report the run again.
        import molecule_runtime.run_once as _run_once

        _real_run_once = _run_once.run_once

        async def _escaping_run_once(*args, **kwargs):
            await _real_run_once(*args, **kwargs)
            raise RuntimeError("escaped after the finished report")

        _run_once.run_once = _escaping_run_once


    if os.environ.get("STUB_ARM_FAILS"):
        # Stands in for a pod whose pids or memory limit refuses a new thread:
        # Timer.start(), and so arming the exit bound, raises.
        import threading as _threading
        import types as _types

        import molecule_runtime.run_once as _run_once

        class _NoThreadTimer(_threading.Timer):
            def start(self):
                raise RuntimeError("can't start new thread")

        _run_once.threading = _types.SimpleNamespace(Timer=_NoThreadTimer)


    class _Executor:
        async def execute(self, context, event_queue):
            if MODE == "raise":
                raise RuntimeError("stub boom")
            if MODE == "hang":
                await asyncio.sleep(60)
            if MODE == "stubborn":
                # Keeps swallowing CancelledError for 40 s, then returns.
                await event_queue.enqueue_event(new_text_message("partial"))
                await _swallow_cancel_for(40)
                return
            meta = context.metadata
            text = context.message.parts[0].text
            await event_queue.enqueue_event(
                new_text_message(f"echo:{meta['source_type']}:{meta['schedule_name']}:{text}")
            )

        async def cancel(self, context, event_queue):
            return None


    class Adapter(BaseAdapter):
        @staticmethod
        def name():
            return "stub-light"

        @staticmethod
        def display_name():
            return "Stub (Light e2e)"

        @staticmethod
        def description():
            return "test adapter for once mode"

        async def setup(self, config):
            if MODE == "setup_fails":
                raise RuntimeError("stub setup failed")
            if MODE == "setup_fails_stubborn":
                # Leaves a task behind that keeps swallowing CancelledError.
                self._left_behind = asyncio.ensure_future(_swallow_cancel_for(40))
                raise RuntimeError("stub setup failed")

        async def create_executor(self, config):
            return _Executor()
    """
)


@dataclass
class Call:
    method: str
    path: str
    body: dict | None
    at: float = 0.0  # time.monotonic() when the request arrived
    wall: float = 0.0  # time.time() when the request arrived


class _Recorder(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        self.server.calls.append(
            Call(self.command, self.path.split("?")[0], body, time.monotonic(), time.time())
        )
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _record

    def log_message(self, *_args):  # keep pytest output clean
        pass


_ENTRIES = {
    "serve": "from molecule_runtime.main import main_sync; main_sync()",
    "prepare": "from molecule_runtime.main import prepare_sync; prepare_sync()",
}


def _boot(tmp_path, stub, entry="serve", **env_over):
    """Boot the real runtime once; return (exit code, recorded calls, output)."""
    rc, calls, out, _exited_at = _boot_timed(tmp_path, stub, entry, **env_over)
    return rc, calls, out


def _boot_timed(tmp_path, stub, entry="serve", **env_over):
    """``_boot`` plus the time.monotonic() at which the child had exited."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        platform = f"http://127.0.0.1:{server.server_address[1]}"
        mods = tmp_path / "mods"
        mods.mkdir()
        (mods / "stub_light_adapter.py").write_text(STUB_ADAPTER, encoding="utf-8")
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "config.yaml").write_text("name: light-e2e\nruntime: stub-light\n", encoding="utf-8")
        (configs / ".auth_token").write_text("run-token", encoding="utf-8")
        (tmp_path / "home").mkdir()

        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("MOLECULE_", "LANGFUSE_", "OTEL_", "GIT", "GH_"))
        }
        env.update({
            "HOME": str(tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join(p for p in (str(mods), env.get("PYTHONPATH", "")) if p),
            "ADAPTER_MODULE": "stub_light_adapter",
            "STUB_MODE": stub,
            "WORKSPACE_ID": WS,
            "PLATFORM_URL": platform,
            "WORKSPACE_CONFIG_PATH": str(configs),
            "CONFIGS_DIR": str(configs),
            "MOLECULE_MAILBOX_DIR": str(tmp_path / "mbox"),
            "MOLECULE_RUN_MODE": "once",
            "MOLECULE_RUN_PROMPT": "do the thing",
            "MOLECULE_RUN_SCHEDULE": "daily-report",
            "MOLECULE_RUN_ID": "light-ws-abc-123",
            "MOLECULE_RUN_DEADLINE_SECONDS": "120",
            # A stale relay key from the snapshot: once mode must never fetch it.
            "MOLECULE_CONFIG_RELAY_URI": f"{platform}/relay/bundle",
            "MOLECULE_CONFIG_RELAY_SHA256": "0" * 64,
            "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "ack-token",
        })
        env.update(env_over)
        proc = subprocess.run(
            [sys.executable, "-c", _ENTRIES[entry]],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=90,
        )
        exited_at = time.monotonic()
        return proc.returncode, list(server.calls), proc.stdout + proc.stderr, exited_at
    finally:
        server.shutdown()
        server.server_close()


def test_once_mode_boot_reports_and_never_registers(tmp_path):
    rc, calls, out = _boot(tmp_path, "reply", MOLECULE_RUN_PROMPT="line 1\nline ✓ 2")

    assert rc == 0, out[-3000:]
    # Exactly the two ledger writes: no relay GET, no /registry/*, no boot-event,
    # no plugin-install-report.
    assert [(c.method, c.path, (c.body or {}).get("phase")) for c in calls] == [
        ("POST", RUNS, "started"),
        ("POST", RUNS, "finished"),
    ], out[-3000:]
    finished = calls[1].body
    assert finished["status"] == "succeeded" and finished["exit_code"] == 0
    assert finished["output"] == "echo:self-scheduler:daily-report:line 1\nline ✓ 2"
    assert "[plugins]" not in out  # the declared-plugin install never ran


@pytest.mark.parametrize(
    "stub,env,rc_want,status_want",
    [
        ("raise", {}, 1, "failed"),
        ("hang", {"MOLECULE_RUN_DEADLINE_SECONDS": "8"}, 124, "timed_out"),
        ("setup_fails", {}, 1, "failed"),
    ],
)
def test_once_mode_boot_exit_codes(tmp_path, stub, env, rc_want, status_want):
    rc, calls, out = _boot(tmp_path, stub, **env)

    assert rc == rc_want, out[-3000:]
    assert calls and calls[-1].body["status"] == status_want, out[-3000:]
    assert all(c.path == RUNS for c in calls), [c.path for c in calls]


@pytest.mark.parametrize(
    "stub,rc_want,phases_want,status_want",
    [
        ("stubborn", 124, ["started", "finished"], "timed_out"),  # the turn ignores the cancel
        ("setup_fails_stubborn", 1, ["finished"], "failed"),  # setup left such a task behind
    ],
)
def test_once_mode_exit_is_bounded_when_a_task_ignores_cancel(tmp_path, stub, rc_want, phases_want, status_want):
    """A task that keeps swallowing CancelledError must not hold the process.
    asyncio.run's shutdown waits for every task it cancels, so without the exit
    bound this child lived until the stub gave up (40 s), about 30 s after its
    timed_out report; in a run pod that lasts until the Job deadline."""
    rc, calls, out, exited_at = _boot_timed(tmp_path, stub, MOLECULE_RUN_DEADLINE_SECONDS="8")

    assert rc == rc_want, out[-3000:]
    assert [(c.body or {}).get("phase") for c in calls] == phases_want, out[-3000:]
    assert calls[-1].body["status"] == status_want
    if stub == "stubborn":
        assert calls[-1].body["output"] == "partial"
    lingered = exited_at - calls[-1].at
    assert lingered < 15, f"the child exited {lingered:.1f}s after its finished report"


def test_once_mode_reports_an_exception_inside_the_run_as_its_failure(tmp_path):
    """A prompt byte that is not UTF-8 makes the turn's message raise inside
    run_once, after `started`. The run's one `finished` names the exception; it
    is never reported as an adapter setup failure."""
    rc, calls, out = _boot(tmp_path, "reply", MOLECULE_RUN_PROMPT="report \udcff")

    assert rc == 1, out[-3000:]
    assert [(c.body or {}).get("phase") for c in calls] == ["started", "finished"], out[-3000:]
    finished = calls[-1].body
    assert finished["status"] == "failed" and "UnicodeEncodeError" in finished["error"]
    assert "adapter setup failed" not in finished["error"]
    assert "adapter.setup() failed" not in out


def test_once_mode_never_reports_twice_when_an_exception_escapes_the_run(tmp_path):
    rc, calls, out = _boot(tmp_path, "escape")

    assert rc == 1, out[-3000:]
    # The run's own two reports and nothing else: no second `finished` saying
    # "adapter setup failed".
    assert [(c.body or {}).get("phase") for c in calls] == ["started", "finished"], out[-3000:]
    assert calls[-1].body["status"] == "succeeded"
    assert "adapter.setup() failed" not in out
    assert "escaped after the finished report" in out


@pytest.mark.parametrize(
    "stub,rc_want,phases_want,status_want",
    [
        ("reply", 0, ["started", "finished"], "succeeded"),
        ("setup_fails", 1, ["finished"], "failed"),
    ],
)
def test_once_mode_reports_once_when_the_exit_bound_cannot_be_armed(tmp_path, stub, rc_want, phases_want, status_want):
    """Review 21722 Optional 1: main() arms the exit bound inside the adapter-
    setup `try`. When no thread can start, arming it raised into the setup arm,
    which reported a second `finished` ("adapter setup failed") and exited 1
    under a `succeeded` row. The run's own report must stay its only one."""
    rc, calls, out = _boot(tmp_path, stub, STUB_ARM_FAILS="1")

    assert rc == rc_want, out[-3000:]
    assert [(c.body or {}).get("phase") for c in calls] == phases_want, out[-3000:]
    assert calls[-1].body["status"] == status_want
    assert "could not arm the exit bound (RuntimeError: can't start new thread)" in out
    assert "Traceback" not in out, out[-3000:]
    if stub == "reply":
        assert "adapter.setup() failed" not in out


def test_once_mode_holds_the_turn_until_the_scheduled_minute_plus_hold(tmp_path):
    """Start early, end to end: a Job named by the CronJob controller for the
    current minute, with a hold that puts the target about 10 s after this
    child starts. The `started` report (and so the turn) waits for the target."""
    now = time.time()
    minute = int(now) // 60
    hold = int(now - minute * 60) + 10
    target = minute * 60 + hold

    rc, calls, out = _boot(
        tmp_path,
        "reply",
        MOLECULE_RUN_ID=f"light-1a2b3c4d-9f8e7d6c-{minute}",
        MOLECULE_RUN_HOLD_SECONDS=str(hold),
    )

    assert rc == 0, out[-3000:]
    assert [(c.body or {}).get("phase") for c in calls] == ["started", "finished"], out[-3000:]
    assert calls[0].wall >= target - 0.05, f"started {target - calls[0].wall:.2f}s before the target"
    assert "holding the turn" in out, out[-3000:]
    assert calls[-1].body["status"] == "succeeded"


def test_once_mode_hold_that_reaches_the_turn_deadline_never_starts_the_task(tmp_path):
    """Review 21724: this pod's clock reads 30 min before the Job's minute, and
    the run deadline (D = 20 s, so boot + 15 s) is shorter than even the capped
    hold. The task never starts: one `finished` (timed_out) that names the hold,
    no `started`, exit 124; the log names the clock difference."""
    minute = int(time.time()) // 60 + 30

    rc, calls, out = _boot(
        tmp_path,
        "reply",
        MOLECULE_RUN_ID=f"light-1a2b3c4d-9f8e7d6c-{minute}",
        MOLECULE_RUN_HOLD_SECONDS="60",
        MOLECULE_RUN_DEADLINE_SECONDS="20",
    )

    assert rc == 124, out[-3000:]
    assert [(c.body or {}).get("phase") for c in calls] == ["finished"], out[-3000:]
    finished = calls[0].body
    assert finished["status"] == "timed_out" and finished["exit_code"] == 124
    assert finished["error"].startswith("timed out while waiting to start: the start-early hold reached"), finished
    assert "the hold + 30s at most: this pod's clock is" in out and "until the turn deadline" in out


def test_once_mode_never_holds_a_run_now_job(tmp_path):
    started = time.time()
    rc, calls, out = _boot(
        tmp_path,
        "reply",
        MOLECULE_RUN_ID=f"light-1a2b3c4d-9f8e7d6c-m{int(started)}",
        MOLECULE_RUN_HOLD_SECONDS="60",
    )

    assert rc == 0, out[-3000:]
    assert [(c.body or {}).get("phase") for c in calls] == ["started", "finished"], out[-3000:]
    assert "holding the turn" not in out and "no hold: the run id names no scheduled minute" in out
    assert calls[-1].wall - started < 45, "a run-now Job waited for a hold"


@pytest.mark.parametrize(
    "env",
    [{"MOLECULE_RUN_MODE": "twice"}, {"MOLECULE_RUN_PROMPT": "  "}, {"MOLECULE_RUN_HOLD_SECONDS": "soon"}],
)
def test_once_mode_refusal_exits_2_without_any_call(tmp_path, env):
    rc, calls, out = _boot(tmp_path, "reply", **env)

    assert rc == 2, out[-3000:]
    assert calls == []
    assert "refused to boot" in out


def test_prepare_in_once_mode_is_quiet(tmp_path):
    """The hermes start script runs molecule-runtime-prepare in once mode too:
    it must get the same quiet boot (no relay fetch, no plugin install, no
    platform call at all) and still materialize the config."""
    rc, calls, out = _boot(tmp_path, "reply", entry="prepare")

    assert rc == 0, out[-3000:]
    assert calls == []
    assert "MOLECULE_PREPARE_OK" in out
    assert "[plugins]" not in out
