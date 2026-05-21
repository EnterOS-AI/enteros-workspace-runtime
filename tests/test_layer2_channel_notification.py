"""Push-path Layer 2 tests for `_build_channel_notification`.

Pairs with ``test_layer2_peer_info_enrichment.py`` which covers the
poll-path. The push path (a2a_mcp_server.py) and the poll path
(a2a_tools_inbox.py) MUST surface identical fields per the contract in
``_build_channel_instructions``; these tests pin that.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest import mock


# Stubbed identity / URL helpers used by _build_channel_notification.
# We patch them at module-level so the real registry-cache code path
# doesn't fire during tests.
#
# Why a custom @contextmanager and not just mock.patch.multiple:
# patch.multiple's `__enter__` returns a dict whose keys are ONLY the
# kwargs that were given the sentinel `mock.DEFAULT` value. Kwargs
# given explicit lambdas or Mock instances DON'T appear in the yielded
# dict. The tests below need to introspect call_count on
# `enrich_peer_metadata_nonblocking` and `_agent_card_url_for`, so we
# create the Mock instances OUTSIDE the patcher and yield them via a
# named dict the tests can reference (previously these tests assumed
# patch.multiple would expose them in the yielded dict — that
# misunderstanding silently broke the assertions with `KeyError` once
# the suite ran in CI).


@contextmanager
def _patched_module_imports(layer1_name=None, layer1_role=None, layer1_url=None,
                             registry_record=None, fallback_url="https://fallback/discover"):
    """Build a mock.patch context that swaps the helper functions
    a2a_mcp_server imports at module load. Yields a dict that maps the
    patched names this test file's assertions reference (`enrich_peer_
    metadata_nonblocking` + `_agent_card_url_for`) to the underlying
    `Mock` instances, so call_count assertions work."""
    enrich_mock = mock.Mock(return_value=registry_record)
    url_mock = mock.Mock(return_value=fallback_url)
    patcher = mock.patch.multiple(
        "molecule_runtime.a2a_mcp_server",
        _validate_peer_id=lambda pid: pid if pid else None,
        _sanitize_identity_field=lambda s: (s or "").strip() or None,
        _safe_meta_field=lambda v, allowed: v,
        _safe_activity_id=lambda v: v,
        _safe_ts=lambda v: v,
        _format_channel_content=lambda **kw: "fmt(" + kw.get("text", "") + ")",
        _channel_notification_method=lambda: "notifications/claude/channel",
        enrich_peer_metadata_nonblocking=enrich_mock,
        _agent_card_url_for=url_mock,
    )
    patcher.start()
    try:
        yield {
            "enrich_peer_metadata_nonblocking": enrich_mock,
            "_agent_card_url_for": url_mock,
        }
    finally:
        patcher.stop()


class TestBuildChannelNotificationLayer1:
    PEER_ID = "11111111-2222-3333-4444-555555555555"

    def _msg(self, **overrides):
        base = {
            "activity_id": "act-1",
            "text": "hello",
            "peer_id": self.PEER_ID,
            "kind": "peer_agent",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
        }
        base.update(overrides)
        return base

    def test_layer1_full_skips_registry(self):
        """When the InboxMessage dict already carries peer_name +
        peer_role + agent_card_url (because Layer 1 supplied them on
        the row), the push path must NOT call enrich_peer_metadata
        nor _agent_card_url_for — the platform's values are
        authoritative."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports(
            registry_record={"name": "FromCache", "role": "from cache"},
            fallback_url="https://wrong/discover",
        ) as patches:
            msg = self._msg(
                peer_name="From Layer 1",
                peer_role="from layer 1",
                agent_card_url="https://layer1.test/discover/abc",
            )
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            assert meta["peer_name"] == "From Layer 1"
            assert meta["peer_role"] == "from layer 1"
            assert meta["agent_card_url"] == "https://layer1.test/discover/abc"
            # Registry + fallback URL helper must not have been called
            assert patches["enrich_peer_metadata_nonblocking"].call_count == 0
            assert patches["_agent_card_url_for"].call_count == 0

    def test_pre_layer1_falls_back_to_registry(self):
        """Pre-Layer-1 message (no row-level peer_name/role/url) — the
        push path falls through to the registry-lookup helper and the
        local _agent_card_url_for builder. Same behavior as before
        Layer 1."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports(
            registry_record={"name": "FromRegistry", "role": "from registry"},
            fallback_url="https://fallback/discover/" + self.PEER_ID,
        ) as patches:
            msg = self._msg()  # no peer_* / agent_card_url
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            assert meta["peer_name"] == "FromRegistry"
            assert meta["peer_role"] == "from registry"
            assert meta["agent_card_url"] == "https://fallback/discover/" + self.PEER_ID
            assert patches["enrich_peer_metadata_nonblocking"].call_count == 1
            assert patches["_agent_card_url_for"].call_count == 1

    def test_layer1_name_only_keeps_layer1_url_from_fallback(self):
        """Layer 1 supplied peer_name but no agent_card_url. Registry
        provides role. URL falls back to local builder. peer_name from
        Layer 1 wins over registry record."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports(
            registry_record={"name": "FromRegistry", "role": "from registry"},
            fallback_url="https://fallback/discover/" + self.PEER_ID,
        ) as patches:
            msg = self._msg(peer_name="From Layer 1")
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            # Layer 1 name wins
            assert meta["peer_name"] == "From Layer 1"
            # Registry supplies role since L1 didn't
            assert meta["peer_role"] == "from registry"
            # URL from fallback builder
            assert meta["agent_card_url"] == "https://fallback/discover/" + self.PEER_ID

    def test_canvas_user_no_peer_fields(self):
        """canvas_user message (peer_id empty) emits no peer_name /
        peer_role / agent_card_url, regardless of Layer 1 fields."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports():
            msg = self._msg(peer_id="", kind="canvas_user")
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            assert "peer_name" not in meta
            assert "peer_role" not in meta
            assert "agent_card_url" not in meta

    def test_attachments_propagate_to_meta(self):
        """When the InboxMessage dict has attachments[], the push-path
        meta must include them so the Claude Code adaptor can render
        them as the synthetic-turn envelope (Layer 3 contract)."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports():
            msg = self._msg(
                attachments=[
                    {"kind": "file", "uri": "workspace:foo.pdf", "name": "foo.pdf", "mime_type": "application/pdf"},
                ],
            )
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            assert meta["attachments"] == [
                {"kind": "file", "uri": "workspace:foo.pdf", "name": "foo.pdf", "mime_type": "application/pdf"},
            ]

    def test_empty_attachments_list_omitted(self):
        """An empty attachments[] list is OMITTED from meta — same
        omit-when-absent rule as peer_name/peer_role/agent_card_url."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports():
            msg = self._msg(attachments=[])
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            assert "attachments" not in meta

    def test_no_attachments_key_omitted(self):
        """The dict simply lacks an `attachments` key — meta must omit."""
        from molecule_runtime import a2a_mcp_server as srv

        with _patched_module_imports():
            msg = self._msg()
            envelope = srv._build_channel_notification(msg)
            meta = envelope["params"]["meta"]
            assert "attachments" not in meta
