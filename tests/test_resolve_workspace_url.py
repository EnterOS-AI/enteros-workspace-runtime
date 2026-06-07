"""Tests for resolve_workspace_url — the externally-advertised A2A URL (runtime#95).

The agent registers this URL with the tenant as its push target. The default
advertises the host's cloud-internal name (AWS: ip-<priv-ip>), which only
resolves inside the workspace's own VPC — so a tenant in a DIFFERENT cloud (an
EC2 workspace under a GCP tenant) gets a DNS SERVFAIL and rejects register with
400. MOLECULE_WORKSPACE_URL lets the platform inject a reachable URL (e.g. a
per-workspace Cloudflare tunnel) used verbatim.
"""

from molecule_runtime.main import resolve_workspace_url


def test_injected_url_used_verbatim_no_port_appended():
    env = {"MOLECULE_WORKSPACE_URL": "https://ws-16cb8f10-992.staging.moleculesai.app"}
    # Even with a port arg, the injected URL is returned as-is (the tunnel fronts :8000).
    assert (
        resolve_workspace_url(env, 8000)
        == "https://ws-16cb8f10-992.staging.moleculesai.app"
    )


def test_injected_url_whitespace_is_ignored_falls_back():
    env = {"MOLECULE_WORKSPACE_URL": "   ", "HOSTNAME": "ip-10-10-1-147"}
    assert resolve_workspace_url(env, 8000) == "http://ip-10-10-1-147:8000"


def test_fallback_uses_hostname_when_no_injected_url():
    env = {"HOSTNAME": "ip-10-10-1-147"}
    assert resolve_workspace_url(env, 8000) == "http://ip-10-10-1-147:8000"


def test_fallback_respects_port():
    env = {"HOSTNAME": "host-x"}
    assert resolve_workspace_url(env, 9001) == "http://host-x:9001"


def test_injected_url_takes_precedence_over_hostname():
    env = {
        "MOLECULE_WORKSPACE_URL": "https://ws-x.example.com",
        "HOSTNAME": "ip-10-10-1-147",
    }
    assert resolve_workspace_url(env, 8000) == "https://ws-x.example.com"


def test_empty_hostname_falls_through_to_machine_ip(monkeypatch):
    # No injected URL and blank HOSTNAME → get_machine_ip() is consulted.
    import molecule_runtime.main as m

    monkeypatch.setattr(m, "get_machine_ip", lambda: "203.0.113.5")
    assert resolve_workspace_url({"HOSTNAME": ""}, 8000) == "http://203.0.113.5:8000"
