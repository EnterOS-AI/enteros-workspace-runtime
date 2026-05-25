"""Layer 2 of the 3-layer activity-feed enrichment.

Layer 1 (molecule-core PR mc#1654) ships ``GET /workspaces/:id/activity
?include=peer_info`` returning row-level ``peer_name`` / ``peer_role`` /
``agent_card_url`` / ``attachments[]`` on a2a_receive rows. Layer 2 (this
test file's target — ``inbox.py`` + ``a2a_tools_inbox.py`` +
``a2a_mcp_server.py``) reads those fields defensively:

* ``message_from_activity`` populates ``InboxMessage`` from the row.
  Missing fields stay None (pre-Layer-1 platforms, canvas rows,
  deleted-peer rows).
* ``InboxMessage.to_dict`` emits the new fields only when set
  (omit-when-absent envelope rule).
* ``_extract_attachments_from_request_body`` parses
  ``request_body.params.message.parts[]`` inline so the poll path
  surfaces attachments even on pre-Layer-1 platforms (registry-fallback).
* ``_enrich_inbound_for_agent`` (a2a_tools_inbox) skips the registry
  round-trip when Layer 1 already supplied name + role + url.
* ``_build_channel_notification`` (a2a_mcp_server push path) prefers
  Layer-1-supplied values over the local registry cache and falls back
  cleanly when only some are present.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# _extract_attachments_from_request_body — inline parser used both
# directly by Layer 2 (registry-fallback) and indirectly by Layer 1's
# Go-side helper which produces the same shape.
# ---------------------------------------------------------------------------


class TestExtractAttachmentsFromRequestBody:
    def setup_method(self):
        from molecule_runtime.inbox import _extract_attachments_from_request_body
        self._extract = _extract_attachments_from_request_body

    def test_empty_body(self):
        assert self._extract({}) is None

    def test_no_params(self):
        assert self._extract({"jsonrpc": "2.0", "method": "message/send"}) is None

    def test_no_message(self):
        assert self._extract({"params": {}}) is None

    def test_no_parts(self):
        assert self._extract({"params": {"message": {}}}) is None

    def test_text_only_returns_none(self):
        body = {"params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}}
        assert self._extract(body) is None

    def test_v1_file_kind(self):
        body = {"params": {"message": {"parts": [
            {"kind": "text", "text": "see attached"},
            {"kind": "file", "file": {"uri": "workspace:foo.pdf", "mime_type": "application/pdf", "name": "foo.pdf"}},
        ]}}}
        atts = self._extract(body)
        assert atts == [{"kind": "file", "uri": "workspace:foo.pdf", "mime_type": "application/pdf", "name": "foo.pdf"}]

    def test_image_and_audio(self):
        body = {"params": {"message": {"parts": [
            {"kind": "image", "file": {"uri": "workspace:a.png", "mime_type": "image/png", "name": "a.png"}},
            {"kind": "audio", "file": {"uri": "workspace:b.mp3", "mime_type": "audio/mpeg", "name": "b.mp3"}},
        ]}}}
        atts = self._extract(body)
        assert atts is not None and len(atts) == 2
        assert atts[0]["kind"] == "image"
        assert atts[1]["kind"] == "audio"

    def test_v0_type_discriminator_with_inlined_fields(self):
        # Legacy v0 shape: type=file, fields inlined on the part itself
        body = {"params": {"message": {"parts": [
            {"type": "file", "uri": "workspace:legacy.txt", "mime_type": "text/plain", "name": "legacy.txt"},
        ]}}}
        atts = self._extract(body)
        assert atts == [{"kind": "file", "uri": "workspace:legacy.txt", "mime_type": "text/plain", "name": "legacy.txt"}]

    def test_skip_parts_with_no_uri_and_no_name(self):
        body = {"params": {"message": {"parts": [
            {"kind": "file", "file": {}},  # malformed — skip
            {"kind": "file", "file": {"name": "ok.bin"}},  # name-only — keep
        ]}}}
        atts = self._extract(body)
        assert atts == [{"kind": "file", "name": "ok.bin"}]

    def test_malformed_parts_list_returns_none(self):
        assert self._extract({"params": {"message": {"parts": "not-a-list"}}}) is None

    def test_non_dict_entries_skipped(self):
        body = {"params": {"message": {"parts": [
            None, 42, "string",
            {"kind": "file", "file": {"uri": "workspace:ok.bin"}},
        ]}}}
        atts = self._extract(body)
        assert atts == [{"kind": "file", "uri": "workspace:ok.bin"}]

    def test_flat_chat_upload_manifest_image(self):
        body = {
            "uri": "platform-pending:091a9180-b303-4a20-aefe-3a4a675b8aa4/26111d48-aaaa-bbbb-cccc-dddddddddddd",
            "name": "pasted-2026-05-25T01-22-33-0-0.png",
            "mimeType": "image/png",
            "size": 1234,
            "file_id": "26111d48-aaaa-bbbb-cccc-dddddddddddd",
        }
        atts = self._extract(body)
        assert atts == [{
            "kind": "image",
            "uri": "platform-pending:091a9180-b303-4a20-aefe-3a4a675b8aa4/26111d48-aaaa-bbbb-cccc-dddddddddddd",
            "mime_type": "image/png",
            "name": "pasted-2026-05-25T01-22-33-0-0.png",
        }]


# ---------------------------------------------------------------------------
# InboxMessage.to_dict — omit-when-absent envelope for the Layer-1 fields
# ---------------------------------------------------------------------------


class TestInboxMessageToDictLayer1:
    def _make(self, **overrides):
        from molecule_runtime.inbox import InboxMessage
        defaults = {
            "activity_id": "act-1",
            "text": "hello",
            "peer_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
        }
        defaults.update(overrides)
        return InboxMessage(**defaults)

    def test_default_omits_layer1_fields(self):
        d = self._make().to_dict()
        for k in ("peer_name", "peer_role", "agent_card_url", "attachments"):
            assert k not in d, f"{k} should be absent when not supplied"

    def test_peer_name_only_surfaces(self):
        d = self._make(peer_name="Production Manager").to_dict()
        assert d["peer_name"] == "Production Manager"
        assert "peer_role" not in d
        assert "agent_card_url" not in d
        assert "attachments" not in d

    def test_full_layer1_envelope(self):
        d = self._make(
            peer_name="Production Manager",
            peer_role="product manager",
            agent_card_url="https://platform.test/registry/discover/abc",
            attachments=[{"kind": "file", "uri": "workspace:r.pdf", "name": "r.pdf"}],
        ).to_dict()
        assert d["peer_name"] == "Production Manager"
        assert d["peer_role"] == "product manager"
        assert d["agent_card_url"] == "https://platform.test/registry/discover/abc"
        assert d["attachments"] == [{"kind": "file", "uri": "workspace:r.pdf", "name": "r.pdf"}]

    def test_empty_string_layer1_fields_omitted(self):
        # Empty-string values are treated as absent — never surface as
        # "" in the envelope.
        d = self._make(peer_name="", peer_role="", agent_card_url="").to_dict()
        for k in ("peer_name", "peer_role", "agent_card_url"):
            assert k not in d

    def test_empty_attachments_list_omitted(self):
        d = self._make(attachments=[]).to_dict()
        assert "attachments" not in d


# ---------------------------------------------------------------------------
# message_from_activity — Layer 1 row fields are read defensively
# ---------------------------------------------------------------------------


class TestMessageFromActivityLayer1:
    def test_reads_layer1_fields_when_present(self, monkeypatch):
        # Stub out the uploads-rewrite import so the test doesn't depend
        # on the upload-cache state.
        from molecule_runtime import inbox_uploads
        monkeypatch.setattr(inbox_uploads, "rewrite_request_body", lambda body: None)

        from molecule_runtime.inbox import message_from_activity

        row: dict[str, Any] = {
            "id": "act-x",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
            "summary": "Agent message: hi",
            "request_body": {
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}
            },
            # Layer-1 row-level fields
            "peer_name": "CEO Assistant",
            "peer_role": "operator orchestrator",
            "agent_card_url": "https://platform.test/registry/discover/11111111-2222-3333-4444-555555555555",
        }
        msg = message_from_activity(row)
        assert msg.peer_name == "CEO Assistant"
        assert msg.peer_role == "operator orchestrator"
        assert msg.agent_card_url == "https://platform.test/registry/discover/11111111-2222-3333-4444-555555555555"

    def test_missing_layer1_fields_stays_none(self, monkeypatch):
        from molecule_runtime import inbox_uploads
        monkeypatch.setattr(inbox_uploads, "rewrite_request_body", lambda body: None)

        from molecule_runtime.inbox import message_from_activity
        row = {
            "id": "act-x",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
            "summary": "Agent message: hi",
            "request_body": {"params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}},
        }
        msg = message_from_activity(row)
        assert msg.peer_name is None
        assert msg.peer_role is None
        assert msg.agent_card_url is None
        assert msg.attachments is None

    def test_attachments_from_layer1_row(self, monkeypatch):
        from molecule_runtime import inbox_uploads
        monkeypatch.setattr(inbox_uploads, "rewrite_request_body", lambda body: None)

        from molecule_runtime.inbox import message_from_activity
        row = {
            "id": "act-x",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
            "summary": "Agent message: with attachment",
            "request_body": {},  # body is empty
            "attachments": [
                {"kind": "file", "uri": "workspace:foo.pdf", "name": "foo.pdf"},
            ],
        }
        msg = message_from_activity(row)
        assert msg.attachments == [{"kind": "file", "uri": "workspace:foo.pdf", "name": "foo.pdf"}]

    def test_attachments_fallback_to_inline_parts(self, monkeypatch):
        # Pre-Layer-1 platform: no row-level attachments[] but the
        # request_body has file parts. The inline-parts extractor
        # populates msg.attachments.
        from molecule_runtime import inbox_uploads
        monkeypatch.setattr(inbox_uploads, "rewrite_request_body", lambda body: None)

        from molecule_runtime.inbox import message_from_activity
        row = {
            "id": "act-x",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
            "summary": "Agent message: file",
            "request_body": {"params": {"message": {"parts": [
                {"kind": "text", "text": "look"},
                {"kind": "file", "file": {"uri": "workspace:foo.pdf", "name": "foo.pdf", "mime_type": "application/pdf"}},
            ]}}},
        }
        msg = message_from_activity(row)
        assert msg.attachments == [
            {"kind": "file", "uri": "workspace:foo.pdf", "mime_type": "application/pdf", "name": "foo.pdf"},
        ]

    def test_layer1_row_attachments_wins_over_inline_parts(self, monkeypatch):
        # When both row-level attachments[] AND request_body.params.
        # message.parts[] are present, the row-level value wins
        # (platform's view is authoritative). Inline parts are the
        # registry-fallback, used only when the row doesn't supply.
        from molecule_runtime import inbox_uploads
        monkeypatch.setattr(inbox_uploads, "rewrite_request_body", lambda body: None)

        from molecule_runtime.inbox import message_from_activity
        row = {
            "id": "act-x",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
            "summary": "Agent message",
            "request_body": {"params": {"message": {"parts": [
                {"kind": "file", "file": {"uri": "workspace:inline.bin"}},
            ]}}},
            "attachments": [
                {"kind": "file", "uri": "workspace:from-layer1.bin"},
            ],
        }
        msg = message_from_activity(row)
        assert msg.attachments == [{"kind": "file", "uri": "workspace:from-layer1.bin"}]

    def test_malformed_attachments_filtered(self, monkeypatch):
        # Row-level attachments[] with garbage entries (no kind, non-
        # dict) get filtered. Only well-shaped entries propagate.
        from molecule_runtime import inbox_uploads
        monkeypatch.setattr(inbox_uploads, "rewrite_request_body", lambda body: None)

        from molecule_runtime.inbox import message_from_activity
        row = {
            "id": "act-x",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "method": "message/send",
            "created_at": "2026-05-21T21:00:00Z",
            "request_body": {},
            "attachments": [
                {"kind": "file", "uri": "workspace:ok.bin"},
                {"no_kind": True},
                "not-a-dict",
                None,
            ],
        }
        msg = message_from_activity(row)
        assert msg.attachments == [{"kind": "file", "uri": "workspace:ok.bin"}]


# ---------------------------------------------------------------------------
# _enrich_inbound_for_agent — skip-registry-when-layer1-complete logic
# ---------------------------------------------------------------------------


class TestEnrichInboundForAgentLayer1FastPath:
    def test_skips_registry_when_all_three_layer1_fields_present(self):
        # When name + role + url are all on the dict (because L1 supplied
        # them through InboxMessage.to_dict), no registry round-trip
        # should happen. We assert this by patching a2a_client and
        # verifying the patched function isn't called.
        from molecule_runtime import a2a_tools_inbox

        d = {
            "peer_id": "11111111-2222-3333-4444-555555555555",
            "peer_name": "From Layer 1",
            "peer_role": "from layer 1",
            "agent_card_url": "https://layer1.test/discover/x",
            "kind": "peer_agent",
        }

        # Build a stub a2a_client that records calls — if the helper
        # actually called enrich_peer_metadata_nonblocking, the call
        # count would tick.
        with mock.patch.dict(
            "sys.modules",
            {
                "molecule_runtime.a2a_client": mock.MagicMock(
                    _agent_card_url_for=mock.Mock(return_value="should-not-be-used"),
                    enrich_peer_metadata_nonblocking=mock.Mock(return_value=None),
                ),
            },
        ):
            out = a2a_tools_inbox._enrich_inbound_for_agent(dict(d))
            mod = __import__("molecule_runtime.a2a_client", fromlist=["_agent_card_url_for"])
            assert mod.enrich_peer_metadata_nonblocking.call_count == 0
            assert mod._agent_card_url_for.call_count == 0

        # Layer-1 values preserved verbatim
        assert out["peer_name"] == "From Layer 1"
        assert out["peer_role"] == "from layer 1"
        assert out["agent_card_url"] == "https://layer1.test/discover/x"

    def test_falls_through_to_registry_when_layer1_partial(self):
        # Only peer_name supplied — registry lookup must still happen
        # for peer_role; agent_card_url falls back to local helper
        # since L1 didn't supply.
        from molecule_runtime import a2a_tools_inbox

        d = {
            "peer_id": "11111111-2222-3333-4444-555555555555",
            "peer_name": "From Layer 1",  # only name
            "kind": "peer_agent",
        }
        with mock.patch.dict(
            "sys.modules",
            {
                "molecule_runtime.a2a_client": mock.MagicMock(
                    _agent_card_url_for=mock.Mock(return_value="https://fallback/discover/x"),
                    enrich_peer_metadata_nonblocking=mock.Mock(
                        return_value={"name": "Registry Name", "role": "from registry"}
                    ),
                ),
            },
        ):
            out = a2a_tools_inbox._enrich_inbound_for_agent(dict(d))
            mod = __import__("molecule_runtime.a2a_client", fromlist=["_agent_card_url_for"])
            assert mod.enrich_peer_metadata_nonblocking.call_count == 1
            assert mod._agent_card_url_for.call_count == 1

        # Layer-1 name wins over registry name
        assert out["peer_name"] == "From Layer 1"
        # Role comes from registry since L1 didn't supply
        assert out["peer_role"] == "from registry"
        # URL falls back to local helper
        assert out["agent_card_url"] == "https://fallback/discover/x"

    def test_canvas_user_no_enrichment(self):
        # peer_id="" means canvas_user — helper returns unchanged dict
        # without trying to enrich.
        from molecule_runtime import a2a_tools_inbox
        d = {"peer_id": "", "kind": "canvas_user"}
        with mock.patch.dict(
            "sys.modules",
            {"molecule_runtime.a2a_client": mock.MagicMock()},
        ):
            out = a2a_tools_inbox._enrich_inbound_for_agent(dict(d))
            mod = __import__("molecule_runtime.a2a_client")
        assert out == d


# ---------------------------------------------------------------------------
# Poller URL params include ?include=peer_info
# ---------------------------------------------------------------------------


class TestPollerRequestsPeerInfo:
    def test_poll_once_passes_include_peer_info(self, monkeypatch, tmp_path):
        """The poller appends include=peer_info to its /activity GET so
        Layer-1-aware platforms return enrichment. Pre-Layer-1 platforms
        silently ignore the param (additive opt-in)."""
        from molecule_runtime import inbox

        # Stub httpx to capture the request params
        captured = {}

        class _StubResp:
            status_code = 200

            def json(self):
                return []

        class _StubClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params=None, headers=None):
                captured["url"] = url
                captured["params"] = params or {}
                captured["headers"] = headers or {}
                return _StubResp()

        # Patch the httpx import inside _poll_once. The function does
        # `import httpx` inline; mock the whole module before the call.
        stub_httpx = mock.MagicMock()
        stub_httpx.Client = _StubClient
        monkeypatch.setitem(__import__("sys").modules, "httpx", stub_httpx)

        state = inbox.InboxState(cursor_path=tmp_path / "cursor")
        n = inbox._poll_once(state, "https://platform.test", "ws-1", headers={}, timeout_secs=5.0)
        assert n == 0  # empty response
        assert captured["params"].get("include") == "peer_info"
        assert captured["params"].get("type") == "a2a_receive"

    def test_poll_once_enqueues_chat_upload_receive_with_attachments(self, monkeypatch, tmp_path):
        """Image-only canvas uploads are real messages.

        The upload row itself is the only activity row for a pasted image
        without caption, so the poller must fetch/stage the bytes and then
        enqueue an InboxMessage carrying attachments[].
        """
        from molecule_runtime import inbox, inbox_uploads

        inbox_uploads.get_cache().clear()

        pending_uri = "platform-pending:091a9180-b303-4a20-aefe-3a4a675b8aa4/26111d48-aaaa-bbbb-cccc-dddddddddddd"
        local_uri = "workspace:/workspace/.molecule/chat-uploads/abc-pasted.png"
        row = {
            "id": "act-upload",
            "source_id": None,
            "method": "chat_upload_receive",
            "created_at": "2026-05-25T01:22:33Z",
            "summary": "chat_upload_receive: pasted-2026-05-25T01-22-33-0-0.png",
            "request_body": {
                "uri": pending_uri,
                "name": "pasted-2026-05-25T01-22-33-0-0.png",
                "mimeType": "image/png",
                "size": 1234,
                "file_id": "26111d48-aaaa-bbbb-cccc-dddddddddddd",
            },
        }

        class _StubResp:
            status_code = 200

            def json(self):
                return [row]

        class _StubClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params=None, headers=None):
                return _StubResp()

        class _FakeBatchFetcher:
            def __init__(self, *args, **kwargs):
                pass

            def submit(self, upload_row):
                inbox_uploads.get_cache().set(pending_uri, local_uri)

            def wait_all(self):
                pass

            def close(self):
                pass

        stub_httpx = mock.MagicMock()
        stub_httpx.Client = _StubClient
        monkeypatch.setitem(__import__("sys").modules, "httpx", stub_httpx)
        monkeypatch.setattr(inbox_uploads, "BatchFetcher", _FakeBatchFetcher)

        state = inbox.InboxState(cursor_path=tmp_path / "cursor")
        n = inbox._poll_once(state, "https://platform.test", "091a9180-b303-4a20-aefe-3a4a675b8aa4", headers={}, timeout_secs=5.0)

        assert n == 1
        messages = state.peek()
        assert len(messages) == 1
        msg = messages[0]
        assert msg.method == "chat_upload_receive"
        assert msg.peer_id == ""
        assert msg.attachments == [{
            "kind": "image",
            "uri": local_uri,
            "mime_type": "image/png",
            "name": "pasted-2026-05-25T01-22-33-0-0.png",
        }]
