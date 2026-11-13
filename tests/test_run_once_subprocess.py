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

    from a2a.helpers import new_text_message

    from molecule_runtime.adapter_base import BaseAdapter

    MODE = os.environ.get("STUB_MODE", "reply")


    class _Executor:
        async def execute(self, context, event_queue):
            if MODE == "raise":
                raise RuntimeError("stub boom")
            if MODE == "hang":
                await asyncio.sleep(60)
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

        async def create_executor(self, config):
            return _Executor()
    """
)


@dataclass
class Call:
    method: str
    path: str
    body: dict | None


class _Recorder(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        self.server.calls.append(Call(self.command, self.path.split("?")[0], body))
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
        return proc.returncode, list(server.calls), proc.stdout + proc.stderr
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


@pytest.mark.parametrize("env", [{"MOLECULE_RUN_MODE": "twice"}, {"MOLECULE_RUN_PROMPT": "  "}])
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
