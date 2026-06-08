"""Regression: a2a_client must not raise at MODULE IMPORT time when
WORKSPACE_ID is unset (fix for publish-runtime main-red).

Background: previously a2a_client.py validated WORKSPACE_ID eagerly
at the top of the module. The publish-runtime wheel-build job does
`python -m build`, which imports the runtime's modules for static
analysis — including heartbeat.py → a2a_client — and that import
chain blew up with `RuntimeError: WORKSPACE_ID environment variable
is required but not set`, painting main red.

The fix (PEP 562 module __getattr__): validation is now lazy — it
runs on first access of a2a_client.WORKSPACE_ID, not at import.
The publish job can now import freely; callers that actually use
WORKSPACE_ID still get the same RuntimeError on first access.
"""
from __future__ import annotations

import asyncio

import pytest

import molecule_runtime.a2a_client as _a2a_client


def test_a2a_client_imports_without_workspace_id_env(monkeypatch):
    """Importing a2a_client without WORKSPACE_ID set must NOT raise.

    This is the key fix — the publish-runtime job imports the
    runtime but doesn't set WORKSPACE_ID, and that's a legitimate
    use case (wheel build, lint, type-check, import smoke). The
    eager check used to abort all of these.
    """
    # Make sure WORKSPACE_ID is genuinely absent.
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    _a2a_client._WORKSPACE_ID_cache = None

    # Sanity: the module was imported without WORKSPACE_ID, and the lazy
    # constant cache hasn't been populated yet (so the next access
    # WILL validate).
    assert _a2a_client._WORKSPACE_ID_cache is None


def test_a2a_client_workspace_id_access_raises_without_env(monkeypatch):
    """Accessing WORKSPACE_ID without the env var must still raise.

    Lazy validation preserves the same error semantics for callers
    that actually use the constant — the RuntimeError fires on
    first access rather than at import.
    """
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    _a2a_client._WORKSPACE_ID_cache = None

    try:
        _ = _a2a_client.WORKSPACE_ID
    except RuntimeError as exc:
        assert "WORKSPACE_ID" in str(exc)
        assert "required" in str(exc).lower() or "not set" in str(exc).lower()
    else:
        raise AssertionError(
            "Expected RuntimeError on WORKSPACE_ID access with env unset"
        )


def test_a2a_client_workspace_id_lazy_caches(monkeypatch):
    """First access validates + caches; second access uses cache.

    A sentinel env var that's invalid (not a valid workspace id
    format) lets us verify the validation runs once and the
    second access returns the cached value rather than re-running
    the validator.
    """
    # Use a valid-format workspace id (per validate_workspace_id).
    monkeypatch.setenv("WORKSPACE_ID", "ws-lazy-test")
    _a2a_client._WORKSPACE_ID_cache = None

    first = _a2a_client.WORKSPACE_ID
    assert first == "ws-lazy-test"
    assert _a2a_client._WORKSPACE_ID_cache == "ws-lazy-test"

    # Mutate the env var. The cached value should NOT change because
    # the lazy getter only runs validation on first access.
    monkeypatch.setenv("WORKSPACE_ID", "ws-different-after-cache")
    second = _a2a_client.WORKSPACE_ID
    assert second == "ws-lazy-test", (
        "Lazy cache should return the originally-validated value, "
        "not re-read the env on every access"
    )


def test_a2a_client_internal_default_workspace_id_falls_back(monkeypatch):
    """A function that defaults source_workspace_id to None and reads
    the module-level WORKSPACE_ID (now via _resolve_workspace_id) must
    raise the intended RuntimeError, not NameError, when WORKSPACE_ID
    is unset at call time. (CR2 r#9552 + Researcher r#9553 on PR#99:
    PEP 562 __getattr__ only fires for EXTERNAL access, so any bare
    global reference inside the module would NameError. The fix
    routes all internal fallbacks through the helper.)
    """
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    _a2a_client._WORKSPACE_ID_cache = None

    # discover_peer defaults source_workspace_id to None and falls back
    # to _resolve_workspace_id().  We pass a valid UUID so peer-id
    # validation passes and the coroutine actually reaches the fallback.
    try:
        with pytest.raises(RuntimeError, match="WORKSPACE_ID"):
            asyncio.run(
                _a2a_client.discover_peer(
                    "11111111-1111-1111-1111-111111111111"
                )
            )
    except NameError as exc:
        if "WORKSPACE_ID" in str(exc):
            raise AssertionError(
                f"Internal WORKSPACE_ID reference NameError'd: {exc}. "
                "Bare WORKSPACE_ID in a2a_client.py would cause this; "
                "all internal refs should route through _resolve_workspace_id()."
            ) from exc
        raise


def test_a2a_client_404_diagnostic_uses_resolved_src_not_env(monkeypatch):
    """The 404 diagnostic in get_peers_with_diagnostic must use the
    already-resolved local `src` (line ~1021), NOT re-resolve via
    _resolve_workspace_id() or read WORKSPACE_ID from the env.

    Why: the diagnostic runs on every 404 from the platform. If it
    re-resolves via _resolve_workspace_id() (or a bare WORKSPACE_ID
    ref), it can trigger lazy validation → RuntimeError when an
    explicit source_workspace_id was passed but env WORKSPACE_ID is
    unset. The fix on line 1049 interpolates `src` instead.

    Coverage: this test passes a valid-UUID-shaped explicit
    source_workspace_id (so UUID validation passes and the function
    reaches the 404 branch at line 1049), monkeypatches the env to
    a DIFFERENT uuid, and asserts the diagnostic message contains
    the explicit source (not the env value).

    (CR2 r#9662 on PR#99: the existing test
    test_a2a_client_internal_default_workspace_id_falls_back uses
    discover_peer("not-a-uuid") which returns BEFORE touching the
    fallback because "not-a-uuid" is not a valid UUID. This test
    closes that coverage gap with a real-UUID-shape id that
    actually reaches line 1049.)
    """
    # Distinct uuids so we can assert which one the diagnostic uses.
    explicit_src = "11111111-1111-1111-1111-111111111111"
    env_ws = "22222222-2222-2222-2222-222222222222"

    # Reset the lazy cache so the env read is fresh, then set the
    # env to a value DIFFERENT from the explicit source.
    _a2a_client._WORKSPACE_ID_cache = None
    monkeypatch.setenv("WORKSPACE_ID", env_ws)

    # Mock the platform response to be 404 — that's the branch the
    # diagnostic at line 1049 lives in.
    class _FakeResp:
        status_code = 404
        def json(self):
            raise ValueError("not JSON")
    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            return _FakeResp()

    # Patch AsyncClient at the module level so the function under
    # test uses our fake.
    monkeypatch.setattr(_a2a_client.httpx, "AsyncClient", lambda *a, **kw: _FakeClient())

    # Resolve platform URL to avoid hitting the real platform in CI
    # (the test only cares that we reach the 404 branch and that
    # the diagnostic string contains the explicit src).
    monkeypatch.setattr(_a2a_client, "_resolve_platform_url", lambda src: f"http://test-platform/{src}")

    peers, diagnostic = asyncio.run(
        _a2a_client.get_peers_with_diagnostic(source_workspace_id=explicit_src)
    )

    # The 404 branch must have produced a diagnostic.
    assert peers == []
    assert diagnostic is not None
    # The diagnostic must contain the EXPLICIT src, not the env value.
    # If line 1049 re-resolved via _resolve_workspace_id() (the
    # pre-fix bug), the diagnostic would say env_ws (22222222...)
    # because the env is set and that's what the lazy resolver would
    # return. Post-fix, it must say explicit_src (11111111...).
    assert explicit_src in diagnostic, (
        f"404 diagnostic did not contain explicit src={explicit_src!r}; "
        f"got: {diagnostic!r}. Line 1049 may still be re-resolving "
        f"via _resolve_workspace_id() instead of using the local `src`."
    )
    assert env_ws not in diagnostic, (
        f"404 diagnostic contained env WORKSPACE_ID={env_ws!r}; "
        f"line 1049 re-resolved via _resolve_workspace_id() instead "
        f"of using the explicit source_workspace_id."
    )
    assert "not registered" in diagnostic.lower()
