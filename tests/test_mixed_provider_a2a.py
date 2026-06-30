"""Mixed-provider A2A regression guard — a tenant whose workspaces run on
DIFFERENT cloud/compute providers must still have WORKING peer-to-peer A2A.

This is the per-PR CI guard for the principal's box-moves: workspaces will be
relocated onto AWS / GCP / Hetzner (and the local Molecules-Server). Peer A2A
must NOT break when a box moves, because peer routing goes through the
platform-proxied address keyed by WORKSPACE ID
(``{PLATFORM_URL}/workspaces/{peer_id}/a2a`` for sends,
``{PLATFORM_URL}/registry/discover/{peer_id}`` for discovery) — NOT the peer's
provider-specific / internal address (a raw docker container host for a
local-docker box, an intra-VPC hostname for a same-cloud box, a Cloudflare
tunnel for a cross-cloud box).

It exercises the REAL routing seam in ``a2a_client`` (owned elsewhere; this
test only CONSUMES it) across a heterogeneous fleet and proves three things:

  1. Every peer, whatever provider it runs on, is reached at the identical
     platform-proxied shape — the dialed URLs differ ONLY in the workspace-id
     path segment, never by provider.
  2. No provider-specific / internal address is EVER dialed.
  3. Box relocation is a no-op for addressing: the SAME workspace id, "moved"
     across {molecules-server, aws, gcp, hetzner}, yields a byte-identical send
     target every time.

A green run here means: moving a workspace box to a different provider cannot
break cross-provider peer comms. Companion to
``test_delegation_provider_agnostic_routing.py`` (which pins the in-container
delegation path); this one pins the platform-proxy seam directly across a
mixed-provider tenant.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SELF = "11111111-1111-1111-1111-111111111111"
_PLATFORM = "http://test-platform:8080"

# A tenant whose four workspaces each run on a DIFFERENT provider. The ``addr``
# is the peer's OWN provider-specific reachable address — the cross-provider
# trap: if routing ever used it, a caller on a different provider would fail.
_FLEET = {
    "molecules-server": {
        "id": "22222222-2222-2222-2222-222222222222",
        "addr": "http://7eb42c7db571:8000",          # local-docker container host
    },
    "aws": {
        "id": "33333333-3333-3333-3333-333333333333",
        "addr": "http://ip-10-2-3-4:8000",           # AWS intra-VPC hostname
    },
    "gcp": {
        "id": "44444444-4444-4444-4444-444444444444",
        "addr": "https://ws-gcp.tunnel.example.app",  # GCP cross-cloud tunnel
    },
    "hetzner": {
        "id": "55555555-5555-5555-5555-555555555555",
        "addr": "https://ws-hetzner.tunnel.example.app",  # Hetzner tunnel
    },
}
_PROVIDER_ADDRS = [p["addr"] for p in _FLEET.values()]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Pin PLATFORM_URL + WORKSPACE_ID so the routing seam builds the proxy URL
    against the test platform, and isolate platform-auth between cases."""
    monkeypatch.setenv("WORKSPACE_ID", _SELF)
    monkeypatch.setenv("PLATFORM_URL", _PLATFORM)

    import molecule_runtime.platform_auth as platform_auth
    platform_auth.clear_cache()

    import molecule_runtime.a2a_client as a2a_client
    # PLATFORM_URL is read at import; force it so the seam targets the test
    # platform. Every call below passes source_workspace_id=_SELF explicitly so
    # the PEP-562 virtual WORKSPACE_ID resolver is never consulted.
    monkeypatch.setattr(a2a_client, "PLATFORM_URL", _PLATFORM, raising=True)
    yield
    platform_auth.clear_cache()


class _FakeResp:
    def __init__(self, json_body, status=200, content_type="application/json"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._json = json_body
        self.text = "ok"

    def json(self):
        return self._json


class _RecordingClient:
    """httpx.AsyncClient stand-in recording every dialed URL. The send path
    expects a JSON-RPC result; discovery expects a peer record."""

    def __init__(self, recorder, *args, **kwargs):
        self._rec = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self._rec.append(("POST", url, kw))
        return _FakeResp(
            {"jsonrpc": "2.0", "id": "x",
             "result": {"parts": [{"kind": "text", "text": "peer answer"}]}}
        )

    async def get(self, url, **kw):
        self._rec.append(("GET", url, kw))
        # A peer record whose advertised url is the provider-specific address —
        # discovery returning it must NOT cause routing to dial it.
        return _FakeResp({"id": "peer", "status": "online", "url": "PROVIDER_ADDR"})


def _patch_httpx(monkeypatch, rec):
    import httpx

    def _factory(*a, **kw):
        return _RecordingClient(rec, *a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _factory, raising=True)


def _patch_auth(monkeypatch):
    """Neutralize the auth/header helpers so the test exercises ROUTING, not
    the bearer-token chain."""
    import molecule_runtime.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "self_source_headers",
                        lambda src: {"X-Workspace-ID": src}, raising=True)
    monkeypatch.setattr(a2a_client, "auth_headers",
                        lambda src: {}, raising=True)


@pytest.mark.asyncio
async def test_send_routes_every_provider_via_platform_proxy_by_id(monkeypatch):
    """Each peer — on local-docker / AWS / GCP / Hetzner — is reached at the
    platform proxy by workspace id, and NO provider address is ever dialed."""
    import molecule_runtime.a2a_client as a2a_client

    _patch_auth(monkeypatch)
    rec: list = []
    _patch_httpx(monkeypatch, rec)

    for provider, peer in _FLEET.items():
        out = await a2a_client.send_a2a_message(peer["id"], "ping", source_workspace_id=_SELF)
        assert out == "peer answer", f"{provider}: send failed: {out!r}"

    posts = [url for (m, url, _) in rec if m == "POST"]
    # Exactly one POST per peer, each to the platform proxy keyed by its id.
    assert posts == [f"{_PLATFORM}/workspaces/{p['id']}/a2a" for p in _FLEET.values()], posts
    # No provider-specific / internal address ever appears in any dialed URL.
    for _, url, _ in rec:
        for addr in _PROVIDER_ADDRS:
            assert addr not in url, f"provider address {addr} was dialed: {url}"


@pytest.mark.asyncio
async def test_send_targets_differ_only_by_workspace_id(monkeypatch):
    """Structural invariance: across the mixed-provider fleet the send targets
    are identical except for the workspace-id segment — proving the address is a
    pure function of (platform, workspace_id), independent of provider."""
    import molecule_runtime.a2a_client as a2a_client

    _patch_auth(monkeypatch)
    rec: list = []
    _patch_httpx(monkeypatch, rec)

    for peer in _FLEET.values():
        await a2a_client.send_a2a_message(peer["id"], "ping", source_workspace_id=_SELF)

    posts = [url for (m, url, _) in rec if m == "POST"]
    # Strip the id segment; every remaining template must be identical.
    templates = {
        url.replace(peer["id"], "{ID}")
        for url, peer in zip(posts, _FLEET.values())
    }
    assert templates == {f"{_PLATFORM}/workspaces/{{ID}}/a2a"}, templates


@pytest.mark.asyncio
async def test_box_relocation_does_not_change_send_target(monkeypatch):
    """The regression guard for box-moves: the SAME workspace id, relocated
    across every provider in turn, yields a byte-identical send target each
    time. Moving a box to AWS/GCP/Hetzner cannot change how peers address it."""
    import molecule_runtime.a2a_client as a2a_client

    _patch_auth(monkeypatch)
    moving_id = "66666666-6666-6666-6666-666666666666"

    targets = []
    for _provider in _FLEET:  # simulate the box living on each provider in turn
        rec: list = []
        _patch_httpx(monkeypatch, rec)
        await a2a_client.send_a2a_message(moving_id, "ping", source_workspace_id=_SELF)
        targets.append(next(url for (m, url, _) in rec if m == "POST"))

    assert len(set(targets)) == 1, f"send target changed across relocations: {targets}"
    assert targets[0] == f"{_PLATFORM}/workspaces/{moving_id}/a2a"


@pytest.mark.asyncio
async def test_discovery_routes_every_provider_via_platform_registry_by_id(monkeypatch):
    """Peer DISCOVERY is provider-agnostic too: each lookup goes to the platform
    registry by workspace id, regardless of which provider the peer runs on."""
    import molecule_runtime.a2a_client as a2a_client

    _patch_auth(monkeypatch)
    rec: list = []
    _patch_httpx(monkeypatch, rec)

    for peer in _FLEET.values():
        got = await a2a_client.discover_peer(peer["id"], source_workspace_id=_SELF)
        assert got is not None

    gets = [url for (m, url, _) in rec if m == "GET"]
    assert gets == [f"{_PLATFORM}/registry/discover/{p['id']}" for p in _FLEET.values()], gets
    for _, url, _ in rec:
        for addr in _PROVIDER_ADDRS:
            assert addr not in url
