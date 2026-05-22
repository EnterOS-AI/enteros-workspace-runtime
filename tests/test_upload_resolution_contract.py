"""Layer A (RFC#640 follow-up) — structural tests pinning the
MANDATORY upload-resolution contract in ``_build_channel_instructions``.

This contract is the SSOT spec for any /activity-polling adapter that
consumes ``method=chat_upload_receive`` rows. Skipping any step results
in silent file loss — the agent sees ``platform-pending:`` URIs it
cannot open, with no error surfaced.

These tests assert the contract section is PRESENT + WELL-FORMED in
the instructions text. A copy-edit that drops the contract section
(or any of its five required steps) will fail CI here before reaching
production, matching the discipline declared in
``_build_channel_instructions``'s docstring.

Why this is a structural test, not a doc-only check: the instructions
string is read by every spec-compliant MCP client at ``initialize`` and
surfaced to the agent's system prompt automatically. If the contract
section disappears, every TS adapter still in development would start
shipping without resolution — exactly the regression this layer fixes.
"""

from __future__ import annotations

import re

from molecule_runtime.a2a_mcp_server import _build_channel_instructions


class TestUploadResolutionContractPresent:
    def test_section_header_present(self):
        out = _build_channel_instructions()
        assert "Upload resolution (MANDATORY for any /activity-polling adapter)" in out, (
            "the Upload resolution section header is the SSOT entry "
            "point for downstream adapter authors — must remain "
            "discoverable in the instructions text"
        )

    def test_mandatory_keyword_present(self):
        out = _build_channel_instructions()
        assert "MANDATORY" in out, (
            "the MANDATORY keyword is the contract-strength signal — "
            "adapters reading the instructions need an unambiguous "
            "must vs. should distinction here"
        )

    def test_chat_upload_receive_trigger_named(self):
        out = _build_channel_instructions()
        assert "method=chat_upload_receive" in out, (
            "the activity_logs row method that triggers resolution "
            "must be cited by name so adapters know what to dispatch on"
        )


class TestUploadResolutionContractFiveSteps:
    """The contract has exactly five steps. Each one must appear in
    the instructions text with its specific surface call-out. If a
    refactor accidentally collapses two steps or drops one, the
    resolution flow becomes ambiguous and adapters guess wrong."""

    def test_step1_content_fetch_endpoint(self):
        out = _build_channel_instructions()
        assert "GET /workspaces/<ws>/pending-uploads/<file_id>/content" in out, (
            "step 1 must name the content-fetch endpoint exactly so "
            "adapters can curl it without inferring the URL shape"
        )

    def test_step2_local_cache_persist(self):
        out = _build_channel_instructions()
        # Step 2 is the local persist step — must mention BOTH the
        # Claude Code channel cache path AND the Python in-container path
        # so consumers in both ecosystems have a copy-paste reference.
        assert "~/.claude/channels/molecule/inbox" in out
        assert "/workspace/.molecule/chat-uploads" in out

    def test_step3_ack_endpoint(self):
        out = _build_channel_instructions()
        assert "POST `/workspaces/<ws>/pending-uploads/<file_id>/ack`" in out, (
            "step 3 must name the ack endpoint exactly so adapters "
            "POST against the right URL — and so the Phase 3 sweep "
            "knows the row was processed"
        )

    def test_step4_uri_cache_lru(self):
        out = _build_channel_instructions()
        # Step 4 is the URI cache — must reference the LRU size convention
        # so TS adapters match Python's URI_CACHE_MAX_ENTRIES.
        assert "URI cache" in out
        assert "platform-pending:<ws>/<file_id>" in out
        assert "URI_CACHE_MAX_ENTRIES" in out

    def test_step5_uri_rewrite_walk(self):
        out = _build_channel_instructions()
        # Step 5 is the URI rewrite — must mention BOTH the top-level
        # attachments[] surface AND the embedded message-parts surface
        # so adapters don't only rewrite one side.
        assert "rewrite to the cached local URI" in out
        assert "attachments[]" in out
        assert "message.parts" in out


class TestUploadResolutionReferenceImplementations:
    """The contract names two reference implementations: the Python
    one (already merged + production-deployed) and the TS one
    (pending Layer B). Both must be cited so adapter authors have a
    canonical reference to mirror."""

    def test_python_reference_named(self):
        out = _build_channel_instructions()
        assert "molecule_runtime/inbox_uploads.py" in out, (
            "Python reference path must be exact — adapter authors "
            "will grep for it and need to land on the right file"
        )

    def test_ts_reference_named(self):
        out = _build_channel_instructions()
        assert "@molecule-ai/mcp-server/src/inbox-uploads.ts" in out, (
            "TS reference path must be exact even though Layer B "
            "hasn't published yet — the contract cites the target "
            "so TS adapter authors know where to look once it lands"
        )

    def test_contract_test_layer_d_cited(self):
        out = _build_channel_instructions()
        assert "contract test" in out, (
            "the Layer D contract test that fails-CI on adapters "
            "missing the resolution flow must be cited so adapter "
            "authors know they cannot opt out silently"
        )


class TestUploadResolutionStepOrdering:
    """The five steps must appear in order 1..5 in the text — adapters
    expected to implement them sequentially (fetch → persist → ack →
    cache → rewrite), and an out-of-order spec is dangerous because a
    reader could implement them in the order presented."""

    def test_steps_appear_in_order(self):
        out = _build_channel_instructions()
        # Find the section, then scan forward for the numbered markers.
        section_start = out.find("Upload resolution (MANDATORY")
        assert section_start >= 0, "section must exist"
        section_text = out[section_start:]
        # The first occurrence of each "  N." line marker — must be
        # ascending in offset terms (i.e. "  1." appears before "  2."
        # which appears before "  3." etc.).
        offsets = []
        for n in ("  1.", "  2.", "  3.", "  4.", "  5."):
            off = section_text.find(n)
            assert off >= 0, f"step marker {n!r} missing"
            offsets.append(off)
        assert offsets == sorted(offsets), (
            f"steps must appear in 1..5 order; got offsets {offsets}"
        )


class TestUploadResolutionAttachmentBulletUpdated:
    """The attachment-fields bullet (existing surface) must reference
    the new Upload resolution contract by name — adapters reading the
    field doc need to be pointed at the MUST-DO flow when they see a
    ``platform-pending:`` URI."""

    def test_attachment_bullet_references_contract(self):
        out = _build_channel_instructions()
        # The bullet lives in the "In both paths the same fields apply"
        # block. It must point readers at the resolution contract
        # rather than the previous soft-language "the inbox poller
        # swaps" (which implied an optional convenience).
        assert "platform-pending:<ws>/<file_id>" in out
        assert "Upload resolution contract" in out

    def test_attachment_kind_includes_video(self):
        # The L1 flat-upload arm in mc#1657 derives `video` from
        # `video/*` mime prefix; the kind enumeration in the doc must
        # reflect that or downstream adapters will reject video rows
        # as unknown-kind.
        out = _build_channel_instructions()
        # Match the actual kind enumeration in the bullet.
        m = re.search(r"`kind` is `file`,\s*`image`,\s*`audio`,\s*or\s*`video`", out)
        assert m is not None, (
            "kind enumeration must include video — L1 flat-upload "
            "arm produces it from video/* mime prefix"
        )
