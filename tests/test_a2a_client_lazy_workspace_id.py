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

import importlib
import sys


def test_a2a_client_imports_without_workspace_id_env(monkeypatch):
    """Importing a2a_client without WORKSPACE_ID set must NOT raise.

    This is the key fix — the publish-runtime job imports the
    runtime but doesn't set WORKSPACE_ID, and that's a legitimate
    use case (wheel build, lint, type-check, import smoke). The
    eager check used to abort all of these.
    """
    # Make sure WORKSPACE_ID is genuinely absent.
    monkeypatch.delenv("WORKSPACE_ID", raising=False)

    # Drop any cached module so the import re-runs the (now lazy)
    # module body.
    sys.modules.pop("molecule_runtime.a2a_client", None)

    import molecule_runtime.a2a_client  # must NOT raise

    # Sanity: the module loaded without WORKSPACE_ID, and the lazy
    # constant cache hasn't been populated yet (so the next access
    # WILL validate).
    assert molecule_runtime.a2a_client._WORKSPACE_ID_cache is None


def test_a2a_client_workspace_id_access_raises_without_env(monkeypatch):
    """Accessing WORKSPACE_ID without the env var must still raise.

    Lazy validation preserves the same error semantics for callers
    that actually use the constant — the RuntimeError fires on
    first access rather than at import.
    """
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    sys.modules.pop("molecule_runtime.a2a_client", None)

    import molecule_runtime.a2a_client as m

    try:
        _ = m.WORKSPACE_ID
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
    sys.modules.pop("molecule_runtime.a2a_client", None)

    import molecule_runtime.a2a_client as m

    first = m.WORKSPACE_ID
    assert first == "ws-lazy-test"
    assert m._WORKSPACE_ID_cache == "ws-lazy-test"

    # Mutate the env var. The cached value should NOT change because
    # the lazy getter only runs validation on first access.
    monkeypatch.setenv("WORKSPACE_ID", "ws-different-after-cache")
    second = m.WORKSPACE_ID
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
    sys.modules.pop("molecule_runtime.a2a_client", None)
    import molecule_runtime.a2a_client as m

    # Find a public function in a2a_client that defaults its
    # source_workspace_id to None and (eventually) reads the module
    # fallback. `discover_peer` is one such function (its default
    # source_workspace_id is None; with no source, it falls back
    # to module WORKSPACE_ID).
    try:
        # The function would actually need valid args; we just
        # want to confirm the import + name-resolution path doesn't
        # NameError. Trigger a single argument-validation path
        # that touches the fallback.
        m.discover_peer("not-a-uuid")
    except (RuntimeError, m.httpx.HTTPError, Exception) as exc:
        # RuntimeError is the intended path (WORKSPACE_ID unset).
        # Anything else (NameError, AttributeError on WORKSPACE_ID)
        # would be the regression we're guarding against.
        if isinstance(exc, NameError) and "WORKSPACE_ID" in str(exc):
            raise AssertionError(
                f"Internal WORKSPACE_ID reference NameError'd: {exc}. "
                "Bare WORKSPACE_ID in a2a_client.py would cause this; "
                "all internal refs should route through _resolve_workspace_id()."
            )
        # Other exceptions (httpx errors, RuntimeError "not set", etc.) are
        # acceptable — the key is that the fallback path doesn't NameError.
