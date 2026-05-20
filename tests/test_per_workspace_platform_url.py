"""Per-workspace platform_url support (RFC#601, task #296 Phase B0).

Covers the additive surface that lets a single external-agent process
register into workspaces on different platform tenants: resolver parses
the optional ``platform_url`` field, platform_auth registers it,
``auth_headers`` derives Origin from it, ``a2a_client._resolve_platform_url``
consults it then falls through to the module-level constant. Back-compat
is the load-bearing invariant — entries / callers without per-workspace
URLs see identical behavior to pre-RFC#601 code.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Reset env between tests. WORKSPACE_ID is RESET to the conftest
    sentinel (not deleted) — a2a_client raises at import time on a
    missing WORKSPACE_ID (CWE-20 gate)."""
    for var in ("MOLECULE_WORKSPACES", "MOLECULE_WORKSPACE_TOKEN", "PLATFORM_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WORKSPACE_ID", "test-conftest-sentinel")


@pytest.fixture(autouse=True)
def _clear_platform_auth_registry():
    """``clear_cache`` covers both the token dict + the new platform_url
    dict (Phase B0 added the latter)."""
    import molecule_runtime.platform_auth as platform_auth

    platform_auth.clear_cache()
    yield
    platform_auth.clear_cache()


def _import_mcp_cli():
    import molecule_runtime.mcp_cli as mcp_cli

    return importlib.reload(mcp_cli)


class TestResolverParsesPlatformUrl:
    def test_per_entry_platform_url_lands_in_third_element(self, monkeypatch):
        monkeypatch.setenv(
            "MOLECULE_WORKSPACES",
            json.dumps([
                {"id": "ws-personal", "token": "tok-p", "platform_url": "https://personal.example"},
                {"id": "ws-staging", "token": "tok-s", "platform_url": "https://staging.example/"},
            ]),
        )
        out, errors = _import_mcp_cli()._resolve_workspaces()
        assert errors == []
        # Trailing slash on second entry is stripped — matches the
        # canonical form mcp_cli stores in os.environ["PLATFORM_URL"].
        assert out == [
            ("ws-personal", "tok-p", "https://personal.example"),
            ("ws-staging", "tok-s", "https://staging.example"),
        ]

    def test_missing_or_blank_platform_url_yields_empty_string(self, monkeypatch):
        # Same-tenant operators omit / blank the field — "" means fall
        # through to module-level PLATFORM_URL. Back-compat invariant.
        monkeypatch.setenv(
            "MOLECULE_WORKSPACES",
            json.dumps([
                {"id": "ws-a", "token": "tok-a"},
                {"id": "ws-b", "token": "tok-b", "platform_url": "   "},
            ]),
        )
        out, errors = _import_mcp_cli()._resolve_workspaces()
        assert errors == []
        assert out == [("ws-a", "tok-a", ""), ("ws-b", "tok-b", "")]

    def test_non_string_platform_url_is_a_validation_error(self, monkeypatch):
        # Loud rejection so a typo doesn't silently route the workspace
        # to the wrong tenant.
        monkeypatch.setenv(
            "MOLECULE_WORKSPACES",
            json.dumps([{"id": "ws-a", "token": "tok-a", "platform_url": 123}]),
        )
        out, errors = _import_mcp_cli()._resolve_workspaces()
        assert out == []
        assert any("platform_url" in e and "must be a string" in e for e in errors)


class TestPlatformAuthRegistry:
    def test_register_and_lookup_strips_trailing_slash(self):
        import molecule_runtime.platform_auth as platform_auth

        platform_auth.register_workspace_platform_url("ws-a", "https://a.example/")
        platform_auth.register_workspace_platform_url("ws-b", "https://b.example")
        assert platform_auth.get_workspace_platform_url("ws-a") == "https://a.example"
        assert platform_auth.get_workspace_platform_url("ws-b") == "https://b.example"
        # Unregistered → None, signalling "fall through to module-level".
        assert platform_auth.get_workspace_platform_url("ws-c") is None

    def test_register_empty_url_or_invalid_id_is_noop(self):
        # Empty URL means "no override". Injection-bearing workspace_id
        # is rejected by the same gate as ``register_workspace_token``
        # so a crafted MOLECULE_WORKSPACES entry can't echo into URLs.
        import molecule_runtime.platform_auth as platform_auth

        platform_auth.register_workspace_platform_url("ws-a", "")
        platform_auth.register_workspace_platform_url("../escape", "https://x.example")
        assert platform_auth.get_workspace_platform_url("ws-a") is None
        assert platform_auth.get_workspace_platform_url("../escape") is None

    def test_register_idempotent_and_rotation_and_clear(self):
        import molecule_runtime.platform_auth as platform_auth

        platform_auth.register_workspace_platform_url("ws-a", "https://a.example")
        platform_auth.register_workspace_platform_url("ws-a", "https://a.example")  # idempotent
        assert platform_auth.get_workspace_platform_url("ws-a") == "https://a.example"
        platform_auth.register_workspace_platform_url("ws-a", "https://new.example")  # rotation
        assert platform_auth.get_workspace_platform_url("ws-a") == "https://new.example"
        platform_auth.clear_cache()
        assert platform_auth.get_workspace_platform_url("ws-a") is None


class TestAuthHeadersOrigin:
    def test_origin_derived_from_per_workspace_url(self, monkeypatch, tmp_path):
        # When the workspace has its own URL registered, Origin points
        # at THAT URL — multi-tenant operators need this so each
        # tenant's edge WAF sees a same-origin request.
        import molecule_runtime.platform_auth as platform_auth

        monkeypatch.setattr(platform_auth, "_token_file", lambda: tmp_path / ".auth_token")
        monkeypatch.setenv("PLATFORM_URL", "https://module-level.example")
        platform_auth.register_workspace_token("ws-personal", "tok-p")
        platform_auth.register_workspace_platform_url("ws-personal", "https://personal.example")

        h = platform_auth.auth_headers("ws-personal")
        assert h["Origin"] == "https://personal.example"
        assert h["Authorization"] == "Bearer tok-p"

    def test_origin_falls_back_to_env_when_workspace_url_unregistered(
        self, monkeypatch, tmp_path
    ):
        # Workspace token registered but NO per-workspace URL — Origin
        # falls through to module-level PLATFORM_URL. Back-compat.
        import molecule_runtime.platform_auth as platform_auth

        monkeypatch.setattr(platform_auth, "_token_file", lambda: tmp_path / ".auth_token")
        monkeypatch.setenv("PLATFORM_URL", "https://module-level.example")
        platform_auth.register_workspace_token("ws-a", "tok-a")

        h = platform_auth.auth_headers("ws-a")
        assert h["Origin"] == "https://module-level.example"

    def test_origin_falls_back_when_no_workspace_id_arg(self, monkeypatch, tmp_path):
        # Single-workspace caller (auth_headers() with no arg) ignores
        # the per-workspace registry — pre-RFC#601 behavior preserved.
        import molecule_runtime.platform_auth as platform_auth

        monkeypatch.setattr(platform_auth, "_token_file", lambda: tmp_path / ".auth_token")
        monkeypatch.setenv("PLATFORM_URL", "https://module-level.example")
        monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "legacy-tok")
        # A per-workspace URL registered for SOME OTHER ws doesn't leak
        # into the no-arg call.
        platform_auth.register_workspace_platform_url("ws-other", "https://other.example")

        h = platform_auth.auth_headers()
        assert h["Origin"] == "https://module-level.example"
        assert h["Authorization"] == "Bearer legacy-tok"


class TestResolvePlatformUrl:
    def test_returns_per_workspace_url_when_registered(self, monkeypatch):
        import molecule_runtime.a2a_client as a2a_client
        import molecule_runtime.platform_auth as platform_auth

        monkeypatch.setattr(a2a_client, "PLATFORM_URL", "http://module-level.example")
        platform_auth.register_workspace_platform_url("ws-a", "https://per-workspace.example")
        assert a2a_client._resolve_platform_url("ws-a") == "https://per-workspace.example"

    def test_falls_back_to_module_constant(self, monkeypatch):
        # src=None / "" / known-but-unregistered all land on the
        # module-level constant. Single-tenant operators (both
        # single-workspace AND same-tenant multi-workspace) see no
        # behaviour change vs pre-RFC#601.
        import molecule_runtime.a2a_client as a2a_client
        import molecule_runtime.platform_auth as platform_auth

        monkeypatch.setattr(a2a_client, "PLATFORM_URL", "http://module-level.example")
        platform_auth.register_workspace_token("ws-a", "tok-a")  # token only, no URL
        assert a2a_client._resolve_platform_url(None) == "http://module-level.example"
        assert a2a_client._resolve_platform_url("") == "http://module-level.example"
        assert a2a_client._resolve_platform_url("ws-a") == "http://module-level.example"


class TestMcpCliWiresPerWorkspacePlatformUrl:
    def test_main_populates_platform_url_registry(self, monkeypatch):
        # End-to-end wiring: mcp_cli reads MOLECULE_WORKSPACES with
        # per-entry platform_url, registers each into platform_auth.
        # Heartbeat + inbox-poller spawn is disabled via env so the
        # test exits cleanly without spinning daemon threads.
        import molecule_runtime.platform_auth as platform_auth

        monkeypatch.setenv("PLATFORM_URL", "https://module-level.example")
        monkeypatch.setenv(
            "MOLECULE_WORKSPACES",
            json.dumps([
                {"id": "ws-a", "token": "tok-a", "platform_url": "https://a.example"},
                {"id": "ws-b", "token": "tok-b"},
            ]),
        )
        monkeypatch.setenv("MOLECULE_MCP_DISABLE_HEARTBEAT", "1")
        monkeypatch.setenv("MOLECULE_MCP_DISABLE_INBOX", "1")
        mcp_cli = _import_mcp_cli()
        # Stub out the heavy MCP server entry point — registry-populate
        # is the side-effect under test, before cli_main() runs.
        monkeypatch.setattr(
            "molecule_runtime.a2a_mcp_server.cli_main", lambda: None, raising=False
        )
        mcp_cli.main()

        # ws-a's URL came from the JSON entry; ws-b had no field so the
        # registry stays None for it (fall-through to module-level
        # PLATFORM_URL applies at HTTP-construct time).
        assert platform_auth.get_workspace_platform_url("ws-a") == "https://a.example"
        assert platform_auth.get_workspace_platform_url("ws-b") is None
        # Tokens both landed regardless of per-entry URL presence.
        assert platform_auth.get_workspace_token("ws-a") == "tok-a"
        assert platform_auth.get_workspace_token("ws-b") == "tok-b"
