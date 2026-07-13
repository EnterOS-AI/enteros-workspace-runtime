"""Contract gates for the SDK-owned channel client vendored by the runtime."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from molecule_runtime import channel_events
from molecule_runtime.adapter_base import RuntimeCapabilities


def test_channel_events_reexports_the_vendored_sdk_client() -> None:
    from molecule_runtime import channel_sdk

    assert channel_events.CHANNEL_API_VERSION == "1"
    assert channel_events.CHANNEL_API_VERSION_ENV == "MOLECULE_CHANNEL_API_VERSION"
    assert channel_events.build_channel_message_send_request is (
        channel_sdk.build_channel_message_send_request
    )
    assert channel_events.channel_message_response_text is (
        channel_sdk.channel_message_response_text
    )
    assert channel_events.send_channel_message is channel_sdk.send_channel_message
    assert channel_events.ChannelEventUnavailable is (
        channel_sdk.ChannelCapabilityUnavailable
    )
    assert channel_events.ChannelEventProtocolError is channel_sdk.ChannelProtocolError
    assert channel_events.ChannelEventDeliveryUnknown is (
        channel_sdk.ChannelDeliveryUnknown
    )


def test_sdk_builder_removes_client_claimed_source() -> None:
    request = channel_events.build_channel_message_send_request(
        "hello",
        metadata={"source": "spoofed", "chat_id": "C123"},
        request_id="req-1",
    )

    assert request["params"]["metadata"] == {"chat_id": "C123"}
    assert "metadata" not in request["params"]["message"]


def test_runtime_advertises_channel_plugin_dispatch_host() -> None:
    """Every 0.4+ runtime hosts API v1 independently of its agent adapter."""
    assert RuntimeCapabilities().to_dict()["channel_dispatch"] is True


def test_vendored_channel_sdk_matches_configured_sdk_source_byte_for_byte() -> None:
    source = os.environ.get("MOLECULE_CHANNEL_SDK_SOURCE", "").strip()
    if not source:
        pytest.skip("set MOLECULE_CHANNEL_SDK_SOURCE for the cross-repo drift gate")

    assert Path(source).read_bytes() == (
        Path(__file__).parents[1] / "molecule_runtime" / "channel_sdk.py"
    ).read_bytes()
