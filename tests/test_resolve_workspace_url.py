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


# ── Push-mode loopback guard (registration-400 fix) ──────────────────────────
# Under push delivery the platform DIALS the advertised URL, so its write-time
# SSRF guard (workspace-server validateAgentURL) rejects a loopback host — it
# blocks the literal 127.0.0.1 but accepts host=="localhost" by name. When the
# fallback resolves to a loopback host under push, resolve_workspace_url must
# coerce to the accepted "localhost" token so a localbuild/dev box that never
# got MOLECULE_WORKSPACE_URL registers instead of 400 url_validate_failed.


def test_push_mode_coerces_loopback_machine_ip_to_localhost(monkeypatch):
    import molecule_runtime.main as m

    monkeypatch.setattr(m, "get_machine_ip", lambda: "127.0.0.1")
    # Default delivery_mode is "push".
    assert resolve_workspace_url({"HOSTNAME": ""}, 8000) == "http://localhost:8000"


def test_push_mode_coerces_loopback_hostname_to_localhost():
    # An explicit loopback HOSTNAME under push is also coerced.
    assert (
        resolve_workspace_url({"HOSTNAME": "127.0.0.1"}, 8000, delivery_mode="push")
        == "http://localhost:8000"
    )


def test_localhost_hostname_left_as_localhost_under_push():
    # host=="localhost" is already accepted by the guard — coercion is a no-op.
    assert (
        resolve_workspace_url({"HOSTNAME": "localhost"}, 8123, delivery_mode="push")
        == "http://localhost:8123"
    )


def test_poll_mode_leaves_loopback_untouched(monkeypatch):
    # In poll mode the platform never dials the URL, so no coercion/warning.
    import molecule_runtime.main as m

    monkeypatch.setattr(m, "get_machine_ip", lambda: "127.0.0.1")
    assert (
        resolve_workspace_url({"HOSTNAME": ""}, 8000, delivery_mode="poll")
        == "http://127.0.0.1:8000"
    )


def test_push_mode_non_loopback_host_unchanged():
    # A routable host under push is advertised verbatim (no coercion).
    assert (
        resolve_workspace_url({"HOSTNAME": "ip-10-10-1-147"}, 8000, delivery_mode="push")
        == "http://ip-10-10-1-147:8000"
    )


def test_get_machine_ip_probe_failure_returns_localhost(monkeypatch):
    # The probe-failure branch must return the guard-accepted loopback token
    # ("localhost"), NOT 127.0.0.1 — the latter would 400 the push register.
    import molecule_runtime.main as m
    import socket as _socket

    def _boom(*_a, **_k):
        raise OSError("no route to probe host")

    monkeypatch.setattr(_socket, "socket", _boom)
    assert m.get_machine_ip() == "localhost"
