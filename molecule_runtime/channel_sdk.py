"""Provider-neutral client contract for supervised channel plugins.

The SDK owns this module.  A workspace runtime implements the authenticated
Unix-socket host and injects the versioned capability into ``kind: channel``
plugin daemons.  Provider plugins use this client; Molecule Core has no channel
adapters, routes, credentials, or provider-specific configuration contract.

The module deliberately has no ``molecule_runtime`` import.  It is also safe to
vendor byte-for-byte into a plugin artifact when the host does not install the
authoring SDK.  Only the network calls (:func:`send_channel_message`,
:func:`send_trigger_message`, :func:`probe_trigger_liveness`) need the optional
``httpx`` dependency supplied by the workspace runtime (or
``molecule-ai-sdk[channel]``).
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
"""HTTP header carrying the ephemeral local capability (shared by both lanes)."""

# --- trigger lane (kind: trigger) --------------------------------------------
# A trigger plugin (scheduler is the first type) gets the SAME private local-A2A
# socket mechanism as a channel, but its provenance is stamped as an autonomous
# self-turn (``source_type`` in an allow-list) rather than an external channel
# source. Distinct env-var names so a channel and a trigger daemon in the same
# workspace get their own capabilities and neither can read the other's. The
# host validates the same capability header and shares CHANNEL_API_VERSION.
TRIGGER_A2A_SOCKET_ENV = "MOLECULE_TRIGGER_A2A_SOCKET"
"""Runtime-created private Unix socket for a trigger plugin's local A2A self-turn."""

TRIGGER_A2A_TOKEN_ENV = "MOLECULE_TRIGGER_A2A_TOKEN"
"""Ephemeral per-plugin capability for the trigger's private Unix socket."""

TRIGGER_PLUGIN_ID_ENV = "MOLECULE_TRIGGER_PLUGIN_ID"
"""Runtime-owned trigger-plugin identity used for trusted provenance stamping."""

TRIGGER_API_VERSION_ENV = "MOLECULE_TRIGGER_API_VERSION"
"""Runtime-injected trigger contract version (shares CHANNEL_API_VERSION)."""

TRIGGER_DEFAULT_SOURCE_TYPE = "self-scheduler"
"""Autonomous self-turn ``source_type`` a scheduler trigger requests by default.

The host re-validates this against its own allow-list and stamps the runtime-owned
``source`` provenance; a client cannot widen the grant by asserting another value.
"""

TRIGGER_LIVENESS_PATH = "/turn-liveness"
"""Capability-gated GET on the trigger lane returning the runtime's turn-lease snapshot.

The runtime already owns exactly ONE honest liveness signal for an in-flight
turn — the turn lease, which is *touched* on every tool call and expires only
after an idle TTL with no touch, under an un-bypassable absolute wall-clock cap.
This path exposes that same object (never a second, drifting mechanism) to the
trigger daemon that fired the turn, so the daemon can distinguish "working" from
"wedged" instead of guessing from elapsed wall time.

Response body (``application/json``):

``{"lease": false, "reason": str}``
    No lease is installed (the mailbox kernel is off). The caller has NO liveness
    signal and must fall back to its own absolute ceiling.

``{"lease": true, "idle_seconds": float, "ttl_seconds": float,
   "turn_age_seconds": float, "absolute_cap_seconds": float,
   "idle_expired": bool, "absolute_cap_exceeded": bool, "alive": bool}``
    ``alive`` is ``not idle_expired and not absolute_cap_exceeded`` — the single
    verdict; the components are reported so a caller can name the CAUSE of a
    stall rather than record an indeterminate outcome.

A host that predates this contract answers 404, which
:func:`probe_trigger_liveness` reports as "no signal" rather than an error.
"""

TRIGGER_LIVENESS_PROBE_TIMEOUT_SECONDS = 5.0
"""Wall-clock bound for the liveness probe itself.

The probe is a cheap local read of an in-memory object. Unlike the turn it
reports on, it MUST be bounded: an unbounded probe against a wedged host would
be the very failure the probe exists to detect.
"""


class ChannelCapabilityUnavailable(RuntimeError):
    """The host did not provide a complete, supported local-A2A capability.

    Raised by both the channel and trigger clients: capability absence is
    known-safe (the request never crossed the boundary), so the caller may skip.
    """


class ChannelProtocolError(RuntimeError):
    """A complete local-A2A response was a valid JSON-RPC error or bad result."""


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
    timeout_seconds: float | None = 600.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Send one inbound turn through the runtime's authenticated local host.

    Capability absence is known-safe and raises
    :class:`ChannelCapabilityUnavailable`.  Once an HTTP send is attempted,
    every transport, decode, or envelope failure raises
    :class:`ChannelDeliveryUnknown`; callers must not replay that provider event
    because the agent may already have accepted it.
    """
    request = build_channel_message_send_request(
        text,
        metadata=metadata,
        request_id=request_id,
        message_id=message_id,
        attachments=attachments,
    )
    return await _post_local_a2a(
        request,
        lane="channel",
        socket_env=CHANNEL_A2A_SOCKET_ENV,
        token_env=CHANNEL_A2A_TOKEN_ENV,
        version_env=CHANNEL_API_VERSION_ENV,
        socket_path=socket_path,
        capability_token=capability_token,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        environ=environ,
    )


def build_trigger_message_send_request(
    text: str,
    *,
    source_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Build the A2A ``message/send`` request for an autonomous trigger self-turn.

    Unlike a channel turn, a trigger turn carries a ``source_type`` (a routine
    self-ping class the host allow-lists — ``self-scheduler`` by default) instead
    of an external ``source``.  The runtime-owned ``source`` provenance is stamped
    host-side after capability verification, so any client-supplied ``source`` is
    stripped here and the host re-validates ``source_type`` against its allow-list.
    """
    if not isinstance(text, str):
        raise TypeError("trigger message text must be a string")

    resolved_request_id = request_id or str(uuid.uuid4())
    resolved_message_id = message_id or resolved_request_id
    clean_metadata = dict(metadata or {})
    clean_metadata.pop("source", None)
    clean_metadata["source_type"] = (
        source_type or clean_metadata.get("source_type") or TRIGGER_DEFAULT_SOURCE_TYPE
    )

    return {
        "jsonrpc": "2.0",
        "id": resolved_request_id,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": resolved_message_id,
                "parts": [{"kind": "text", "text": text}],
            },
            "metadata": clean_metadata,
        },
    }


async def send_trigger_message(
    text: str,
    *,
    source_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    message_id: str | None = None,
    socket_path: str | PathLike[str] | None = None,
    capability_token: str | None = None,
    api_version: str | None = None,
    timeout_seconds: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fire one autonomous self-turn through the runtime's authenticated host.

    Same delivery contract as :func:`send_channel_message`: capability absence is
    known-safe (:class:`ChannelCapabilityUnavailable`); once the request crosses
    the boundary any failure raises :class:`ChannelDeliveryUnknown` and must not
    be replayed.  A trigger daemon should therefore treat an unknown outcome as
    "possibly fired" and advance its schedule state rather than retry.

    ``timeout_seconds`` defaults to ``None`` — NO read deadline.  A self-turn
    returns only when the agent's turn completes, and how long that legitimately
    takes is a property of the WORK, not of the transport.  A flat HTTP read
    timeout here is a wall-clock turn-duration policy wearing a transport
    costume: it fires on a genuinely-working agent, and because the request had
    already crossed the boundary it can only be reported as
    :class:`ChannelDeliveryUnknown` — an outcome nobody can act on.  Liveness is
    instead judged by the runtime's turn lease via
    :func:`probe_trigger_liveness`, which resets on tool activity and therefore
    tells "working" apart from "wedged".  The connect phase stays bounded (a
    socket that will not accept a connection is a genuine transport failure), and
    a caller that wants a deadline may still pass one explicitly.
    """
    request = build_trigger_message_send_request(
        text,
        source_type=source_type,
        metadata=metadata,
        request_id=request_id,
        message_id=message_id,
    )
    return await _post_local_a2a(
        request,
        lane="trigger",
        socket_env=TRIGGER_A2A_SOCKET_ENV,
        token_env=TRIGGER_A2A_TOKEN_ENV,
        version_env=TRIGGER_API_VERSION_ENV,
        socket_path=socket_path,
        capability_token=capability_token,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        environ=environ,
    )


def _resolve_capability(
    *,
    lane: str,
    socket_env: str,
    token_env: str,
    version_env: str,
    socket_path: str | PathLike[str] | None,
    capability_token: str | None,
    api_version: str | None,
    environ: Mapping[str, str] | None,
) -> tuple[str, str]:
    """Resolve ``(socket_path, capability_token)`` for one lane, or refuse.

    Absence or a version mismatch raises :class:`ChannelCapabilityUnavailable`
    BEFORE any I/O, which is the known-safe classification every caller relies on
    (the request never crossed the boundary).
    """
    env = os.environ if environ is None else environ
    resolved_version = (api_version or env.get(version_env, "")).strip()
    if not resolved_version:
        raise ChannelCapabilityUnavailable(
            f"{version_env} is absent; this host cannot run {lane} plugins"
        )
    if resolved_version != CHANNEL_API_VERSION:
        raise ChannelCapabilityUnavailable(
            f"unsupported {lane} API version "
            f"{resolved_version!r}; plugin requires {CHANNEL_API_VERSION!r}"
        )

    resolved_path = os.fspath(socket_path) if socket_path is not None else ""
    resolved_path = resolved_path.strip() or env.get(socket_env, "").strip()
    if not resolved_path:
        raise ChannelCapabilityUnavailable(f"{socket_env} is absent")
    resolved_token = (capability_token or "").strip() or env.get(token_env, "").strip()
    if not resolved_token:
        raise ChannelCapabilityUnavailable(f"{token_env} is absent")
    return resolved_path, resolved_token


def _lane_timeout(timeout_seconds: float | None) -> Any:
    """Build the httpx timeout for a lane request.

    ``None`` means NO read/write/pool deadline — the caller (not the transport)
    owns any duration policy — while the CONNECT phase stays bounded, because a
    socket that will not accept a connection is a genuine transport failure and
    not a long-running turn.
    """
    import httpx

    connect = 5.0 if timeout_seconds is None else min(timeout_seconds, 5.0)
    return httpx.Timeout(timeout_seconds, connect=connect)


async def probe_trigger_liveness(
    *,
    socket_path: str | PathLike[str] | None = None,
    capability_token: str | None = None,
    api_version: str | None = None,
    timeout_seconds: float = TRIGGER_LIVENESS_PROBE_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Read the runtime's turn-lease snapshot over the trigger lane.

    Returns the ``{"lease": true, ...}`` snapshot documented on
    :data:`TRIGGER_LIVENESS_PATH` when the host has a lease installed, and
    ``None`` when there is NO liveness signal to be had — the host predates the
    contract (404), no lease is installed (kernel off), or the probe itself
    failed.  ``None`` is deliberately not an error: "I could not learn whether
    the turn is alive" is a real state a caller must handle by falling back to
    its own absolute ceiling, and conflating it with "the turn is dead" would
    reintroduce the wall-clock kill this contract exists to remove.

    Capability absence still raises :class:`ChannelCapabilityUnavailable`,
    matching :func:`send_trigger_message`: a daemon with no capability has no
    business probing.  This call is a READ and never mutates turn state, so
    unlike a delivery it is safe to retry and is always time-bounded.
    """
    resolved_path, resolved_token = _resolve_capability(
        lane="trigger",
        socket_env=TRIGGER_A2A_SOCKET_ENV,
        token_env=TRIGGER_A2A_TOKEN_ENV,
        version_env=TRIGGER_API_VERSION_ENV,
        socket_path=socket_path,
        capability_token=capability_token,
        api_version=api_version,
        environ=environ,
    )
    try:
        import httpx
    except ImportError as error:  # pragma: no cover - packaging guard
        raise ChannelCapabilityUnavailable(
            "trigger client requires httpx; install molecule-ai-sdk[channel]"
        ) from error

    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=resolved_path),
            base_url="http://molecule.local",
            timeout=_lane_timeout(timeout_seconds),
        ) as client:
            response = await client.get(
                TRIGGER_LIVENESS_PATH,
                headers={CHANNEL_CAPABILITY_HEADER: resolved_token},
            )
    except Exception:  # noqa: BLE001 - a failed probe is "no signal", never fatal
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("lease") is not True:
        return None
    return payload


async def _post_local_a2a(
    request: dict[str, Any],
    *,
    lane: str,
    socket_env: str,
    token_env: str,
    version_env: str,
    socket_path: str | PathLike[str] | None,
    capability_token: str | None,
    api_version: str | None,
    timeout_seconds: float | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Resolve the per-lane capability and POST one request over the Unix socket.

    Shared transport for both the channel and trigger clients; ``lane`` only
    labels the human-readable error text.  Capability absence raises
    :class:`ChannelCapabilityUnavailable` before any send; once a send is
    attempted, every failure raises :class:`ChannelDeliveryUnknown`.
    """
    resolved_path, resolved_token = _resolve_capability(
        lane=lane,
        socket_env=socket_env,
        token_env=token_env,
        version_env=version_env,
        socket_path=socket_path,
        capability_token=capability_token,
        api_version=api_version,
        environ=environ,
    )

    try:
        import httpx
    except ImportError as error:  # pragma: no cover - packaging guard
        raise ChannelCapabilityUnavailable(
            f"{lane} client requires httpx; install molecule-ai-sdk[channel]"
        ) from error

    transport = httpx.AsyncHTTPTransport(uds=resolved_path)
    timeout = _lane_timeout(timeout_seconds)
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
                f"local {lane} delivery outcome is unknown; this turn must not be replayed"
            ) from error
        raise

    try:
        payload = response.json()
    except ValueError as error:
        raise ChannelDeliveryUnknown(
            f"local {lane} response was invalid after send; this turn must not be replayed"
        ) from error
    if not (
        isinstance(payload, dict)
        and payload.get("jsonrpc") == "2.0"
        and ("result" in payload or "error" in payload)
    ):
        raise ChannelDeliveryUnknown(
            f"local {lane} response was malformed after send; this turn must not be replayed"
        )
    return payload


__all__ = [
    "CHANNEL_A2A_SOCKET_ENV",
    "CHANNEL_A2A_TOKEN_ENV",
    "CHANNEL_API_VERSION",
    "CHANNEL_API_VERSION_ENV",
    "CHANNEL_CAPABILITY_HEADER",
    "CHANNEL_PLUGIN_ID_ENV",
    "TRIGGER_A2A_SOCKET_ENV",
    "TRIGGER_A2A_TOKEN_ENV",
    "TRIGGER_API_VERSION_ENV",
    "TRIGGER_DEFAULT_SOURCE_TYPE",
    "TRIGGER_LIVENESS_PATH",
    "TRIGGER_LIVENESS_PROBE_TIMEOUT_SECONDS",
    "TRIGGER_PLUGIN_ID_ENV",
    "ChannelCapabilityUnavailable",
    "ChannelDeliveryUnknown",
    "ChannelMessageSender",
    "ChannelProtocolError",
    "build_channel_message_send_request",
    "build_trigger_message_send_request",
    "channel_message_response_text",
    "probe_trigger_liveness",
    "send_channel_message",
    "send_trigger_message",
]
