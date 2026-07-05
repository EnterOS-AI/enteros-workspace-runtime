"""Schema-conformance gate for the workspace-comms wire contract.

This REPLACES the old AST drift-checker (``scripts/check_platform_comm_contract.py``
+ its ``tests/test_platform_comm_contract*.py``). Rather than statically
pattern-matching code structure, it captures the ACTUAL register + heartbeat +
A2A-envelope payloads the runtime puts on the wire and validates each against the
SSOT JSON-Schema (draft 2020-12) that lives in
``molecule-ai-sdk/contracts/workspace-comms`` (the contracts SSOT moved there
when ``molecule-contracts`` was archived; the schemas were folded in
byte-for-byte, so each ``$id`` still carries the historical
``molecule-contracts`` URL). That is strictly stronger than the AST guard: it
checks the real produced bytes (driven through the real builders / POST sites),
not a code shape a refactor could satisfy while still emitting a
non-conformant body.

The schemas are vendored under ``tests/fixtures/workspace_comms/`` (byte-for-byte
copies of the molecule-ai-sdk SSOT — see PROVENANCE.md) so the gate runs fully
offline in CI with no cross-repo clone, no token, and no network.

Each contract instance models BOTH directions (``request`` + ``response``); the
runtime is the REQUEST producer, so we validate the captured bodies against the
schema's ``#/properties/request`` sub-schema.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).parent / "fixtures" / "workspace_comms"

# The vendored schemas' ``$id`` deliberately retains the HISTORICAL
# molecule-contracts URL (the byte-for-byte fold-in into molecule-ai-sdk did not
# rewrite it — see PROVENANCE.md), so this prefix stays as-is even though the live
# SSOT now lives in molecule-ai-sdk/contracts/workspace-comms. It is an identifier
# tripwire, not a fetch URL — nothing here reaches the archived repo.
CANONICAL_ID_PREFIX = (
    "https://git.moleculesai.app/molecule-ai/molecule-contracts/workspace-comms/"
)

# Documented runtime-only diagnostic field(s) the runtime layers onto the
# register/heartbeat body via identity_gate_payload(). The producer (molecule-core
# RegisterPayload/HeartbeatPayload) does not model these — Go silently ignores
# unknown JSON fields — so the SSOT request schema deliberately omits them
# (additionalProperties:false). See the contracts repo's heartbeat divergence
# notes. We strip ONLY this explicit allowlist before validating; any OTHER
# unexpected field stays in the payload and is caught by additionalProperties.
_RUNTIME_ONLY_DIAGNOSTIC_FIELDS = frozenset({"platform_mcp_diag"})


def _request_validator(schema_name: str) -> Draft202012Validator:
    """Build a draft-2020-12 validator for a schema's ``#/properties/request``.

    The request sub-schema ``$ref``s ``#/$defs/agentCard`` at the document root,
    so we register the WHOLE schema as a resource (under its ``$id``) and point
    the validator at the request sub-schema by ref — the internal ``$defs``
    pointers then resolve against the full document.
    """
    schema = json.loads((SCHEMA_DIR / f"{schema_name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].startswith(CANONICAL_ID_PREFIX), (
        f"vendored {schema_name}.schema.json $id {schema['$id']!r} is not the "
        f"canonical contracts URL — the vendored copy has drifted from SSOT"
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    resource = Resource(contents=schema, specification=DRAFT202012)
    registry = Registry().with_resource(uri=schema["$id"], resource=resource)
    return Draft202012Validator(
        {"$ref": schema["$id"] + "#/properties/request"}, registry=registry
    )


def _strip_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _RUNTIME_ONLY_DIAGNOSTIC_FIELDS}


def _assert_valid(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        rendered = "\n".join(f"  - at {list(e.path)}: {e.message}" for e in errors)
        raise AssertionError(
            "payload does not conform to the workspace-comms contract:\n"
            f"{rendered}\npayload={json.dumps(payload, indent=2)}"
        )


# ---------------------------------------------------------------------------
# Capturing HTTP stubs — record the REAL body the POST sites build
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        # No auth_token / inbound secret → exercises the no-mint persistence path
        # without writing anything to disk.
        return {"status": "ok"}


class _CapturingAsyncClient:
    """httpx.AsyncClient-shaped stub: records the json= body of each POST."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": kwargs.get("json")})
        return _FakeResponse()


class _CapturingSyncClient:
    """httpx.Client-shaped stub: records the json= body of each POST."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": kwargs.get("json")})
        return _FakeResponse()


# ---------------------------------------------------------------------------
# Positive: the real builders / POST sites emit contract-conformant payloads
# ---------------------------------------------------------------------------


def test_register_payload_conforms() -> None:
    """Drive the real register_with_platform POST site and validate the captured
    body (the identity_gate_payload-layered RegisterPayload)."""
    from molecule_runtime.main import register_with_platform

    client = _CapturingAsyncClient()
    ok = asyncio.run(
        register_with_platform(
            client,
            platform_url="http://platform.test",
            workspace_id="0b8f2c4a-1d3e-4f56-9a7b-2c8d4e6f1a90",
            workspace_url="https://demo.moleculesai.app/a2a",
            agent_card={"name": "Org Concierge", "description": "concierge"},
            headers={},
        )
    )
    assert ok is True
    assert client.calls and "/registry/register" in client.calls[-1]["url"]
    body = _strip_diagnostics(client.calls[-1]["json"])
    _assert_valid(_request_validator("register"), body)


def test_heartbeat_payload_conforms() -> None:
    """Drive the real HeartbeatLoop._send_heartbeat body builder and validate the
    captured HeartbeatPayload."""
    from molecule_runtime.heartbeat import HeartbeatLoop

    loop = HeartbeatLoop(
        platform_url="http://platform.test",
        workspace_id="0b8f2c4a-1d3e-4f56-9a7b-2c8d4e6f1a90",
    )
    loop.current_task = "provisioning a google-adk workspace"
    loop.active_tasks = 1
    client = _CapturingSyncClient()
    loop._send_heartbeat(client)
    assert client.calls and "/registry/heartbeat" in client.calls[-1]["url"]
    body = _strip_diagnostics(client.calls[-1]["json"])
    _assert_valid(_request_validator("heartbeat"), body)


def test_a2a_envelope_conforms() -> None:
    """The runtime's canonical outbound message/send builder
    (build_message_send_params) produces params that, wrapped in the JSON-RPC
    envelope the platform constructs, satisfy the A2A-envelope contract."""
    from molecule_runtime.a2a_client import build_message_send_params

    params = build_message_send_params(
        "Platform readiness check — no action needed.",
        metadata={"concierge_warmup": True},
    )
    envelope = {
        "jsonrpc": "2.0",
        "id": "concierge-warmup-0b8f2c4a",
        "method": "message/send",
        "params": params,
    }
    _assert_valid(_request_validator("a2a-envelope"), envelope)


# ---------------------------------------------------------------------------
# Negative: the gate MUST reject a divergent payload (anti-vacuous proof)
# ---------------------------------------------------------------------------


def test_gate_catches_type_keyed_part() -> None:
    """A2A v0.3 parts are discriminated by ``kind``; a ``type``-keyed part is
    silently dropped by the receiver's v0.3 validator (#2345). The gate must
    catch a regression that ships ``type`` instead of ``kind``."""
    validator = _request_validator("a2a-envelope")
    bad_envelope = {
        "jsonrpc": "2.0",
        "id": "abc",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "abc",
                "parts": [{"type": "text", "text": "dropped by v0.3 validator"}],
            }
        },
    }
    assert list(validator.iter_errors(bad_envelope)), (
        "gate FAILED to reject a type-keyed (non-kind) A2A part — it would not "
        "catch the #2345 regression class"
    )


def test_gate_catches_missing_workspace_id() -> None:
    validator = _request_validator("heartbeat")
    bad = {"error_rate": 0.0, "active_tasks": 1}  # missing required workspace_id
    assert list(validator.iter_errors(bad))


def test_gate_catches_bad_kind_enum() -> None:
    validator = _request_validator("register")
    bad = {"id": "x", "agent_card": {"name": "n"}, "kind": "superuser"}
    assert list(validator.iter_errors(bad))


def test_gate_catches_unexpected_extra_field() -> None:
    """A NON-allowlisted extra field must still trip additionalProperties:false —
    proving the diagnostic-strip allowlist does not blanket-permit junk."""
    validator = _request_validator("register")
    bad = {"id": "x", "agent_card": {"name": "n"}, "totally_new_field": True}
    assert list(validator.iter_errors(bad))


@pytest.mark.parametrize("schema_name", ["register", "heartbeat", "a2a-envelope"])
def test_vendored_schema_is_canonical(schema_name: str) -> None:
    """Tripwire: the vendored schema is the SSOT mirror, not a fork."""
    _request_validator(schema_name)  # raises if $id/$schema drifted
