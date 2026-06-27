"""Real-subprocess regression gate for the build_routes 4-branch contract.

Background
----------

``tests/conftest.py`` stubs ``a2a.*`` modules at pytest collect time so the
rest of the suite can import ``molecule_runtime.*`` without the heavy
``a2a-sdk`` dep. That means any in-process test of ``build_routes`` that
imports through the normal pytest path is asserting against STUBS, not the
production wire. A regression that breaks the real ``a2a.server.routes`` /
``DefaultRequestHandler`` integration would still pass.

This file closes that gap by driving ``build_routes`` in child Python
processes that do NOT import ``tests/conftest``. Each branch spawns a fresh
interpreter, builds the real Starlette app with the real a2a-sdk (when
installed), and asserts the contract via ``TestClient``.

The gate is intentionally LOUD-skip (not silent-green) when the real
``a2a-sdk`` is absent, so a missing dependency cannot mask a regression.

Scope
-----

Only the four ``build_routes`` branches described in issue #88 are covered
here. The existing ``tests/test_boot_routes.py`` covers in-process shape;
``tests/test_credential_helper_subprocess.py`` covers the git credential
subcontract; ``tests/test_boot_register_retry.py`` covers boot-register
retry. This file does NOT expand beyond the build_routes contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Child-process harness
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    """Return the repository root (cwd when pytest collects this file)."""
    return str(Path.cwd().resolve())


def _real_a2a_available() -> bool:
    """Probe a fresh interpreter for the real a2a-sdk server surface."""
    probe = textwrap.dedent(
        """
        import a2a.server.routes
        import a2a.server.request_handlers
        import a2a.server.tasks
        import a2a.types
        print("ok")
        """
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip() == "ok"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


@pytest.fixture(scope="module")
def _require_real_a2a() -> None:
    """Fail-closed: skip the whole module if the real SDK is missing."""
    if not _real_a2a_available():
        pytest.skip(
            "real a2a-sdk not installed in a fresh interpreter — "
            "the build_routes subprocess gate is inert"
        )


def _run_child(body: str) -> dict:
    """Run ``body`` in a clean child Python process and return its JSON stdout.

    The child starts with an empty module cache, so it does NOT see the
    ``a2a.*`` stubs installed by ``tests/conftest.py``.
    """
    root = _repo_root()
    script = (
        f"import json, os, sys\n"
        f"sys.path.insert(0, {root!r})\n"
        + textwrap.dedent(body).lstrip("\n")
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"child process failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise AssertionError(
            f"could not parse child output as JSON: {exc}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from exc


# ---------------------------------------------------------------------------
# Shared child snippets
# ---------------------------------------------------------------------------

_CARD_CONSTRUCTOR = textwrap.dedent(
    """
    from a2a.types import AgentCard, AgentCapabilities, AgentInterface
    card = AgentCard(
        name="real-subprocess-gate",
        description="issue #88 build_routes regression gate",
        version="0.0.0",
        capabilities=AgentCapabilities(),
        skills=[],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            AgentInterface(url="http://test.local", protocol_binding="jsonrpc"),
        ],
    )
    """
).strip()


def _build_app(body: str) -> str:
    """Wrap ``body`` with the common imports/app construction boilerplate."""
    header = (
        "import json\n"
        "from starlette.applications import Starlette\n"
        "from starlette.testclient import TestClient\n"
        "from molecule_runtime.boot_routes import build_routes\n"
        f"{_CARD_CONSTRUCTOR}\n"
    )
    return header + textwrap.dedent(body).lstrip("\n")


# ---------------------------------------------------------------------------
# Branch A: executor is non-None (production happy-path)
# ---------------------------------------------------------------------------

def test_build_routes_branch_a_executor_happy_path_real_subprocess(_require_real_a2a):
    """Real-subprocess Branch A: with a real executor, POST / is wired through
    the real a2a-sdk ``DefaultRequestHandler`` (200 JSON-RPC response), the
    card route serves 200, and there is exactly one route at ``/``.

    Watch-fail: dropping ``create_agent_card_routes`` → card 404.
    Watch-fail: re-coupling the JSON-RPC route to ``adapter.setup()`` →
    POST / 404/405 instead of a real JSON-RPC response.
    """
    body = _build_app(
        textwrap.dedent(
            """
            class _SmokeExecutor:
                async def execute(self, *args, **kwargs):
                    return None

            routes = build_routes(agent_card=card, executor=_SmokeExecutor(), adapter_error=None)
            app = Starlette(routes=routes)
            client = TestClient(app)

            card_resp = client.get("/.well-known/agent-card.json")
            post_resp = client.post(
                "/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tasks/send", "params": {}},
            )
            get_root_resp = client.get("/")

            print(json.dumps({
                "card_status": card_resp.status_code,
                "post_status": post_resp.status_code,
                "post_error_code": post_resp.json().get("error", {}).get("code"),
                "get_root_status": get_root_resp.status_code,
                "route_paths": [getattr(r, "path", None) for r in routes],
            }))
            """
        )
    )
    out = _run_child(body)

    assert out["card_status"] == 200, f"card route did not return 200: {out}"
    assert out["post_status"] == 200, (
        f"POST / did not return 200 (likely not wired to real handler): {out}"
    )
    assert out["post_error_code"] != -32603, (
        f"POST / hit the not-configured handler (-32603) instead of the real handler: {out}"
    )
    assert out["get_root_status"] == 405, (
        f"GET / should be 405 on the JSON-RPC route: {out}"
    )
    assert out["route_paths"].count("/") == 1, (
        f"exactly one route at / expected: {out['route_paths']}"
    )


# ---------------------------------------------------------------------------
# Branch B: executor is None, adapter_error is None
# ---------------------------------------------------------------------------

def test_build_routes_branch_b_not_configured_no_error_real_subprocess(_require_real_a2a):
    """Real-subprocess Branch B: with no executor and no adapter_error, the
    JSON-RPC route returns ``-32603`` with the generic fallback reason, GET /
    is 405, and the card route still serves 200.

    Watch-fail: re-coupling the JSON-RPC route to ``adapter.setup()`` →
    POST / 404/405 instead of structured -32603.
    """
    body = _build_app(
        textwrap.dedent(
            """
            routes = build_routes(agent_card=card, executor=None, adapter_error=None)
            app = Starlette(routes=routes)
            client = TestClient(app)

            card_resp = client.get("/.well-known/agent-card.json")
            post_resp = client.post(
                "/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tasks/send", "params": {}},
            )
            get_root_resp = client.get("/")
            post_json = post_resp.json()

            print(json.dumps({
                "card_status": card_resp.status_code,
                "post_status": post_resp.status_code,
                "error_code": post_json.get("error", {}).get("code"),
                "error_data": post_json.get("error", {}).get("data"),
                "echoed_id": post_json.get("id"),
                "get_root_status": get_root_resp.status_code,
                "route_paths": [getattr(r, "path", None) for r in routes],
            }))
            """
        )
    )
    out = _run_child(body)

    assert out["card_status"] == 200, f"card route did not return 200: {out}"
    assert out["post_status"] == 503, (
        f"POST / did not return the expected 503 JSON-RPC error: {out}"
    )
    assert out["error_code"] == -32603, f"expected -32603, got {out['error_code']}"
    assert "adapter.setup() failed" in str(out["error_data"]), (
        f"generic fallback reason missing: {out}"
    )
    assert out["echoed_id"] == 2, f"request id was not echoed: {out}"
    assert out["get_root_status"] == 405, (
        f"GET / should be 405 on the not-configured route: {out}"
    )
    assert out["route_paths"].count("/") == 1, (
        f"exactly one route at / expected: {out['route_paths']}"
    )


# ---------------------------------------------------------------------------
# Branch C: executor is None, adapter_error is set
# ---------------------------------------------------------------------------

def test_build_routes_branch_c_not_configured_with_error_real_subprocess(_require_real_a2a):
    """Real-subprocess Branch C: with no executor but a concrete
    ``adapter_error`` string, the JSON-RPC ``error.data`` surfaces that reason.

    Watch-fail: build_routes silently drops the error string.
    """
    reason = "MISSING_MODEL: moonshot/kimi-k2.6"
    body_template = textwrap.dedent(
        """
        routes = build_routes(agent_card=card, executor=None, adapter_error=%REASON%)
        app = Starlette(routes=routes)
        client = TestClient(app)

        card_resp = client.get("/.well-known/agent-card.json")
        post_resp = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 3, "method": "tasks/send", "params": {}},
        )
        post_json = post_resp.json()

        print(json.dumps({
            "card_status": card_resp.status_code,
            "post_status": post_resp.status_code,
            "error_code": post_json.get("error", {}).get("code"),
            "error_data": post_json.get("error", {}).get("data"),
            "route_paths": [getattr(r, "path", None) for r in routes],
        }))
        """
    )
    body = _build_app(body_template.replace("%REASON%", repr(reason)))
    out = _run_child(body)

    assert out["card_status"] == 200, f"card route did not return 200: {out}"
    assert out["post_status"] == 503, (
        f"POST / did not return the expected 503 JSON-RPC error: {out}"
    )
    assert out["error_code"] == -32603, f"expected -32603, got {out['error_code']}"
    assert reason in str(out["error_data"]), (
        f"adapter_error string was not surfaced in error.data: {out}"
    )
    assert out["route_paths"].count("/") == 1, (
        f"exactly one route at / expected: {out['route_paths']}"
    )


# ---------------------------------------------------------------------------
# Branch D: empty / falsy inputs
# ---------------------------------------------------------------------------

def test_build_routes_branch_d_empty_inputs_real_subprocess(_require_real_a2a):
    """Real-subprocess Branch D: with all inputs falsy, ``build_routes`` does
    not crash and still mounts at least the card route.

    Watch-fail: a refactor that raises on empty inputs breaks the defensive
    guard for misordered boot.
    """
    body = textwrap.dedent(
        """
        import json
        from molecule_runtime.boot_routes import build_routes

        routes = build_routes(agent_card=None, executor=None, adapter_error=None)
        print(json.dumps({
            "route_paths": [getattr(r, "path", None) for r in routes],
            "has_card_route": any(
                getattr(r, "path", None) == "/.well-known/agent-card.json"
                for r in routes
            ),
        }))
        """
    )
    out = _run_child(body)

    assert out["has_card_route"] is True, (
        f"card route missing with empty inputs: {out}"
    )
    assert out["route_paths"].count("/") == 1, (
        f"exactly one route at / expected: {out['route_paths']}"
    )
