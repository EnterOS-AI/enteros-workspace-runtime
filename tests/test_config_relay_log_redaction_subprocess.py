"""Real-process gate: a config-relay fetch never writes its presigned URL to logs.

The leak this pins: ``main.py``'s import-time logging bootstrap puts the root
logger at INFO on stdout, httpx logs every request at INFO with its full URL,
and the config-relay prelude GETs a SigV4 presigned URL — so each fetch attempt
printed ``X-Amz-Credential`` and ``X-Amz-Signature`` to container stdout, which
the log collector ships to Loki.

The check runs in a fresh interpreter because the leak depends on process-wide
logging state that only exists after ``import molecule_runtime.main`` in a
process with no prior logging configuration, which is how the
``molecule-runtime`` and ``molecule-runtime-prepare`` console scripts start.
It drives ``run_config_relay_prelude`` against a mock transport that answers
403 (the failure mode observed in production), captures everything the child
writes to stdout and stderr, and asserts on that text.

All values are synthetic.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SIGNATURE = "f00d" * 16
ACCESS_KEY_ID = "SYNTHETICKEYIDFORTESTS0000000000"
HOST = "bucket.example-account.r2.cloudflarestorage.com"
PATH = "/relay/ws-1/n0nce.json"
PRESIGNED = (
    f"https://{HOST}{PATH}"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={ACCESS_KEY_ID}%2F20260929%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260929T180747Z&X-Amz-Expires=600&X-Amz-SignedHeaders=host"
    f"&x-id=GetObject&X-Amz-Signature={SIGNATURE}"
)

_CHILD = textwrap.dedent(
    """
    import logging
    import sys

    import httpx

    import molecule_runtime.main  # noqa: F401 — the production logging bootstrap
    from molecule_runtime.config_relay import run_config_relay_prelude

    url, config_path = sys.argv[1], sys.argv[2]
    env = {
        "MOLECULE_CONFIG_RELAY_URI": url,
        "MOLECULE_CONFIG_RELAY_SHA256": "0" * 64,
        "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "synthetic-ack",
        "MOLECULE_CP_URL": "https://cp.example",
        "WORKSPACE_ID": "ws-1",
    }

    def forbidden(request):
        return httpx.Response(403)

    def transport_error_quoting_the_url(request):
        # An error whose text embeds the request URL, the way
        # httpx.HTTPStatusError renders ("... for url '<url>'").
        raise httpx.ConnectError(f"connect failed for url '{request.url}'", request=request)

    def prelude(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            run_config_relay_prelude(
                workspace_id="ws-1", config_path=config_path, env=env,
                client=client, sleep=lambda _s: None,
            )
        except SystemExit as exc:
            print(f"SystemExit: {exc}", flush=True)

    print("=== phase 1: default bootstrap levels", flush=True)
    prelude(forbidden)
    prelude(transport_error_quoting_the_url)
    print("=== phase 2: httpx request logging turned back up", flush=True)
    logging.getLogger("httpx").setLevel(logging.INFO)
    prelude(forbidden)
    print("=== done", flush=True)
    """
)


def _run_child(tmp_path: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("LOG_LEVEL", "PYTHONWARNINGS")}
    env["LOG_LEVEL"] = "INFO"  # the main.py default, pinned against the caller's env
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, PRESIGNED, str(tmp_path / "configs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    combined = result.stdout + result.stderr
    assert "=== done" in result.stdout, (
        f"child did not finish (rc={result.returncode}); stderr tail:\n{result.stderr[-2000:]}"
    )
    return combined


def test_relay_fetch_logs_never_carry_the_presigned_query(tmp_path):
    out = _run_child(tmp_path)

    # The capability never reaches stdout/stderr, in either phase.
    assert SIGNATURE not in out
    assert ACCESS_KEY_ID not in out

    phase1, _, rest = out.partition("=== phase 2")
    phase2 = rest.partition("=== done")[0]

    # Phase 1 (production default): no per-request httpx lines at all, and
    # the failure still says what failed and where.
    assert "HTTP Request:" not in phase1
    assert "HTTP 403" in phase1
    assert "transport error: ConnectError" in phase1
    assert f"from https://{HOST}{PATH} (X-Amz-Date=20260929T180747Z, X-Amz-Expires=600)" in phase1
    assert f"for url 'https://{HOST}{PATH}?<redacted>'" in phase1

    # Phase 2: with httpx back at INFO the request lines return, redacted by
    # the filter the bootstrap installed on the root handler.
    assert phase2.count(f"HTTP Request: GET https://{HOST}{PATH}?<redacted>") == 6
