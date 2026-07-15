"""Local A2A transport for plugin-declared channel daemons (runtime#215 PR-2).

The socket is a transport for the existing A2A JSON-RPC contract, not a new
channel protocol.  These tests pin the two new responsibilities only:

* a private Unix HTTP binding is delivered to each spawned plugin daemon; and
* that binding overwrites client-claimed channel identity with the identity of
  the plugin the runtime discovered and spawned.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from molecule_runtime.channel_events import (
    CHANNEL_A2A_SOCKET_ENV,
    CHANNEL_A2A_TOKEN_ENV,
    CHANNEL_API_VERSION,
    CHANNEL_API_VERSION_ENV,
    CHANNEL_PLUGIN_ID_ENV,
    ChannelEventDeliveryUnknown,
    ChannelEventProtocolError,
    ChannelEventUnavailable,
    ChannelEventSocketManager,
    RuntimeStampedChannelProvenance,
    build_channel_message_send_request,
    channel_message_response_text,
    send_channel_message,
)
from molecule_runtime.plugin_daemons import DaemonSpec, start_supervisor_when_bound

TEST_CAPABILITY = "test-channel-capability"


def _message(method: str = "message/send") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "evt-1",
        "method": method,
        "params": {
            "metadata": {
                "source": "spoofed-channel",
                "chat_id": "oc_123",
                "user_id": "ou_456",
                "username": "Ada",
                "message_id": "om_789",
            },
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "evt-1",
                "parts": [{"kind": "text", "text": "hello"}],
            },
        },
    }


def test_reusable_message_builder_emits_exact_platform_request_shape():
    request = build_channel_message_send_request(
        "hello",
        metadata={"chat_id": "C123", "user_id": "U456"},
        request_id="req-1",
        message_id="msg-1",
    )

    assert request == {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "msg-1",
                "parts": [{"kind": "text", "text": "hello"}],
            },
            "metadata": {"chat_id": "C123", "user_id": "U456"},
        },
    }


@pytest.mark.asyncio
async def test_reusable_sender_fails_closed_when_runtime_capability_absent(monkeypatch):
    monkeypatch.delenv(CHANNEL_API_VERSION_ENV, raising=False)
    with pytest.raises(ChannelEventUnavailable, match=CHANNEL_API_VERSION_ENV):
        await send_channel_message("hello")


@pytest.mark.asyncio
async def test_reusable_sender_never_marks_socket_failure_safe_to_replay(tmp_path):
    with pytest.raises(ChannelEventDeliveryUnknown, match="must not be replayed"):
        await send_channel_message(
            "hello",
            socket_path=tmp_path / "missing.sock",
            capability_token="test-capability",
            api_version=CHANNEL_API_VERSION,
            timeout_seconds=0.1,
        )


def test_response_text_helper_reads_real_a2a_v1_completed_task_shape():
    response = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "result": {
            "kind": "task",
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {
                "state": "completed",
                "message": {
                    "kind": "message",
                    "messageId": "reply-1",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "hello from the agent"}],
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                },
            },
            "artifacts": [
                {
                    "artifactId": "artifact-1",
                    "parts": [{"kind": "text", "text": "streamed copy"}],
                }
            ],
        },
    }

    assert channel_message_response_text(response) == "hello from the agent"


def test_response_text_helper_supports_legacy_message_result_and_errors():
    legacy = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "result": {"parts": [{"kind": "text", "text": "legacy reply"}]},
    }
    assert channel_message_response_text(legacy) == "legacy reply"

    error = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {"code": -32603, "message": "agent not configured"},
    }
    with pytest.raises(ChannelEventProtocolError, match="agent not configured"):
        channel_message_response_text(error)


def test_runtime_stamps_source_only_on_canonical_params_metadata():
    captured: dict = {}

    async def endpoint(request: Request) -> JSONResponse:
        captured.update(await request.json())
        return JSONResponse({"jsonrpc": "2.0", "id": "evt-1", "result": {"ok": True}})

    app = RuntimeStampedChannelProvenance(
        Starlette(routes=[Route("/", endpoint, methods=["POST"])]),
        plugin_id="lark-channel-molecule",
        capability_token=TEST_CAPABILITY,
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/",
            json=_message(),
            headers={"x-molecule-channel-capability": TEST_CAPABILITY},
        )

    assert response.status_code == 200
    params = captured["params"]
    assert params["metadata"]["source"] == "lark-channel-molecule"
    assert "metadata" not in params["message"]
    # The remaining channel fields are already part of the platform A2A
    # contract.  The transport must not rename or move them.
    assert params["metadata"]["chat_id"] == "oc_123"
    assert params["metadata"]["user_id"] == "ou_456"
    assert params["metadata"]["username"] == "Ada"
    assert params["metadata"]["message_id"] == "om_789"

    schema = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "workspace_comms"
            / "a2a-envelope.schema.json"
        ).read_text()
    )
    # The body after runtime stamping is still the vendored platform A2A
    # request contract, rather than a socket-only envelope.
    Draft202012Validator(schema["properties"]["request"]).validate(captured)


def test_runtime_adds_canonical_metadata_when_client_omits_it():
    captured: dict = {}

    async def endpoint(request: Request) -> JSONResponse:
        captured.update(await request.json())
        return JSONResponse({"ok": True})

    body = _message()
    del body["params"]["metadata"]
    app = RuntimeStampedChannelProvenance(
        Starlette(routes=[Route("/", endpoint, methods=["POST"])]),
        plugin_id="slack-channel-molecule",
        capability_token=TEST_CAPABILITY,
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        assert client.post(
            "/",
            json=body,
            headers={"x-molecule-channel-capability": TEST_CAPABILITY},
        ).status_code == 200

    params = captured["params"]
    assert params["metadata"]["source"] == "slack-channel-molecule"
    assert "metadata" not in params["message"]


def test_client_claimed_source_on_message_metadata_is_rejected_before_dispatch():
    dispatched = False

    async def endpoint(_request: Request) -> JSONResponse:
        nonlocal dispatched
        dispatched = True
        return JSONResponse({"ok": True})

    body = _message()
    body["params"]["message"]["metadata"] = {"source": "spoofed-channel"}
    app = RuntimeStampedChannelProvenance(
        Starlette(routes=[Route("/", endpoint, methods=["POST"])]),
        plugin_id="slack-channel-molecule",
        capability_token=TEST_CAPABILITY,
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/",
            json=body,
            headers={"x-molecule-channel-capability": TEST_CAPABILITY},
        )

    assert response.status_code == 400
    assert "params.message.metadata.source" in response.json()["error"]["message"]
    assert dispatched is False


def test_non_a2a_routes_are_byte_for_byte_passthrough():
    async def card(_request: Request) -> JSONResponse:
        return JSONResponse({"name": "workspace", "source": "application-owned"})

    app = RuntimeStampedChannelProvenance(
        Starlette(routes=[Route("/.well-known/agent-card.json", card)]),
        plugin_id="lark-channel-molecule",
        capability_token=TEST_CAPABILITY,
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/.well-known/agent-card.json")
    assert response.json() == {"name": "workspace", "source": "application-owned"}


def test_malformed_metadata_fails_closed_before_agent_dispatch():
    dispatched = False

    async def endpoint(_request: Request) -> JSONResponse:
        nonlocal dispatched
        dispatched = True
        return JSONResponse({"ok": True})

    body = _message()
    body["params"]["metadata"] = "not-an-object"
    app = RuntimeStampedChannelProvenance(
        Starlette(routes=[Route("/", endpoint, methods=["POST"])]),
        plugin_id="lark-channel-molecule",
        capability_token=TEST_CAPABILITY,
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/",
            json=body,
            headers={"x-molecule-channel-capability": TEST_CAPABILITY},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert dispatched is False


@pytest.mark.asyncio
async def test_manager_binds_private_uds_injects_env_and_preserves_jsonrpc():
    captured: dict = {}
    captured_headers: dict = {}

    async def endpoint(request: Request) -> JSONResponse:
        captured.update(await request.json())
        captured_headers.update(request.headers)
        return JSONResponse(
            {"jsonrpc": "2.0", "id": captured["id"], "result": {"parts": []}}
        )

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    spec = DaemonSpec(
        name="bridge",
        plugin="lark-channel-molecule",
        kind="channel",
        command=["does-not-spawn-in-this-test"],
        env={
            CHANNEL_A2A_SOCKET_ENV: "/tmp/client-claimed.sock",
            CHANNEL_PLUGIN_ID_ENV: "client-claimed-plugin",
            CHANNEL_API_VERSION_ENV: "manifest-claimed-version",
        },
    )
    manager = ChannelEventSocketManager(
        app,
        [spec],
        startup_timeout_seconds=3,
    )

    try:
        assert await manager.start() is True
        socket_path = spec.env[CHANNEL_A2A_SOCKET_ENV]
        assert socket_path != "/tmp/client-claimed.sock"
        assert spec.env[CHANNEL_PLUGIN_ID_ENV] == "lark-channel-molecule"
        assert spec.env[CHANNEL_API_VERSION_ENV] == CHANNEL_API_VERSION

        root_mode = stat.S_IMODE(os.stat(os.path.dirname(socket_path)).st_mode)
        socket_mode = stat.S_IMODE(os.stat(socket_path).st_mode)
        assert root_mode == 0o700
        assert stat.S_ISSOCK(os.stat(socket_path).st_mode)
        assert socket_mode == 0o600

        body = _message()
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://molecule.local",
            timeout=3,
        ) as client:
            response = await client.post(
                "/",
                json=body,
                headers={
                    "x-molecule-channel-capability": spec.env[CHANNEL_A2A_TOKEN_ENV]
                },
            )

        # The existing JSON-RPC method and response cross the local transport
        # unchanged; only provenance is runtime-owned.
        assert captured["method"] == "message/send"
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": "evt-1",
            "result": {"parts": []},
        }
        assert captured["params"]["metadata"]["source"] == "lark-channel-molecule"
        assert "x-molecule-channel-capability" not in captured_headers

        helper_response = await send_channel_message(
            "from helper",
            metadata={"chat_id": "oc_helper", "user_id": "ou_helper"},
            request_id="helper-request",
            message_id="helper-message",
            environ=spec.env,
            timeout_seconds=3,
        )
        assert helper_response == {
            "jsonrpc": "2.0",
            "id": "helper-request",
            "result": {"parts": []},
        }
        assert captured["params"]["message"]["parts"] == [
            {"kind": "text", "text": "from helper"}
        ]
        assert captured["params"]["metadata"] == {
            "chat_id": "oc_helper",
            "user_id": "ou_helper",
            "source": "lark-channel-molecule",
        }
    finally:
        await manager.stop()

    assert not os.path.exists(socket_path)


@pytest.mark.asyncio
async def test_reusable_helper_round_trips_real_a2a_sdk_completed_task(monkeypatch):
    """No route fake: real build_routes + executor + a2a-sdk over the UDS."""
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface

    from molecule_runtime.a2a_executor import RuntimeA2AExecutor
    from molecule_runtime.boot_routes import build_routes

    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    monkeypatch.delenv("PLATFORM_URL", raising=False)

    class Chunk:
        content = "real pong"

    class Agent:
        async def astream_events(self, _payload, *, config=None, version=None):
            yield {
                "event": "on_chat_model_stream",
                "run_id": "real-a2a-run",
                "data": {"chunk": Chunk()},
            }
            yield {
                "event": "on_chat_model_end",
                "run_id": "real-a2a-run",
                "data": {"output": None},
            }

    class Heartbeat:
        active_tasks = 0
        current_task = ""

    card = AgentCard(
        name="channel-contract-test",
        description="real a2a-sdk local channel contract",
        version="0.0.0",
        supported_interfaces=[
            AgentInterface(
                protocol_binding="https://a2a.g/v1", url="http://molecule.local"
            )
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    executor = RuntimeA2AExecutor(Agent(), heartbeat=Heartbeat(), model="test")
    app = Starlette(routes=build_routes(card, executor, adapter_error=None))
    spec = DaemonSpec(
        name="bridge", plugin="slack-channel-molecule", kind="channel", command=["x"]
    )
    manager = ChannelEventSocketManager(app, [spec])

    await manager.start()
    try:
        response = await send_channel_message(
            "PING",
            metadata={"chat_id": "C123", "user_id": "U456"},
            request_id="real-request",
            message_id="real-message",
            environ=spec.env,
            timeout_seconds=5,
        )
    finally:
        await manager.stop()

    assert response["id"] == "real-request"
    assert response["result"]["kind"] == "task"
    assert response["result"]["status"]["state"] == "completed"
    assert channel_message_response_text(response) == "real pong"


@pytest.mark.asyncio
async def test_streaming_ack_and_turn_complete_events_pass_through_unchanged():
    release_turn = asyncio.Event()

    async def stream(request: Request) -> StreamingResponse:
        body = await request.json()
        assert body["method"] == "message/stream"
        assert body["params"]["metadata"]["source"] == "lark-channel-molecule"

        async def events():
            yield b'data: {"kind":"status-update","status":{"state":"working"}}\n\n'
            await release_turn.wait()
            yield b'data: {"kind":"message","role":"agent","parts":[]}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    spec = DaemonSpec(
        name="bridge", plugin="lark-channel-molecule", kind="channel", command=["x"]
    )
    manager = ChannelEventSocketManager(
        Starlette(routes=[Route("/", stream, methods=["POST"])]),
        [spec],
        startup_timeout_seconds=3,
    )
    await manager.start()
    try:
        transport = httpx.AsyncHTTPTransport(uds=spec.env[CHANNEL_A2A_SOCKET_ENV])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://molecule.local", timeout=3
        ) as client:
            async with client.stream(
                "POST",
                "/",
                json=_message("message/stream"),
                headers={
                    "x-molecule-channel-capability": spec.env[CHANNEL_A2A_TOKEN_ENV]
                },
            ) as response:
                lines = response.aiter_lines()
                assert await anext(lines) == (
                    'data: {"kind":"status-update","status":{"state":"working"}}'
                )
                assert await anext(lines) == ""
                release_turn.set()
                assert await anext(lines) == (
                    'data: {"kind":"message","role":"agent","parts":[]}'
                )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_same_plugin_shares_identity_socket_different_plugins_do_not():
    specs = [
        DaemonSpec(
            name="one", plugin="lark-channel-molecule", kind="channel", command=["x"]
        ),
        DaemonSpec(
            name="two", plugin="lark-channel-molecule", kind="channel", command=["x"]
        ),
        DaemonSpec(
            name="one", plugin="slack-channel-molecule", kind="channel", command=["x"]
        ),
    ]
    manager = ChannelEventSocketManager(Starlette(), specs)
    try:
        await manager.start()
        assert (
            specs[0].env[CHANNEL_A2A_SOCKET_ENV] == specs[1].env[CHANNEL_A2A_SOCKET_ENV]
        )
        assert (
            specs[0].env[CHANNEL_A2A_SOCKET_ENV] != specs[2].env[CHANNEL_A2A_SOCKET_ENV]
        )
        assert specs[0].env[CHANNEL_A2A_TOKEN_ENV] == specs[1].env[CHANNEL_A2A_TOKEN_ENV]
        assert specs[0].env[CHANNEL_A2A_TOKEN_ENV] != specs[2].env[CHANNEL_A2A_TOKEN_ENV]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plugin_capability_prevents_cross_plugin_provenance_spoof():
    captured = []

    async def endpoint(request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse({"jsonrpc": "2.0", "id": "evt-1", "result": {}})

    lark = DaemonSpec(
        name="bridge", plugin="lark-channel", kind="channel", command=["x"]
    )
    slack = DaemonSpec(
        name="bridge", plugin="slack-channel", kind="channel", command=["x"]
    )
    manager = ChannelEventSocketManager(
        Starlette(routes=[Route("/", endpoint, methods=["POST"])]), [lark, slack]
    )
    await manager.start()
    try:
        transport = httpx.AsyncHTTPTransport(uds=lark.env[CHANNEL_A2A_SOCKET_ENV])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://molecule.local", timeout=3
        ) as client:
            missing = await client.post("/", json=_message())
            wrong = await client.post(
                "/",
                json=_message(),
                headers={"x-molecule-channel-capability": slack.env[CHANNEL_A2A_TOKEN_ENV]},
            )
            accepted = await client.post(
                "/",
                json=_message(),
                headers={"x-molecule-channel-capability": lark.env[CHANNEL_A2A_TOKEN_ENV]},
            )
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert accepted.status_code == 200
        assert len(captured) == 1
        assert captured[0]["params"]["metadata"]["source"] == "lark-channel"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_missing_plugin_identity_withholds_manifest_claimed_capability():
    spec = DaemonSpec(
        name="bridge",
        kind="channel",
        command=["x"],
        env={
            CHANNEL_A2A_SOCKET_ENV: "/tmp/manifest-claimed.sock",
            CHANNEL_A2A_TOKEN_ENV: "manifest-claimed-token",
            CHANNEL_PLUGIN_ID_ENV: "manifest-claimed-plugin",
            CHANNEL_API_VERSION_ENV: "manifest-claimed-version",
        },
    )
    manager = ChannelEventSocketManager(Starlette(), [spec])

    assert await manager.start() is True
    assert CHANNEL_A2A_SOCKET_ENV not in spec.env
    assert CHANNEL_A2A_TOKEN_ENV not in spec.env
    assert CHANNEL_PLUGIN_ID_ENV not in spec.env
    assert CHANNEL_API_VERSION_ENV not in spec.env


@pytest.mark.asyncio
async def test_non_channel_daemon_receives_no_channel_capability():
    spec = DaemonSpec(
        name="worker",
        plugin="background-worker",
        kind="mcp-server",
        command=["x"],
        env={
            CHANNEL_A2A_SOCKET_ENV: "/tmp/manifest-claimed.sock",
            CHANNEL_A2A_TOKEN_ENV: "manifest-claimed-token",
            CHANNEL_PLUGIN_ID_ENV: "manifest-claimed-plugin",
            CHANNEL_API_VERSION_ENV: "manifest-claimed-version",
        },
    )
    manager = ChannelEventSocketManager(Starlette(), [spec])

    assert await manager.start() is True
    assert CHANNEL_A2A_SOCKET_ENV not in spec.env
    assert CHANNEL_A2A_TOKEN_ENV not in spec.env
    assert CHANNEL_PLUGIN_ID_ENV not in spec.env
    assert CHANNEL_API_VERSION_ENV not in spec.env


@pytest.mark.asyncio
async def test_unsafe_socket_directory_fails_closed_without_spawning(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "channel-events"
    link.symlink_to(real_dir, target_is_directory=True)
    spec = DaemonSpec(
        name="bridge", plugin="lark-channel-molecule", kind="channel", command=["x"]
    )
    manager = ChannelEventSocketManager(Starlette(), [spec], socket_dir=link)

    with pytest.raises(RuntimeError, match="symlink"):
        await manager.start()
    assert CHANNEL_A2A_SOCKET_ENV not in spec.env
    assert CHANNEL_A2A_TOKEN_ENV not in spec.env
    assert CHANNEL_PLUGIN_ID_ENV not in spec.env


@pytest.mark.asyncio
async def test_bind_failure_removes_capability_then_starts_daemon_supervisor():
    class BoundServer:
        started = True

    class FailingTransport:
        cleared = False

        async def start(self):
            raise RuntimeError("uds unavailable")

        def clear_daemon_env(self):
            self.cleared = True

    class Supervisor:
        started = False

        def start(self):
            self.started = True

    event_transport = FailingTransport()
    supervisor = Supervisor()
    assert await start_supervisor_when_bound(
        BoundServer(), supervisor, event_transport=event_transport
    )
    assert event_transport.cleared is True
    assert supervisor.started is True


@pytest.mark.asyncio
async def test_add_specs_binds_new_lane_and_leaves_running_lanes_untouched():
    """Hot-install (scheduler-as-trigger-plugin): a plugin installed AFTER
    start() gets its private socket bound by add_specs() WITHOUT re-binding or
    disturbing the already-serving lanes."""
    async def endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": {"parts": []}})

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    first = DaemonSpec(name="bridge", plugin="lark-channel-molecule", kind="channel",
                       command=["x"])
    manager = ChannelEventSocketManager(app, [first], startup_timeout_seconds=3)
    try:
        assert await manager.start() is True
        first_sock = first.env[CHANNEL_A2A_SOCKET_ENV]
        first_tok = first.env[CHANNEL_A2A_TOKEN_ENV]
        assert stat.S_ISSOCK(os.stat(first_sock).st_mode)

        # Hot-add a second plugin's lane.
        second = DaemonSpec(name="bridge2", plugin="slack-channel-molecule",
                            kind="channel", command=["x"])
        added = await manager.add_specs([second])

        assert added == ["slack-channel-molecule"]
        # The new lane is bound + secured with its OWN socket/token.
        second_sock = second.env[CHANNEL_A2A_SOCKET_ENV]
        assert second_sock != first_sock
        assert stat.S_IMODE(os.stat(second_sock).st_mode) == 0o600
        assert stat.S_ISSOCK(os.stat(second_sock).st_mode)
        # The already-serving lane is untouched (same socket + token, still up).
        assert first.env[CHANNEL_A2A_SOCKET_ENV] == first_sock
        assert first.env[CHANNEL_A2A_TOKEN_ENV] == first_tok
        assert stat.S_ISSOCK(os.stat(first_sock).st_mode)

        # Re-adding an already-bound spec is a no-op (no re-bind).
        assert await manager.add_specs([second]) == []
        assert second.env[CHANNEL_A2A_SOCKET_ENV] == second_sock
    finally:
        await manager.stop()
