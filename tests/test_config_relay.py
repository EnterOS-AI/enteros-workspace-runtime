"""Config-relay fetch-and-ack boot prelude (cf-r2-relay-config-secret-delivery).

Pins the runtime side of the R2 transient config+secret relay: the box fetches
its {config.yaml + prompts/* + secrets} bundle over a short-TTL presigned HTTPS
GET, verifies the sha256, unpacks it into /configs, then POSTs the ready-ack so
the CP deletes the transient object.

Contract mirrored from the CP side (internal/configrelay/relay.go +
internal/handlers/workspace_relay_ack.go):
  - env: MOLECULE_CONFIG_RELAY_URI / _SHA256 / _ACK_TOKEN (+ MOLECULE_CP_URL,
    WORKSPACE_ID already on the box);
  - wire format: {path: base64(content)} JSON, sha256 taken over the raw body;
  - ack: POST <cp>/cp/workspaces/<id>/relay-ack, Authorization: Bearer <token>,
    204 on success.

These tests assert the four load-bearing behaviours the flag flip depends on:
  * a REAL bundle is fetched, verified, and unpacked byte-exact (files 0600);
  * a sha256 mismatch is REJECTED (fail-closed) — a tampered/truncated bundle
    never reaches /configs;
  * the ready-ack is POSTed with the bearer to the derived CP endpoint;
plus the inert (feature-off) no-op, fail-closed env/traversal guards, and the
transient retry/backoff envelope (cold presign + 5xx ack).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime import config_relay  # noqa: E402
from molecule_runtime.config_relay import (  # noqa: E402
    RelayConfigError,
    fetch_bundle,
    post_ack,
    relay_env,
    run_config_relay_prelude,
    unpack_bundle,
    validate_bundle_path,
)

_NO_SLEEP = lambda _seconds: None  # noqa: E731 — inert backoff for deterministic tests


# --------------------------------------------------------------------------- #
# Wire-format helpers (byte-identical to the CP marshal).
# --------------------------------------------------------------------------- #
def _make_bundle(files: dict[str, bytes]) -> tuple[bytes, str]:
    """Marshal {path: bytes} into the {path: base64} JSON body + its sha256 hex,
    exactly as the CP's ValidateAndMarshalConfigBundle + Stage do."""
    payload = json.dumps(
        {p: base64.b64encode(c).decode("ascii") for p, c in files.items()}
    ).encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# relay_env — the enable gate + fail-closed env contract.
# --------------------------------------------------------------------------- #
def test_relay_env_absent_uri_is_inert():
    assert relay_env({}) is None
    assert relay_env({"MOLECULE_CONFIG_RELAY_URI": "   "}) is None


def test_relay_env_full_contract_parses():
    env = {
        "MOLECULE_CONFIG_RELAY_URI": "https://r2.example/relay/ws-1/n.json?sig=x",
        "MOLECULE_CONFIG_RELAY_SHA256": "ABCDEF",
        "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "ack-tok",
    }
    uri, sha, tok = relay_env(env)
    assert uri.endswith("n.json?sig=x")
    assert sha == "abcdef"  # normalised lower-case
    assert tok == "ack-tok"


@pytest.mark.parametrize("missing", ["MOLECULE_CONFIG_RELAY_SHA256", "MOLECULE_CONFIG_RELAY_ACK_TOKEN"])
def test_relay_env_partial_contract_fails_closed(missing):
    env = {
        "MOLECULE_CONFIG_RELAY_URI": "https://r2.example/x",
        "MOLECULE_CONFIG_RELAY_SHA256": "abc",
        "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "tok",
    }
    del env[missing]
    with pytest.raises(RelayConfigError):
        relay_env(env)


# --------------------------------------------------------------------------- #
# fetch_bundle — integrity + transient retry.
# --------------------------------------------------------------------------- #
def test_fetch_bundle_real_body_verified():
    payload, sha = _make_bundle({"config.yaml": b"model: x\n"})
    got = fetch_bundle(
        "https://r2/x", sha, client=_client(lambda req: httpx.Response(200, content=payload)), sleep=_NO_SLEEP
    )
    assert got == payload


def test_fetch_bundle_sha_mismatch_fails_closed_after_ceiling():
    payload, _ = _make_bundle({"config.yaml": b"real\n"})
    wrong_sha = hashlib.sha256(b"different").hexdigest()
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=payload)

    with pytest.raises(RelayConfigError, match="fetch failed after"):
        fetch_bundle("https://r2/x", wrong_sha, client=_client(handler), sleep=_NO_SLEEP, max_attempts=3)
    assert calls["n"] == 3  # re-fetched every attempt, then failed closed


def test_fetch_bundle_cold_presign_403_then_success():
    """A 403/404 in the window right after the PUT is transient — retried, not fatal."""
    payload, sha = _make_bundle({"config.yaml": b"ok\n"})
    seq = iter([httpx.Response(403), httpx.Response(404), httpx.Response(200, content=payload)])
    got = fetch_bundle("https://r2/x", sha, client=_client(lambda req: next(seq)), sleep=_NO_SLEEP)
    assert got == payload


def test_fetch_bundle_transport_error_retried_then_fails_closed():
    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(RelayConfigError):
        fetch_bundle("https://r2/x", "deadbeef", client=_client(handler), sleep=_NO_SLEEP, max_attempts=2)


# --------------------------------------------------------------------------- #
# validate_bundle_path + unpack_bundle — traversal guard + real unpack + perms.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "a/../../b", "foo\\bar", "sp ace", ""])
def test_validate_bundle_path_rejects_traversal_and_bad_charset(bad):
    with pytest.raises(RelayConfigError):
        validate_bundle_path(bad)


@pytest.mark.parametrize("good", ["config.yaml", "prompts/system.md", "prompts/sub/x.md", ".secrets"])
def test_validate_bundle_path_accepts_clean(good):
    assert validate_bundle_path(good) == good


def test_unpack_bundle_writes_real_files_with_secure_perms(tmp_path):
    files = {
        "config.yaml": b"name: agent\nmodel: claude\n",
        "prompts/system.md": b"You are helpful.\n",
        ".secrets": b"API_KEY=shh\n",
    }
    payload, _ = _make_bundle(files)
    written = unpack_bundle(payload, tmp_path)

    assert len(written) == 3
    for rel, content in files.items():
        dest = tmp_path / rel
        assert dest.read_bytes() == content
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        if os.name == "posix":
            assert mode == 0o600, f"{rel} perms {oct(mode)} (secrets must be 0600)"


def test_unpack_bundle_rejects_traversal_before_write(tmp_path):
    payload = json.dumps({"../evil": base64.b64encode(b"x").decode()}).encode()
    with pytest.raises(RelayConfigError):
        unpack_bundle(payload, tmp_path)
    assert not (tmp_path.parent / "evil").exists()


def test_unpack_bundle_rejects_bad_base64(tmp_path):
    payload = json.dumps({"config.yaml": "not!valid!base64!"}).encode()
    with pytest.raises(RelayConfigError):
        unpack_bundle(payload, tmp_path)


def test_unpack_bundle_rejects_non_object_json(tmp_path):
    with pytest.raises(RelayConfigError):
        unpack_bundle(b"[1,2,3]", tmp_path)


# --------------------------------------------------------------------------- #
# post_ack — bearer to derived endpoint, transient retry, best-effort.
# --------------------------------------------------------------------------- #
def test_post_ack_posts_bearer_to_derived_endpoint():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("Authorization")
        seen["method"] = req.method
        return httpx.Response(204)

    acked = post_ack("https://cp.example/", "ws-42", "ack-secret", client=_client(handler), sleep=_NO_SLEEP)
    assert acked is True
    assert seen["method"] == "POST"
    assert seen["url"] == "https://cp.example/cp/workspaces/ws-42/relay-ack"
    assert seen["auth"] == "Bearer ack-secret"


def test_post_ack_retries_5xx_then_204():
    seq = iter([httpx.Response(503), httpx.Response(502), httpx.Response(204)])
    assert post_ack("https://cp", "ws", "t", client=_client(lambda r: next(seq)), sleep=_NO_SLEEP) is True


def test_post_ack_permanent_401_is_non_fatal_false():
    assert post_ack("https://cp", "ws", "t", client=_client(lambda r: httpx.Response(401)), sleep=_NO_SLEEP) is False


def test_post_ack_skipped_without_cp_url():
    # No network call should be attempted; returns False (backstop reaper covers it).
    def handler(req):  # pragma: no cover - must not be called
        raise AssertionError("post_ack must not call out without a CP URL")

    assert post_ack("", "ws", "t", client=_client(handler), sleep=_NO_SLEEP) is False


# --------------------------------------------------------------------------- #
# run_config_relay_prelude — end-to-end orchestration.
# --------------------------------------------------------------------------- #
def test_prelude_inert_when_uri_absent(tmp_path):
    assert run_config_relay_prelude(workspace_id="ws", config_path=tmp_path, env={}) is None
    assert list(tmp_path.iterdir()) == []


def test_prelude_end_to_end_fetch_unpack_ack(tmp_path):
    files = {"config.yaml": b"model: claude\n", "prompts/system.md": b"hi\n"}
    payload, sha = _make_bundle(files)
    ack_seen = {}

    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, content=payload)
        ack_seen["url"] = str(req.url)
        ack_seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(204)

    env = {
        "MOLECULE_CONFIG_RELAY_URI": "https://r2.example/relay/ws-9/n.json?sig=abc",
        "MOLECULE_CONFIG_RELAY_SHA256": sha,
        "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "the-ack-token",
        "MOLECULE_CP_URL": "https://api.example",
        "WORKSPACE_ID": "ws-9",
    }
    result = run_config_relay_prelude(
        workspace_id="ws-9", config_path=tmp_path, env=env, client=_client(handler), sleep=_NO_SLEEP
    )
    assert result is not None
    assert result.acked is True
    assert (tmp_path / "config.yaml").read_bytes() == files["config.yaml"]
    assert (tmp_path / "prompts/system.md").read_bytes() == files["prompts/system.md"]
    # ack went to the CP endpoint derived from MOLECULE_CP_URL + WORKSPACE_ID.
    assert ack_seen["url"] == "https://api.example/cp/workspaces/ws-9/relay-ack"
    assert ack_seen["auth"] == "Bearer the-ack-token"


def test_prelude_fail_closed_on_sha_mismatch_aborts_boot(tmp_path):
    payload, _ = _make_bundle({"config.yaml": b"x\n"})
    env = {
        "MOLECULE_CONFIG_RELAY_URI": "https://r2/x",
        "MOLECULE_CONFIG_RELAY_SHA256": hashlib.sha256(b"wrong").hexdigest(),
        "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "t",
        "MOLECULE_CP_URL": "https://cp",
        "WORKSPACE_ID": "ws",
    }
    with pytest.raises(SystemExit):
        run_config_relay_prelude(
            workspace_id="ws",
            config_path=tmp_path,
            env=env,
            client=_client(lambda r: httpx.Response(200, content=payload)),
            sleep=_NO_SLEEP,
        )


def test_prelude_partial_env_aborts_boot(tmp_path):
    env = {"MOLECULE_CONFIG_RELAY_URI": "https://r2/x"}  # no sha / token
    with pytest.raises(SystemExit):
        run_config_relay_prelude(workspace_id="ws", config_path=tmp_path, env=env)


def test_prelude_unacked_still_boots(tmp_path):
    """A delivered config with a failing ack must NOT abort boot (backstop deletes)."""
    files = {"config.yaml": b"y\n"}
    payload, sha = _make_bundle(files)

    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, content=payload)
        return httpx.Response(500)  # ack keeps failing

    env = {
        "MOLECULE_CONFIG_RELAY_URI": "https://r2/x",
        "MOLECULE_CONFIG_RELAY_SHA256": sha,
        "MOLECULE_CONFIG_RELAY_ACK_TOKEN": "t",
        "MOLECULE_CP_URL": "https://cp",
        "WORKSPACE_ID": "ws",
    }
    result = run_config_relay_prelude(
        workspace_id="ws", config_path=tmp_path, env=env, client=_client(handler), sleep=_NO_SLEEP,
    )
    assert result is not None
    assert result.acked is False
    assert (tmp_path / "config.yaml").read_bytes() == files["config.yaml"]


def test_module_exposes_env_constants():
    # Guard the box-facing contract names against silent drift from the CP consts.
    assert config_relay.RELAY_URI_ENV == "MOLECULE_CONFIG_RELAY_URI"
    assert config_relay.RELAY_SHA256_ENV == "MOLECULE_CONFIG_RELAY_SHA256"
    assert config_relay.RELAY_ACK_TOKEN_ENV == "MOLECULE_CONFIG_RELAY_ACK_TOKEN"
