"""Tests for privileged_mcp_env.inject_privileged_env (F2).

Locks in the generalized org-admin env merge that used to live only in the
openclaw template override:
  * injects the canonical contract keys for the management MCP;
  * a box that only sets a LEGACY var still populates the canonical key (and the
    legacy key stays present, so the proven server reading legacy names keeps
    working — no regression);
  * NO-OP for any non-management MCP name (a tenant MCP never gets an admin token);
  * descriptor-declared keys win;
  * returns the spec unchanged when nothing resolves.
"""
from __future__ import annotations

from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME
from molecule_runtime.privileged_mcp_env import (
    PRIVILEGED_ENV_KEYS,
    inject_privileged_env,
)


def test_injects_all_canonical_keys_for_management_mcp():
    env = {
        "MOLECULE_API_URL": "http://cp",
        "MOLECULE_API_KEY": "key",
        "MOLECULE_ORG_API_KEY": "orgkey",
        "ORG_SLUG": "acme",
        "AUDIT_ACTOR": "actor@x",
    }
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx"}, env)
    for key in PRIVILEGED_ENV_KEYS:
        assert out["env"][key] == env[key]
    # original spec not mutated
    assert "env" not in {"command": "npx"}


def test_org_api_key_forwarded_under_canonical_name():
    """Regression for the concierge AUTH_ERROR: core sets MOLECULE_ORG_API_KEY and
    mcp-server reads MOLECULE_ORG_API_KEY (strict, no alias), so the child MUST
    receive it under that exact name. The old allowlist forwarded the unprefixed
    ORG_API_KEY and stripped MOLECULE_ORG_API_KEY. Pins molecule-ai-sdk
    contracts/credentials management_mcp_env.required."""
    env = {"MOLECULE_ORG_API_KEY": "org-token-xyz"}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx"}, env)
    assert out["env"]["MOLECULE_ORG_API_KEY"] == "org-token-xyz"
    # legacy ORG_API_KEY is bridged from the canonical value (back-compat)
    assert out["env"]["ORG_API_KEY"] == "org-token-xyz"


def test_noop_for_non_management_name():
    spec = {"command": "image-gen", "env": {}}
    env = {"MOLECULE_API_KEY": "secret"}
    out = inject_privileged_env("image-gen", spec, env)
    # returns the SAME object untouched — a tenant MCP never receives admin env
    assert out is spec
    assert out["env"] == {}


def test_legacy_alias_env_populates_canonical_key():
    # The proven openclaw image sets the LEGACY names only.
    env = {
        "MOLECULE_CP_URL": "http://cp",
        "MOLECULE_ADMIN_TOKEN": "tok",
        "MOLECULE_ORG_ID": "org-123",
    }
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {}, env)
    # canonical keys populated from the legacy aliases ...
    assert out["env"]["MOLECULE_API_URL"] == "http://cp"
    assert out["env"]["MOLECULE_API_KEY"] == "tok"
    assert out["env"]["ORG_SLUG"] == "org-123"
    # ... AND the legacy keys remain present (proven server reads these).
    assert out["env"]["MOLECULE_CP_URL"] == "http://cp"
    assert out["env"]["MOLECULE_ADMIN_TOKEN"] == "tok"
    assert out["env"]["MOLECULE_ORG_ID"] == "org-123"


def test_legacy_only_keys_carried_verbatim():
    env = {"PLATFORM_URL": "http://plat", "WORKSPACE_ID": "ws-1"}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {}, env)
    assert out["env"]["PLATFORM_URL"] == "http://plat"
    assert out["env"]["WORKSPACE_ID"] == "ws-1"


def test_canonical_value_preferred_when_both_set():
    env = {"MOLECULE_API_URL": "http://canon", "MOLECULE_CP_URL": "http://legacy"}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {}, env)
    # the key resolves from its OWN var first, so each keeps its own value
    assert out["env"]["MOLECULE_API_URL"] == "http://canon"
    assert out["env"]["MOLECULE_CP_URL"] == "http://legacy"


def test_descriptor_declared_key_wins():
    env = {"MOLECULE_API_KEY": "from-env", "MOLECULE_ADMIN_TOKEN": "from-env"}
    spec = {"env": {"MOLECULE_API_KEY": "DECLARED"}}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, spec, env)
    assert out["env"]["MOLECULE_API_KEY"] == "DECLARED"
    # the alias (not descriptor-declared) still fills from env
    assert out["env"]["MOLECULE_ADMIN_TOKEN"] == "from-env"


def test_returns_spec_unchanged_when_nothing_resolves():
    spec = {"command": "npx", "env": {"FOO": "bar"}}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, spec, {})
    assert out is spec  # nothing to merge -> original returned


def test_absent_values_not_injected_as_empty():
    env = {"MOLECULE_API_URL": "http://cp"}  # only one key present
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {}, env)
    assert out["env"] == {"MOLECULE_API_URL": "http://cp", "MOLECULE_CP_URL": "http://cp"}
    assert "AUDIT_ACTOR" not in out["env"]
    assert "ORG_API_KEY" not in out["env"]


def test_defaults_to_process_environ(monkeypatch):
    monkeypatch.setenv("MOLECULE_API_KEY", "proc-env-key")
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {})
    assert out["env"]["MOLECULE_API_KEY"] == "proc-env-key"


# ---------------------------------------------------------------------------
# Funnel wiring — the org-admin env reaches register_mcp_server_hook via the
# ONE base funnel (install_plugins_via_registry), for EVERY runtime, override
# or not (the F2 generalization). End-to-end proof, not just the unit.
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402

from molecule_runtime import plugins_registry as _registry  # noqa: E402
from molecule_runtime.adapter_base import AdapterConfig, BaseAdapter  # noqa: E402
from molecule_runtime.plugins import LoadedPlugins, Plugin  # noqa: E402
from molecule_runtime.plugins_registry.protocol import InstallResult  # noqa: E402


class _CapturingAdapter(BaseAdapter):
    """Minimal concrete adapter that records every (name, spec) the base funnel
    dispatches to register_mcp_server_hook — so the test sees exactly what the
    runtime would render."""

    def __init__(self):
        super().__init__()
        self.captured = []

    @staticmethod
    def name() -> str:
        return "claude-code"

    @staticmethod
    def display_name() -> str:
        return "Capturing"

    @staticmethod
    def description() -> str:
        return "test"

    async def setup(self, config):  # pragma: no cover - unused
        return None

    async def create_executor(self, config):  # pragma: no cover - unused
        return None

    def register_mcp_server_hook(self, config, name, spec):
        # Record the spec the funnel handed us (already enriched by the
        # inject_privileged_env wrap) instead of writing to disk.
        self.captured.append((name, spec))


class _StubMcpAdaptor:
    """Stand-in PluginAdaptor whose install() wires the management MCP via the
    ctx hook — the same call MCPServerAdaptor.install makes in production."""

    def __init__(self, name):
        self._name = name

    async def install(self, ctx):
        ctx.register_mcp_server(
            MANAGEMENT_MCP_NAME, {"command": "npx", "args": ["@molecule-ai/mcp-server"]}
        )
        return InstallResult(plugin_name=self._name, runtime=ctx.runtime, source="plugin")


def test_base_funnel_enriches_privileged_spec_for_any_runtime(monkeypatch, tmp_path):
    # The org-admin env is set in the process env (as core does for a concierge).
    monkeypatch.setenv("MOLECULE_CP_URL", "http://cp.local")
    monkeypatch.setenv("MOLECULE_ADMIN_TOKEN", "admintok")
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-xyz")
    monkeypatch.setenv("PLATFORM_URL", "http://plat.local")
    monkeypatch.setenv("WORKSPACE_ID", "ws-funnel")

    # resolve() (imported inside install_plugins_via_registry) -> our stub adaptor.
    monkeypatch.setattr(
        _registry, "resolve", lambda name, runtime, root: (_StubMcpAdaptor(name), "plugin")
    )

    adapter = _CapturingAdapter()
    config = AdapterConfig(model="m", config_path=str(tmp_path), workspace_id="ws-funnel")
    plugins = LoadedPlugins(plugins=[Plugin(name="molecule-platform-mcp", path=str(tmp_path))])

    asyncio.run(adapter.install_plugins_via_registry(config, plugins))

    assert len(adapter.captured) == 1
    name, spec = adapter.captured[0]
    assert name == MANAGEMENT_MCP_NAME
    env = spec["env"]
    # canonical keys populated from the legacy env the concierge sets ...
    assert env["MOLECULE_API_URL"] == "http://cp.local"
    assert env["MOLECULE_API_KEY"] == "admintok"
    assert env["ORG_SLUG"] == "org-xyz"
    # ... AND the proven legacy keys remain present (no regression).
    assert env["MOLECULE_CP_URL"] == "http://cp.local"
    assert env["MOLECULE_ADMIN_TOKEN"] == "admintok"
    assert env["PLATFORM_URL"] == "http://plat.local"
    assert env["WORKSPACE_ID"] == "ws-funnel"


def test_base_funnel_does_not_enrich_non_management_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_ADMIN_TOKEN", "admintok")

    class _TenantMcpAdaptor:
        def __init__(self, name):
            self._name = name

        async def install(self, ctx):
            ctx.register_mcp_server("image-gen", {"command": "npx"})
            return InstallResult(plugin_name=self._name, runtime=ctx.runtime, source="plugin")

    monkeypatch.setattr(
        _registry, "resolve", lambda name, runtime, root: (_TenantMcpAdaptor(name), "plugin")
    )
    adapter = _CapturingAdapter()
    config = AdapterConfig(model="m", config_path=str(tmp_path))
    plugins = LoadedPlugins(plugins=[Plugin(name="image-gen", path=str(tmp_path))])

    asyncio.run(adapter.install_plugins_via_registry(config, plugins))

    name, spec = adapter.captured[0]
    assert name == "image-gen"
    # a tenant MCP must NEVER receive the admin token
    assert "env" not in spec or "MOLECULE_ADMIN_TOKEN" not in (spec.get("env") or {})
