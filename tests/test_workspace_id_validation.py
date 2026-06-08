"""Regression tests for WORKSPACE_ID validation (CWE-20, issue #14).

Originally landed as ``tests/test_workspace_id_validation.py`` alongside
standalone PR #29 in the 0.1.x line. Dropped during the standalone-SSOT
0.2.0 restructure (PR #19) and restored here so 0.2.0 doesn't ship with
a known security regression.

Key differences from the original 0.1.x test:

  - The original validated WORKSPACE_ID at module import time
    (``WORKSPACE_ID: str = validate_workspace_id(os.environ.get(...))``).
    The SSOT 0.2.0 codebase supports multi-workspace external-runtime
    mode where the legacy WORKSPACE_ID env var is unset, so eager
    import-time validation would crash the universal MCP server.
    Validation is now lazy: ``get_workspace_id()`` validates on first
    call and caches the result.
  - ``register_workspace_token()`` validates each per-workspace ID in
    the multi-workspace registry, so the CWE-20 surface is closed for
    both the single- and multi-workspace paths.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Clear the per-process cache before each test and ensure the
    test does not leak a real WORKSPACE_ID into the validator."""
    import molecule_runtime.platform_auth as pa_mod
    pa_mod._reset_workspace_id_cache()
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    yield
    pa_mod._reset_workspace_id_cache()


class TestValidateWorkspaceId:
    """validate_workspace_id() must reject injection characters and
    accept the lowercase-alphanumeric-plus-hyphens shape used by
    platform-generated UUIDs and org-generated alphanumeric IDs."""

    def test_rejects_empty_string(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="empty"):
            validate_workspace_id("")

    def test_rejects_whitespace_only(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("   ")

    def test_rejects_slash(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws/foo")

    def test_rejects_double_dot(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws..foo")

    def test_rejects_hash_fragment(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws#foo")

    def test_rejects_question_mark(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws?foo")

    def test_rejects_ampersand(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws&foo")

    def test_rejects_backslash(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws\\foo")

    def test_rejects_newline(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws\nfoo")

    def test_rejects_carriage_return(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws\rfoo")

    def test_rejects_null_byte(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("ws\x00foo")

    def test_rejects_uppercase(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("Test-Workspace")

    def test_rejects_starts_with_hyphen(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("-test-workspace")

    def test_rejects_underscore(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("test_workspace")

    def test_rejects_space(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id("test workspace")

    def test_rejects_over_max_length(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        # Regex caps the post-leading-char part at 127 chars, so 129 total fails.
        too_long = "a" + ("b" * 128)
        with pytest.raises(ValueError, match="invalid characters"):
            validate_workspace_id(too_long)

    def test_accepts_valid_uuid(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        wsid = "53255246-1f34-432c-87e5-ae07f888f905"
        assert validate_workspace_id(wsid) == wsid

    def test_accepts_simple_alphanumeric(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        assert validate_workspace_id("test-workspace") == "test-workspace"

    def test_accepts_numeric_start(self):
        from molecule_runtime.platform_auth import validate_workspace_id
        assert validate_workspace_id("0a1b2c3d-ef") == "0a1b2c3d-ef"

    def test_strips_surrounding_whitespace(self):
        """Whitespace around an otherwise-valid ID is stripped before
        regex check (matches the original PR #29 contract)."""
        from molecule_runtime.platform_auth import validate_workspace_id
        assert validate_workspace_id("  test-ws  ") == "test-ws"


class TestGetWorkspaceId:
    """get_workspace_id() reads WORKSPACE_ID env var, validates, caches."""

    def test_raises_on_unset_env(self):
        from molecule_runtime.platform_auth import get_workspace_id
        with pytest.raises(ValueError, match="empty"):
            get_workspace_id()

    def test_raises_on_malformed_env(self, monkeypatch):
        from molecule_runtime.platform_auth import get_workspace_id
        monkeypatch.setenv("WORKSPACE_ID", "bad/ws")
        with pytest.raises(ValueError, match="invalid characters"):
            get_workspace_id()

    def test_returns_validated_value_on_good_env(self, monkeypatch):
        from molecule_runtime.platform_auth import get_workspace_id
        monkeypatch.setenv("WORKSPACE_ID", "good-ws-123")
        assert get_workspace_id() == "good-ws-123"

    def test_caches_result(self, monkeypatch):
        """Second call returns the same validated value without
        re-reading the environment — workspace IDs are immutable per
        container lifetime."""
        import molecule_runtime.platform_auth as pa_mod
        monkeypatch.setenv("WORKSPACE_ID", "first-ws")
        first = pa_mod.get_workspace_id()
        # Mutating the env after the first call MUST NOT change the
        # returned value (the validator is intentionally one-shot).
        monkeypatch.setenv("WORKSPACE_ID", "different-ws")
        second = pa_mod.get_workspace_id()
        assert first == second == "first-ws"


class TestRegisterWorkspaceTokenValidation:
    """register_workspace_token() must reject malformed workspace IDs
    so the multi-workspace registry can't store an injection-bearing
    ID that get_workspace_token() returns verbatim."""

    def test_rejects_malformed_id_silently(self, caplog):
        """Invalid IDs are dropped with a warning — operator's mcp_cli
        loop must not crash on a single bad entry in MOLECULE_WORKSPACES."""
        from molecule_runtime.platform_auth import (
            register_workspace_token,
            get_workspace_token,
        )
        register_workspace_token("bad/ws/id", "some-token")
        assert get_workspace_token("bad/ws/id") is None

    def test_accepts_valid_id(self):
        import molecule_runtime.platform_auth as pa_mod
        from molecule_runtime.platform_auth import (
            register_workspace_token,
            get_workspace_token,
        )
        try:
            register_workspace_token("good-ws-1", "t1")
            assert get_workspace_token("good-ws-1") == "t1"
        finally:
            pa_mod.clear_cache()

    def test_strips_whitespace_then_validates(self):
        import molecule_runtime.platform_auth as pa_mod
        from molecule_runtime.platform_auth import (
            register_workspace_token,
            get_workspace_token,
        )
        try:
            register_workspace_token("  good-ws-2  ", "t2")
            assert get_workspace_token("good-ws-2") == "t2"
        finally:
            pa_mod.clear_cache()


class TestRefreshFromDiskAlias:
    """refresh_from_disk is preserved as a back-compat alias for
    callers/tests that imported the original PR #1877 symbol."""

    def test_refresh_from_disk_is_refresh_cache(self):
        from molecule_runtime import platform_auth
        assert platform_auth.refresh_from_disk is platform_auth.refresh_cache


class TestLazyA2aClientImport:
    """a2a_client.WORKSPACE_ID must not be validated at import time (issue #1180).

    Smoke tests, lint scans, and IDE autocomplete import the module without
    setting the env var. Validation is deferred to first use.
    """

    def test_import_succeeds_without_workspace_id(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        # Force re-import by removing from sys.modules
        import sys
        for mod in list(sys.modules):
            if "molecule_runtime.a2a_client" in mod:
                del sys.modules[mod]
        import molecule_runtime.a2a_client as ac
        assert ac is not None

    def test_first_access_raises_when_workspace_id_unset(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        import sys
        for mod in list(sys.modules):
            if "molecule_runtime.a2a_client" in mod:
                del sys.modules[mod]
        import molecule_runtime.a2a_client as ac
        with pytest.raises(RuntimeError, match="WORKSPACE_ID environment variable is required"):
            str(ac.WORKSPACE_ID)

    def test_first_access_returns_validated_id_when_set(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ID", "00000000-0000-0000-0000-000000000001")
        import sys
        for mod in list(sys.modules):
            if "molecule_runtime.a2a_client" in mod:
                del sys.modules[mod]
        import molecule_runtime.a2a_client as ac
        assert ac.WORKSPACE_ID == "00000000-0000-0000-0000-000000000001"

    def test_get_workspace_id_returns_real_str(self, monkeypatch):
        """Internal callers pass the id into httpx headers which require
        real ``str``/``bytes`` — the accessor must not return a lazy
        sentinel (issue #1180 CR2)."""
        monkeypatch.setenv("WORKSPACE_ID", "test-ws-1180")
        import sys
        for mod in list(sys.modules):
            if "molecule_runtime.a2a_client" in mod:
                del sys.modules[mod]
        import molecule_runtime.a2a_client as ac
        wsid = ac.get_workspace_id()
        assert isinstance(wsid, str)
        assert wsid == "test-ws-1180"

    @pytest.mark.asyncio
    async def test_discover_peer_uses_real_str_header(self, monkeypatch):
        """discover_peer() with no source_workspace_id must pass a real
        ``str`` (not a sentinel) as the X-Workspace-ID header value."""
        monkeypatch.setenv("WORKSPACE_ID", "peer-test-ws")
        import sys
        for mod in list(sys.modules):
            if "molecule_runtime.a2a_client" in mod:
                del sys.modules[mod]
        import molecule_runtime.a2a_client as a2a_client

        captured: dict = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"id": "x", "name": "y"}

        class _Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return None
            async def get(self, url, headers):
                captured["headers"] = headers
                return _Resp()

        monkeypatch.setattr(a2a_client.httpx, "AsyncClient", lambda timeout: _Client())
        await a2a_client.discover_peer("11111111-1111-1111-1111-111111111111")
        x_ws_id = captured["headers"]["X-Workspace-ID"]
        assert isinstance(x_ws_id, str)
        assert x_ws_id == "peer-test-ws"
