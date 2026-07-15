"""Provider-neutral client contract for supervised channel plugins.

The SDK owns this module.  A workspace runtime implements the authenticated
Unix-socket host and injects the versioned capability into ``kind: channel``
plugin daemons.  Provider plugins use this client; Molecule Core has no channel
adapters, routes, credentials, or provider-specific configuration contract.

The module deliberately has no ``molecule_runtime`` import.  It is also safe to
vendor byte-for-byte into a plugin artifact when the host does not install the
authoring SDK.  Only :func:`send_channel_message` needs the optional ``httpx``
dependency supplied by the workspace runtime (or ``molecule-ai-sdk[channel]``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from os import PathLike
from typing import Any, Protocol, runtime_checkable


CHANNEL_API_VERSION = "1"
"""Channel client/host contract version implemented by this module."""

CHANNEL_API_VERSION_ENV = "MOLECULE_CHANNEL_API_VERSION"
"""Runtime-injected channel contract version."""

CHANNEL_A2A_SOCKET_ENV = "MOLECULE_CHANNEL_A2A_SOCKET"
"""Runtime-created private Unix socket for the plugin's local A2A request."""

CHANNEL_A2A_TOKEN_ENV = "MOLECULE_CHANNEL_A2A_TOKEN"
"""Ephemeral per-plugin capability for the private Unix socket."""

CHANNEL_PLUGIN_ID_ENV = "MOLECULE_CHANNEL_PLUGIN_ID"
"""Runtime-owned plugin identity used for trusted provenance stamping."""

CHANNEL_CAPABILITY_HEADER = "x-molecule-channel-capability"
"""HTTP header carrying the ephemeral local capability."""

# --- trigger lane (kind: trigger) --------------------------------------------
# A trigger plugin (scheduler is the first type) gets the SAME private local-A2A
# socket mechanism as a channel, but its provenance is stamped as an autonomous
# self-turn (source_type in an allow-list) rather than an external channel
# source. Distinct env-var names so a channel and a trigger daemon in the same
# workspace get their own capabilities and neither can read the other's.
TRIGGER_A2A_SOCKET_ENV = "MOLECULE_TRIGGER_A2A_SOCKET"
"""Runtime-created private Unix socket for a trigger plugin's local A2A self-turn."""

TRIGGER_A2A_TOKEN_ENV = "MOLECULE_TRIGGER_A2A_TOKEN"
"""Ephemeral per-plugin capability for the trigger's private Unix socket."""

TRIGGER_PLUGIN_ID_ENV = "MOLECULE_TRIGGER_PLUGIN_ID"
"""Runtime-owned trigger-plugin identity used for trusted provenance stamping."""

TRIGGER_API_VERSION_ENV = "MOLECULE_TRIGGER_API_VERSION"
"""Runtime-injected trigger contract version (shares CHANNEL_API_VERSION)."""


class ChannelCapabilityUnavailable(RuntimeError):
    """The host did not provide a complete, supported channel capability."""


class ChannelProtocolError(RuntimeError):
    """A complete channel response was a valid JSON-RPC error or bad result."""


class ChannelDeliveryUnknown(RuntimeError):
    """A request crossed the local boundary and therefore must not be replayed."""


@runtime_checkable
class ChannelMessageSender(Protocol):
    """Callable shape a provider bridge can depend on or replace in tests."""

    async def __call__(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        request_id: str | None = None,
        message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


def build_channel_message_send_request(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical A2A ``message/send`` request for an inbound turn.

    Provider metadata lives only at ``params.metadata``.  ``source`` is removed
    because it is runtime-owned and stamped after capability verification.
    """
    if not isinstance(text, str):
        raise TypeError("channel message text must be a string")

    resolved_request_id = request_id or str(uuid.uuid4())
    resolved_message_id = message_id or resolved_request_id
    parts: list[dict[str, Any]] = [{"kind": "text", "text": text}]
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            raise TypeError("channel attachments must be objects")
        parts.append({"kind": "file", "file": dict(attachment)})

    params: dict[str, Any] = {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": resolved_message_id,
            "parts": parts,
        }
    }
    if metadata is not None:
        clean_metadata = dict(metadata)
        clean_metadata.pop("source", None)
        params["metadata"] = clean_metadata

    return {
        "jsonrpc": "2.0",
        "id": resolved_request_id,
        "method": "message/send",
        "params": params,
    }


def channel_message_response_text(payload: dict[str, Any]) -> str:
    """Return agent text from an A2A task/message result.

    A JSON-RPC error or non-object result is a protocol error.  An empty
    completed result is valid and returns an empty string.
    """
    error = payload.get("error")
    if error is not None:
        detail = error.get("message") if isinstance(error, dict) else str(error)
        if not detail:
            detail = str(error)
        raise ChannelProtocolError(f"channel A2A error: {str(detail)[:500]}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise ChannelProtocolError("channel A2A result was not an object")

    status = result.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict):
            text = _text_from_parts(message.get("parts"))
            if text:
                return text

    text = _text_from_parts(result.get("parts"))
    if text:
        return text

    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        artifact_text = "".join(
            _text_from_parts(artifact.get("parts"))
            for artifact in artifacts
            if isinstance(artifact, dict)
        )
        if artifact_text:
            return artifact_text
    return ""


def _text_from_parts(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("kind") == "text"
    )


async def send_channel_message(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    socket_path: str | PathLike[str] | None = None,
    capability_token: str | None = None,
    api_version: str | None = None,
    timeout_seconds: float = 600.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Send one inbound turn through the runtime's authenticated local host.

    Capability absence is known-safe and raises
    :class:`ChannelCapabilityUnavailable`.  Once an HTTP send is attempted,
    every transport, decode, or envelope failure raises
    :class:`ChannelDeliveryUnknown`; callers must not replay that provider event
    because the agent may already have accepted it.
    """
    env = os.environ if environ is None else environ
    resolved_version = (api_version or env.get(CHANNEL_API_VERSION_ENV, "")).strip()
    if not resolved_version:
        raise ChannelCapabilityUnavailable(
            f"{CHANNEL_API_VERSION_ENV} is absent; this host cannot run channel plugins"
        )
    if resolved_version != CHANNEL_API_VERSION:
        raise ChannelCapabilityUnavailable(
            "unsupported channel API version "
            f"{resolved_version!r}; plugin requires {CHANNEL_API_VERSION!r}"
        )

    resolved_path = os.fspath(socket_path) if socket_path is not None else ""
    resolved_path = resolved_path.strip() or env.get(CHANNEL_A2A_SOCKET_ENV, "").strip()
    if not resolved_path:
        raise ChannelCapabilityUnavailable(f"{CHANNEL_A2A_SOCKET_ENV} is absent")
    resolved_token = (capability_token or "").strip() or env.get(
        CHANNEL_A2A_TOKEN_ENV, ""
    ).strip()
    if not resolved_token:
        raise ChannelCapabilityUnavailable(f"{CHANNEL_A2A_TOKEN_ENV} is absent")

    request = build_channel_message_send_request(
        text,
        metadata=metadata,
        request_id=request_id,
        message_id=message_id,
        attachments=attachments,
    )
    try:
        import httpx
    except ImportError as error:  # pragma: no cover - packaging guard
        raise ChannelCapabilityUnavailable(
            "channel client requires httpx; install molecule-ai-sdk[channel]"
        ) from error

    transport = httpx.AsyncHTTPTransport(uds=resolved_path)
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
    send_attempted = False
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://molecule.local",
            timeout=timeout,
        ) as client:
            send_attempted = True
            response = await client.post(
                "/",
                json=request,
                headers={CHANNEL_CAPABILITY_HEADER: resolved_token},
            )
            response.raise_for_status()
    except Exception as error:
        if send_attempted:
            raise ChannelDeliveryUnknown(
                "local channel delivery outcome is unknown; this turn must not be replayed"
            ) from error
        raise

    try:
        payload = response.json()
    except ValueError as error:
        raise ChannelDeliveryUnknown(
            "local channel response was invalid after send; this turn must not be replayed"
        ) from error
    if not (
        isinstance(payload, dict)
        and payload.get("jsonrpc") == "2.0"
        and ("result" in payload or "error" in payload)
    ):
        raise ChannelDeliveryUnknown(
            "local channel response was malformed after send; this turn must not be replayed"
        )
    return payload


__all__ = [
    "CHANNEL_A2A_SOCKET_ENV",
    "CHANNEL_A2A_TOKEN_ENV",
    "CHANNEL_API_VERSION",
    "CHANNEL_API_VERSION_ENV",
    "CHANNEL_CAPABILITY_HEADER",
    "CHANNEL_PLUGIN_ID_ENV",
    "ChannelCapabilityUnavailable",
    "ChannelDeliveryUnknown",
    "ChannelMessageSender",
    "ChannelProtocolError",
    "build_channel_message_send_request",
    "channel_message_response_text",
    "send_channel_message",
]
