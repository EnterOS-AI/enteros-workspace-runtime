"""Integration-level e2e: NO a2a stubs.

The package-level ``tests/conftest.py`` stubs ``a2a`` + ``claude_agent_sdk``
into ``sys.modules`` at collect time so the fast unit suite can run without
the heavy SDKs installed. The responsiveness e2e is the OPPOSITE contract:
it must drive the *real* a2a-sdk JSON-RPC server over a real socket, so the
stubs would make the test meaningless (a stubbed DefaultRequestHandler does
nothing).

We can't simply delete the root conftest stubs — the unit suite depends on
them. Instead the root conftest now SKIPS its stub installation when the
real ``a2a`` package is importable (CI installs the wheel via ``pip install
-e .``), which is exactly the condition under which this e2e runs. This
conftest additionally asserts the real SDK is present and skips the whole
module — loudly — if it isn't, so the job can never go silently green
against stubs.
"""
from __future__ import annotations

import pytest


def _real_a2a_available() -> bool:
    try:
        import a2a  # noqa: F401
        from a2a.server.request_handlers import DefaultRequestHandler  # noqa: F401
        from a2a.server.routes import (  # noqa: F401
            create_agent_card_routes,
            create_jsonrpc_routes,
        )
        from a2a.types import AgentCard  # noqa: F401

        # The stub DefaultRequestHandler is a bare ``type(...)`` with no
        # __module__ under ``a2a``; the real one lives in the installed
        # package. Guard against a half-stubbed sys.modules.
        return DefaultRequestHandler.__module__.startswith("a2a.")
    except Exception:
        return False


REAL_A2A = _real_a2a_available()


def pytest_collection_modifyitems(config, items):
    """Loud-skip the whole integration module when the real SDK is absent.

    A silent pass against the stubbed SDK would defeat the entire point of
    an over-the-wire test, so we mark every item in this directory skipped
    with an explicit reason rather than letting them import-error or, worse,
    run against the no-op stub handler.
    """
    if REAL_A2A:
        return
    skip = pytest.mark.skip(
        reason=(
            "real a2a-sdk[http-server] not importable — integration e2e needs "
            "the wheel installed (pip install -e .). LOUD SKIP, not a pass."
        )
    )
    for item in items:
        item.add_marker(skip)
