"""Hermetic assertion of the Langfuse trace-EMISSION path (per-PR, no live Langfuse).

The workspace runtime emits LLM / tool-use / A2A traces to the self-hosted
Langfuse via the OTLP bridge wired in ``molecule_runtime.builtin_tools.telemetry``
(``setup_telemetry`` → second OTLP exporter when ``LANGFUSE_HOST`` +
``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` are all set). If that bridge
silently breaks — wrong endpoint, dropped auth header, or a regression that
stops emitting — agent traces vanish and nobody notices until they go looking.

This test pins the contract WITHOUT any live Langfuse: it stands up a throwaway
in-process HTTP collector, points the runtime's Langfuse env at it, runs the
REAL ``setup_telemetry`` + a span in a subprocess (a subprocess because OTel's
global ``TracerProvider`` is set-once per process), and asserts the span batch
is POSTed to ``/api/public/otel/v1/traces`` with the exact HTTP Basic auth
header Langfuse expects. It also pins the FAIL-OPEN half: a partial credential
set (host present, secret missing) must emit NOTHING — never half-wire tracing.

The live end-to-end counterpart (real agent turn → trace visible in Langfuse,
keyed off run-id) is the gated obs-smoke (scripts/obs_smoke.sh), which runs on
the local stack / dispatch lane where a real Langfuse is reachable.
"""

from __future__ import annotations

import base64
import http.server
import os
import socket
import subprocess
import sys
import threading

import pytest

# Emission driver run in a child process: imports the REAL telemetry module,
# initialises the provider from env, emits one span, and force-flushes the
# BatchSpanProcessor so the export happens before exit.
_EMIT_SNIPPET = """
import time
from opentelemetry import trace
from molecule_runtime.builtin_tools.telemetry import setup_telemetry, get_tracer

setup_telemetry(service_name="langfuse-emission-test")
tracer = get_tracer()
with tracer.start_as_current_span("unit-llm-span") as span:
    span.set_attribute("gen_ai.system", "openai")
    span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
provider = trace.get_tracer_provider()
flush = getattr(provider, "force_flush", None)
if flush:
    flush()
time.sleep(0.3)
"""

# The endpoint + auth scheme the runtime is contracted to use (telemetry.py).
LANGFUSE_OTEL_PATH = "/api/public/otel/v1/traces"


class _Collector(http.server.BaseHTTPRequestHandler):
    """Records every POST so the test can assert path + auth + body presence."""

    received: list[dict] = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        _Collector.received.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body_len": len(body),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):  # silence per-request stderr noise
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_emitter(extra_env: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()}
    # Isolate the Langfuse exporter: drop any generic OTLP endpoint so the only
    # exporter under test is the Langfuse bridge.
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        env.pop(key, None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", _EMIT_SNIPPET],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _serve(handler_cls) -> tuple[http.server.HTTPServer, int]:
    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def test_emits_span_to_langfuse_otel_endpoint_with_basic_auth():
    """With all three LANGFUSE_* set, a span batch is POSTed to the Langfuse
    OTEL endpoint carrying the exact HTTP Basic auth header (pk:sk)."""
    _Collector.received = []
    srv, port = _serve(_Collector)
    try:
        result = _run_emitter(
            {
                "LANGFUSE_HOST": f"http://127.0.0.1:{port}",
                "LANGFUSE_PUBLIC_KEY": "pk-unit",
                "LANGFUSE_SECRET_KEY": "sk-unit",
            }
        )
    finally:
        srv.shutdown()

    assert result.returncode == 0, f"emitter failed: {result.stderr}"
    assert _Collector.received, (
        "no span export reached the fake Langfuse collector — the OTLP "
        "bridge is not emitting (regression in setup_telemetry)"
    )
    req = _Collector.received[0]
    assert req["path"] == LANGFUSE_OTEL_PATH, (
        f"wrong ingest endpoint: {req['path']!r} (expected {LANGFUSE_OTEL_PATH!r})"
    )
    expected_auth = "Basic " + base64.b64encode(b"pk-unit:sk-unit").decode()
    assert req["auth"] == expected_auth, "missing/incorrect Langfuse Basic auth header"
    assert "protobuf" in (req["content_type"] or ""), (
        f"expected OTLP/HTTP protobuf payload, got {req['content_type']!r}"
    )
    assert req["body_len"] > 0, "empty span payload"


def test_fail_open_when_credentials_partial():
    """Host present but secret key missing → emit NOTHING (never half-wire a
    partial credential set). Mirrors the CP-side ApplyLangfuseEnv fail-open."""
    _Collector.received = []
    srv, port = _serve(_Collector)
    try:
        result = _run_emitter(
            {
                "LANGFUSE_HOST": f"http://127.0.0.1:{port}",
                "LANGFUSE_PUBLIC_KEY": "pk-unit",
                # LANGFUSE_SECRET_KEY intentionally unset
            }
        )
    finally:
        srv.shutdown()

    assert result.returncode == 0, f"emitter failed: {result.stderr}"
    assert _Collector.received == [], (
        "fail-open violated: a span was exported with an incomplete "
        "LANGFUSE_* credential set"
    )
