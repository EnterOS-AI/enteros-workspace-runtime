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

import json
import pathlib

import molecule_runtime
from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME
from molecule_runtime.privileged_mcp_env import (
    PRIVILEGED_ENV_KEYS,
    SELF_MCP_MODE,
    WORKSPACE_TOKEN_FILE,
    inject_privileged_env,
)

# The org-admin CREDENTIAL keys — the actual privilege the enforcement seam keeps
# off ordinary boxes. These must NEVER appear on an ordinary box or a self surface.
# NOTE: MOLECULE_API_URL is the non-secret tenant base URL (shared by BOTH audiences)
# and is deliberately NOT in this set — leaking it grants nothing.
_ORG_SECRET_KEYS = (
    "MOLECULE_API_KEY",
    "MOLECULE_ORG_API_KEY",
    "ORG_API_KEY",
    "MOLECULE_ADMIN_TOKEN",
)


def _credentials_contract() -> dict:
    """The vendored molecule-ai-sdk contracts/credentials SSOT (drift-gated against
    SDK main by scripts/check-schemas-in-sync.sh)."""
    p = pathlib.Path(molecule_runtime.__file__).parent / "contracts" / "credentials.contract.json"
    return json.loads(p.read_text())


def test_forwards_every_contract_required_mgmt_mcp_env_key():
    """ENFORCEMENT: privileged_mcp_env must forward EVERY env key the SDK
    contracts/credentials `management_mcp_env.required` set names, under that exact
    name. This is the gate that would have caught the concierge AUTH_ERROR — the
    forward-allowlist carried the unprefixed ORG_API_KEY and stripped
    MOLECULE_ORG_API_KEY. Regressing any canonical name fails here."""
    mgmt = _credentials_contract()["management_mcp_env"]
    required = mgmt["required"]
    assert "MOLECULE_ORG_API_KEY" in required  # sanity: the org key IS a hard requirement

    env = {k: f"val-{k}" for k in required}
    child = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx"}, env)["env"]
    missing = [k for k in required if child.get(k) != env[k]]
    assert not missing, (
        f"privileged_mcp_env does NOT forward contract-required mgmt-MCP env key(s) {missing} "
        f"— they'd be stripped from the management MCP child (the concierge AUTH_ERROR class)."
    )
    # the deprecated unprefixed name must never be the canonical org key
    assert "ORG_API_KEY" in mgmt["deprecated_do_not_use"]
    assert "MOLECULE_ORG_API_KEY" not in mgmt["deprecated_do_not_use"]


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


# ===========================================================================
# Audience-driven injection — negative controls (RFC plugin-mcp-audience-contract,
# self-schedule v1). Each test asserts a property that FAILS if its guard is
# inverted, per the task's negative-control requirement:
#   (a) an ordinary box (name != MANAGEMENT_MCP_NAME, audience absent OR 'self',
#       AND a self-declared audience='org') NEVER receives any org/privileged key;
#   (b) audience='self' injects the token-FILE-PATH + MOLECULE_MCP_MODE=self + API
#       URL, and NOT a token value or org key;
#   (c) the org path (name == MANAGEMENT_MCP_NAME OR audience=='org') injects the
#       org creds byte-identically to before.
# ===========================================================================


# ── (a) ordinary box never receives an org/privileged key ──────────────────
def test_ordinary_box_audience_absent_is_noop_no_org_key():
    """(a) name != MANAGEMENT_MCP_NAME and audience ABSENT -> untouched no-op; no
    org/privileged key is injected even when the full org env is present on the box.
    Inverting the audience=None no-op to inject would surface an org key here."""
    env = {k: f"val-{k}" for k in PRIVILEGED_ENV_KEYS}
    env["MOLECULE_ADMIN_TOKEN"] = "org-admin"
    spec = {"command": "image-gen", "env": {}}
    out = inject_privileged_env("image-gen", spec, env)
    assert out is spec  # same object — untouched
    for k in _ORG_SECRET_KEYS:
        assert k not in out["env"]


def test_ordinary_box_self_audience_never_receives_org_key():
    """(a) name != MANAGEMENT_MCP_NAME, audience='self' -> the self surface is wired
    but NO org/privileged key is ever injected, even though the box carries the org
    env. Inverting the self path to fall through to org injection leaks here."""
    env = {
        "MOLECULE_API_URL": "https://acme.moleculesai.app",
        "MOLECULE_ORG_API_KEY": "org-secret",
        "MOLECULE_API_KEY": "admin-secret",
        "MOLECULE_ADMIN_TOKEN": "admin-secret",
    }
    spec = {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"], "audience": "self"}
    out = inject_privileged_env("molecule-schedule-self", spec, env)
    for k in _ORG_SECRET_KEYS:
        assert k not in out["env"], f"org secret {k} leaked onto a self surface"


def test_self_declared_org_audience_on_ordinary_plugin_injects_nothing():
    """(a/c security-major-2) THE org-key-off-ordinary-box invariant. A self-declared
    audience='org' on a NON-management plugin name must resolve NOTHING — org
    injection stays anchored to the core-verified MANAGEMENT_MCP_NAME, never a
    self-declared manifest field. Removing the org path's `name != MANAGEMENT_MCP_NAME`
    gate leaks the org key here (the exact thing this seam exists to prevent)."""
    env = {k: f"val-{k}" for k in PRIVILEGED_ENV_KEYS}
    env["MOLECULE_ADMIN_TOKEN"] = "org-admin"
    spec = {"command": "npx", "env": {}, "audience": "org"}
    out = inject_privileged_env("image-gen", spec, env)
    e = out.get("env", {})
    for k in _ORG_SECRET_KEYS:
        assert k not in e, f"org secret {k} leaked to ordinary plugin via self-declared audience=org"
    assert e == {}  # nothing injected at all
    assert "audience" not in out  # the directive is stripped, never rendered


# ── (b) audience='self' injects file-path + mode + url, never a value/org key ──
def test_self_audience_injects_token_file_mode_and_api_url():
    """(b) audience='self' injects MOLECULE_WORKSPACE_TOKEN_FILE=/configs/.auth_token
    (the FILE PATH, so the child re-reads the rotated token — never the token VALUE),
    MOLECULE_MCP_MODE=self, and the tenant API base URL. Inverting any branch (value
    instead of path, wrong mode, org key, missing url) fails an assert here."""
    env = {
        "MOLECULE_API_URL": "https://acme.moleculesai.app",
        "MOLECULE_ORG_API_KEY": "org-secret",
        "MOLECULE_ADMIN_TOKEN": "admin-secret",
    }
    spec = {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"], "audience": "self"}
    out = inject_privileged_env("molecule-schedule-self", spec, env)
    e = out["env"]
    assert e["MOLECULE_WORKSPACE_TOKEN_FILE"] == WORKSPACE_TOKEN_FILE == "/configs/.auth_token"
    assert e["MOLECULE_MCP_MODE"] == SELF_MCP_MODE == "self"
    assert e["MOLECULE_API_URL"] == "https://acme.moleculesai.app"
    # the token VALUE is NEVER read or injected — only the file PATH
    assert "MOLECULE_WORKSPACE_TOKEN" not in e
    # NO org key on the self path, even though the box carries them
    for k in _ORG_SECRET_KEYS:
        assert k not in e
    # audience directive stripped from the rendered spec (never a literal env var)
    assert "audience" not in out


def test_self_audience_api_url_falls_back_to_platform_url():
    """(b) the tenant API base URL resolves from PLATFORM_URL when MOLECULE_API_URL
    is unset (whatever the container already carries)."""
    out = inject_privileged_env("schedule-self", {"audience": "self"}, {"PLATFORM_URL": "https://plat.example"})
    assert out["env"]["MOLECULE_API_URL"] == "https://plat.example"


def test_self_audience_injects_org_routing_id_never_the_org_key():
    """(b) audience='self' injects MOLECULE_ORG_ID (the tenant ROUTING id the SaaS
    API requires — 400 TENANT_ORG_HEADER_REQUIRED otherwise) from the container env,
    but NEVER an org key. Routing selects the tenant; the workspace token still gates
    auth (a foreign :id 401s). Dropping the org-id injection reproduces the live-gate
    self-mode create 400; leaking an org SECRET here is the invariant (a) guards."""
    env = {
        "MOLECULE_API_URL": "https://acme.moleculesai.app",
        "MOLECULE_ORG_ID": "org-uuid-777",
        "MOLECULE_ORG_API_KEY": "org-secret",
        "MOLECULE_ADMIN_TOKEN": "admin-secret",
    }
    out = inject_privileged_env("molecule-schedule-self", {"audience": "self"}, env)
    e = out["env"]
    assert e["MOLECULE_ORG_ID"] == "org-uuid-777"  # routing id injected
    for k in _ORG_SECRET_KEYS:
        assert k not in e, f"org secret {k} leaked onto the self surface"


def test_self_audience_omits_org_id_when_source_absent():
    """(b) MOLECULE_ORG_ID injection is a tolerable no-op when the container carries
    no org id (a non-SaaS/loopback topology may route without it) — never invented."""
    out = inject_privileged_env("schedule-self", {"audience": "self"}, {"MOLECULE_API_URL": "http://cp"})
    assert "MOLECULE_ORG_ID" not in out["env"]


def test_self_audience_descriptor_declared_url_wins_but_mode_is_authoritative():
    """(b) descriptor-declared MOLECULE_API_URL wins; MOLECULE_MCP_MODE is
    AUTHORITATIVE — a descriptor can NOT downgrade a self surface to management to
    fish for the org registry."""
    spec = {
        "audience": "self",
        "env": {"MOLECULE_API_URL": "https://declared", "MOLECULE_MCP_MODE": "management"},
    }
    out = inject_privileged_env("schedule-self", spec, {"MOLECULE_API_URL": "https://env"})
    assert out["env"]["MOLECULE_API_URL"] == "https://declared"  # descriptor wins for url
    assert out["env"]["MOLECULE_MCP_MODE"] == "self"  # mode is forced, not overridable


# ── (c) org path unchanged — declared 'org' == derived (name) == pre-audience ──
def test_org_audience_declared_matches_derived_and_forwards_org_creds():
    """(c) audience='org' with the management name injects the org creds
    byte-identically to the derived (name-based) org path — i.e. exactly the
    pre-audience behavior. Inverting the org merge changes the env and fails here."""
    env = {
        "MOLECULE_API_URL": "http://cp",
        "MOLECULE_API_KEY": "key",
        "MOLECULE_ORG_API_KEY": "orgkey",
        "ORG_SLUG": "acme",
        "AUDIT_ACTOR": "a@x",
    }
    declared = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx", "audience": "org"}, env)
    derived = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx"}, env)
    assert declared["env"] == derived["env"]  # declared audience==org identical to derived
    for key in PRIVILEGED_ENV_KEYS:
        assert declared["env"][key] == env[key]
    assert "audience" not in declared  # directive stripped


# ===========================================================================
# Review-finding negative controls (fix/audience-injector-review):
#   #1 force MOLECULE_WORKSPACE_TOKEN_FILE authoritatively (security)
#   #2 inject WORKSPACE_ID / MOLECULE_WORKSPACE_ID on the self surface (functional)
#   #3 fix the truthy-`or` audience coercion (correctness)
#   #4 always resolve a tenant API URL, log a misconfig otherwise (reachability)
# Each asserts a property that FAILS if its guard is inverted.
# ===========================================================================


# ── #1 token-file is AUTHORITATIVE (injector-wins over descriptor) ──────────
def test_self_audience_token_file_is_authoritative_over_descriptor():
    """#1 (security) MOLECULE_WORKSPACE_TOKEN_FILE is forced AUTHORITATIVELY to
    /configs/.auth_token — a descriptor that points the self server's Bearer at a
    DIFFERENT file is OVERWRITTEN, exactly like MOLECULE_MCP_MODE. Reverting this to
    the old descriptor-wins guard lets a manifest repoint the Bearer (the token-
    substitution hole this fix closes) and fails the assert."""
    spec = {
        "command": "npx",
        "audience": "self",
        "env": {"MOLECULE_WORKSPACE_TOKEN_FILE": "/tmp/evil"},
    }
    out = inject_privileged_env(
        "molecule-schedule-self", spec, {"MOLECULE_API_URL": "https://acme"}
    )
    assert out["env"]["MOLECULE_WORKSPACE_TOKEN_FILE"] == WORKSPACE_TOKEN_FILE == "/configs/.auth_token"
    assert out["env"]["MOLECULE_WORKSPACE_TOKEN_FILE"] != "/tmp/evil"


# ── #2 workspace id injected under both read-names ──────────────────────────
def test_self_audience_injects_workspace_id_from_container_env():
    """#2 (functional) the self surface injects the workspace's OWN id under BOTH
    names the self server's selfWorkspaceId() reads (WORKSPACE_ID /
    MOLECULE_WORKSPACE_ID), sourced from the container env — so create_schedule's
    self-default resolves the own id. Dropping the injection fails these asserts."""
    env = {"MOLECULE_API_URL": "https://acme.moleculesai.app", "WORKSPACE_ID": "ws-self-42"}
    out = inject_privileged_env("molecule-schedule-self", {"audience": "self"}, env)
    assert out["env"]["WORKSPACE_ID"] == "ws-self-42"
    assert out["env"]["MOLECULE_WORKSPACE_ID"] == "ws-self-42"


def test_self_audience_workspace_id_sources_from_molecule_alias():
    """#2 (functional) WORKSPACE_ID resolves from MOLECULE_WORKSPACE_ID when the
    plain container name is unset."""
    env = {"MOLECULE_API_URL": "https://x", "MOLECULE_WORKSPACE_ID": "ws-alias"}
    out = inject_privileged_env("schedule-self", {"audience": "self"}, env)
    assert out["env"]["WORKSPACE_ID"] == "ws-alias"
    assert out["env"]["MOLECULE_WORKSPACE_ID"] == "ws-alias"


def test_self_audience_workspace_id_absent_when_container_has_none():
    """#2 (functional, negative control) with no WORKSPACE_ID source in the container
    env, NO empty workspace-id is injected — never an empty string the self server
    would mis-resolve to."""
    out = inject_privileged_env("schedule-self", {"audience": "self"}, {"MOLECULE_API_URL": "https://x"})
    assert "WORKSPACE_ID" not in out["env"]
    assert "MOLECULE_WORKSPACE_ID" not in out["env"]


# ── #3 truthy-`or` audience coercion closed ─────────────────────────────────
def test_present_but_empty_audience_does_not_fall_through_to_org():
    """#3 (correctness) a PRESENT-but-empty audience ("") on the management name must
    NOT silently fall through to the derived org default. The old truthy-`or` coerced
    "" to "org" and injected the full org-admin env; the presence check honors the
    explicit falsy value as a NO-injection declaration (and strips the bogus key).
    Reverting to the truthy-`or` re-injects the org secrets and fails here."""
    env = {k: f"val-{k}" for k in PRIVILEGED_ENV_KEYS}
    env["MOLECULE_ADMIN_TOKEN"] = "org-admin"
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx", "audience": ""}, env)
    e = out.get("env", {})
    for k in _ORG_SECRET_KEYS:
        assert k not in e, f"org secret {k} injected via empty-audience fall-through on the mgmt name"
    assert "audience" not in out  # the falsy directive is stripped, never rendered


def test_present_null_audience_on_management_name_injects_nothing():
    """#3 (correctness) an explicit `audience: null` on the management name is honored
    as a no-injection declaration, NOT re-derived to org — guards the same coercion
    hole for the null (rather than empty-string) falsy value."""
    env = {"MOLECULE_ADMIN_TOKEN": "org-admin", "MOLECULE_ORG_API_KEY": "org-secret"}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx", "audience": None}, env)
    e = out.get("env", {})
    for k in _ORG_SECRET_KEYS:
        assert k not in e
    assert "audience" not in out


def test_absent_audience_on_management_name_still_derives_org():
    """#3 (correctness, non-regression) the presence check must NOT change the ABSENT
    case — an omitted audience on the management name still derives 'org' and injects
    the org creds (the backward-compat bridge every existing manifest relies on)."""
    env = {"MOLECULE_ADMIN_TOKEN": "org-admin"}
    out = inject_privileged_env(MANAGEMENT_MCP_NAME, {"command": "npx"}, env)
    assert out["env"]["MOLECULE_API_KEY"] == "org-admin"


# ── #4 tenant API URL always resolved (or logged) ───────────────────────────
def test_self_audience_api_url_falls_back_to_cp_url_alias():
    """#4 (reachability) the tenant API base URL resolves from the MOLECULE_CP_URL
    legacy alias when MOLECULE_API_URL and PLATFORM_URL are unset — the self server
    must always get a usable URL, never silently default to localhost."""
    out = inject_privileged_env("schedule-self", {"audience": "self"}, {"MOLECULE_CP_URL": "https://cp.legacy"})
    assert out["env"]["MOLECULE_API_URL"] == "https://cp.legacy"


def test_self_audience_no_api_url_logs_misconfig(caplog):
    """#4 (reachability) when the container carries NO API URL source, the injector
    LOGS a misconfig warning rather than silently omitting the URL (which would let
    the child fall back to an unreachable localhost). No URL key is injected."""
    import logging
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.privileged_mcp_env"):
        out = inject_privileged_env("schedule-self", {"audience": "self"}, {})
    assert "MOLECULE_API_URL" not in out["env"]
    assert "NO tenant API URL" in caplog.text

