"""Acked delivery (MUST-FIX 3) — runtime side: the inbox poller acks the
platform after a drain, gated behind the mailbox kernel, and treats a 404 as a
SOFT failure (platform-before-runtime ordering). Also pins that the inbox
cursor moves onto the durable mailbox volume when the kernel is on.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.configs_dir as configs_dir  # noqa: E402
import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime import inbox  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


def _install_stub_httpx(monkeypatch, get_payload, post_recorder, post_status=200):
    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _Resp(200, get_payload)

        def post(self, url, json=None, headers=None):
            post_recorder.append((url, json))
            return _Resp(post_status, {})

    stub = mock.MagicMock()
    stub.Client = _StubClient
    monkeypatch.setitem(sys.modules, "httpx", stub)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.delenv(mailbox_dir.MAILBOX_DIR_ENV, raising=False)
    yield


# ---------------------------------------------------------------------------
# Faithful model of the molecule-core List handler projection.
#
# The inert-ack bug lived in the projection hop: the GET /workspaces/:id/
# activity List handler (workspace-server/internal/handlers/activity.go)
# used `seq` in its WHERE/ORDER BY tuple cursor but did NOT SELECT it or put
# it on the returned row. So every feed row reached the runtime WITHOUT a
# seq, the poller's `int(row.get("seq", 0))` stayed 0, `if max_seq > 0`
# never fired, no /activity/ack was ever POSTed, and acked-prune reclaimed
# nothing (retention degraded to the 30d hard ceiling → ~4x activity_logs
# growth).
#
# The OLD test masked this: `_a2a_row` hand-injected `seq` straight into the
# API-shape dict, so the ack fired in the test even though the real feed
# carried no seq. This models the handler's projection instead: seq starts
# as an activity_logs DB column and only reaches the feed row if the
# projection carries it. Drop `seq` from LIST_HANDLER_PROJECTED_COLUMNS
# (mirroring the core regression) and the ack goes inert here too.
#
# LIST_HANDLER_PROJECTED_COLUMNS mirrors the `entry` map built in
# activity.go (post core PR #3373). NULLABLE_JSON columns mirror the
# `if reqBody != nil { entry["request_body"] = ... }` guards.
LIST_HANDLER_PROJECTED_COLUMNS = (
    "id", "workspace_id", "activity_type", "source_id", "target_id",
    "method", "summary", "duration_ms", "status", "error_detail",
    "created_at", "seq",
)
_LIST_HANDLER_NULLABLE_JSON_COLUMNS = ("request_body", "response_body", "tool_trace")


def _db_activity_row(row_id: str, seq: int) -> dict:
    """A full activity_logs DB row — the shape the handler SELECTs FROM.

    ``seq`` is a NOT-NULL DB column (20260604000000_activity_logs_seq
    migration); it always exists at the DB layer. Whether it reaches the
    runtime depends entirely on the handler's projection.
    """
    return {
        "id": row_id,
        "workspace_id": "ws-1",
        "activity_type": "a2a_receive",
        "source_id": "peer-1",
        "target_id": "ws-1",
        "method": "message/send",
        "summary": "hi",
        "request_body": {"params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}},
        "response_body": None,
        "tool_trace": None,
        "duration_ms": None,
        "status": "ok",
        "error_detail": None,
        "created_at": "2026-06-30T00:00:00Z",
        "seq": seq,
    }


def _list_handler_feed_row(db_row: dict) -> dict:
    """Project a DB row into the /activity feed shape the List handler emits.

    Copies ONLY the columns the handler projects (LIST_HANDLER_PROJECTED_
    COLUMNS) plus the non-null JSON columns — exactly mirroring the Go
    `entry` map. Because seq must travel db_row -> projection -> feed row,
    a test fed from this helper proves the ack path works against the REAL
    (non-fabricated) feed shape, not a hand-stuffed dict.
    """
    entry = {k: db_row[k] for k in LIST_HANDLER_PROJECTED_COLUMNS if k in db_row}
    for k in _LIST_HANDLER_NULLABLE_JSON_COLUMNS:
        if db_row.get(k) is not None:
            entry[k] = db_row[k]
    return entry


def _a2a_row(row_id: str, seq: int) -> dict:
    """An /activity feed row shaped exactly like the real List handler output.

    seq flows from the DB row THROUGH the projection — it is NOT injected
    directly into the API shape. If core stops projecting seq, this row
    loses it and the ack tests below go red (de-masked).
    """
    return _list_handler_feed_row(_db_activity_row(row_id, seq))


def test_no_ack_when_kernel_off(monkeypatch, tmp_path):
    posts: list = []
    _install_stub_httpx(monkeypatch, [_a2a_row("10", 7)], posts)
    state = inbox.InboxState(cursor_path=tmp_path / "cursor")
    inbox._poll_once(state, "https://platform.test", "ws-1", headers={}, timeout_secs=5.0)
    ack_posts = [p for p in posts if p[0].endswith("/activity/ack")]
    assert ack_posts == [], "kernel OFF must not POST an ack (byte-identical)"


def test_ack_posts_max_seq_when_kernel_on(monkeypatch, tmp_path):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    posts: list = []
    _install_stub_httpx(monkeypatch, [_a2a_row("10", 7), _a2a_row("11", 12)], posts)
    state = inbox.InboxState(cursor_path=tmp_path / "cursor")
    inbox._poll_once(state, "https://platform.test", "ws-1", headers={}, timeout_secs=5.0)
    ack_posts = [p for p in posts if p[0].endswith("/activity/ack")]
    assert len(ack_posts) == 1, "kernel ON acks once per drain"
    assert ack_posts[0][0] == "https://platform.test/workspaces/ws-1/activity/ack"
    assert ack_posts[0][1] == {"acked_seq": 12}, "acks the MAX seq in the batch"


def test_ack_404_is_soft_fail(monkeypatch):
    posts: list = []
    _install_stub_httpx(monkeypatch, [], posts, post_status=404)
    ok = inbox._post_activity_ack("https://platform.test", "ws-1", {}, 5, timeout_secs=1.0)
    assert ok is False, "a 404 (endpoint absent) degrades gracefully, not fatally"


def test_ack_200_returns_true(monkeypatch):
    posts: list = []
    _install_stub_httpx(monkeypatch, [], posts, post_status=200)
    assert inbox._post_activity_ack("https://platform.test", "ws-1", {}, 5) is True


def test_cursor_moves_to_mailbox_when_kernel_on(monkeypatch, tmp_path):
    # OFF: cursor lives under configs_dir (legacy, byte-identical).
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))
    off = inbox.default_cursor_path()
    assert off == configs_dir.resolve() / ".mcp_inbox_cursor"

    # ON: cursor lives on the durable mailbox volume.
    base = tmp_path / "ws" / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    on = inbox.default_cursor_path()
    assert on == base / ".mcp_inbox_cursor"


# ---------------------------------------------------------------------------
# De-masking: the ack must fire from a REAL List-handler-shaped feed row, and
# must NOT fire when seq is absent from the projection (the inert-ack bug).
# ---------------------------------------------------------------------------


def test_ack_fires_from_real_list_handler_row_shape(monkeypatch, tmp_path):
    """End-to-end-ish: a feed row projected the way core's List handler
    projects it (seq sourced from the DB column, carried through the
    projection) drives a real ack. This is the de-masked replacement for
    the old hand-fabricated-seq test."""
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")

    feed_row = _a2a_row("evt-1", 42)
    # The projected feed row carries seq because the modeled core
    # projection includes it — sourced from the DB row, not fabricated
    # into the API shape.
    assert "seq" in feed_row, "real List handler shape must include seq"
    assert feed_row["seq"] == 42

    posts: list = []
    _install_stub_httpx(monkeypatch, [feed_row], posts)
    state = inbox.InboxState(cursor_path=tmp_path / "cursor")
    inbox._poll_once(state, "https://platform.test", "ws-1", headers={}, timeout_secs=5.0)

    ack_posts = [p for p in posts if p[0].endswith("/activity/ack")]
    assert len(ack_posts) == 1, "a real-shaped drained batch must ack exactly once"
    assert ack_posts[0][0] == "https://platform.test/workspaces/ws-1/activity/ack"
    assert ack_posts[0][1] == {"acked_seq": 42}, "acks the seq that survived projection"


def test_no_ack_when_seq_absent_from_projection(monkeypatch, tmp_path):
    """Regression sentinel proving the ack tests genuinely depend on seq
    being projected. This reconstructs the PRE-FIX core behavior: the DB
    row has seq, but the projection DROPS it (the inert-ack bug). The
    poller sees no seq, max_seq stays 0, and NO ack is POSTed. If this
    test ever starts posting an ack, the ack path stopped depending on
    the projected seq and the de-masking above is worthless."""
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")

    db_row = _db_activity_row("evt-1", 42)
    # Buggy projection: every projected column EXCEPT seq (+ the non-null
    # request_body so message extraction still works) — i.e. exactly what
    # the old List handler returned before it projected seq.
    buggy_row = {
        k: db_row[k]
        for k in LIST_HANDLER_PROJECTED_COLUMNS
        if k != "seq" and k in db_row
    }
    buggy_row["request_body"] = db_row["request_body"]
    assert "seq" not in buggy_row

    posts: list = []
    _install_stub_httpx(monkeypatch, [buggy_row], posts)
    state = inbox.InboxState(cursor_path=tmp_path / "cursor")
    inbox._poll_once(state, "https://platform.test", "ws-1", headers={}, timeout_secs=5.0)

    ack_posts = [p for p in posts if p[0].endswith("/activity/ack")]
    assert ack_posts == [], "a seq-less feed row (pre-fix core) must produce NO ack"


def test_core_list_handler_source_projects_seq():
    """Cross-repo contract check: when molecule-core is co-located (dev /
    integrated environment), assert the REAL List handler source actually
    projects seq — into BOTH the SELECT clause and the returned entry map.
    This is the empirical tie between this runtime test's projection model
    and core's true output shape (core PR #3373). Skips in the isolated
    runtime CI where molecule-core isn't checked out."""
    # tests/ -> repo root -> MoleculesAI base -> molecule-core/...
    activity_go = (
        _THIS.parents[2]
        / "molecule-core"
        / "workspace-server"
        / "internal"
        / "handlers"
        / "activity.go"
    )
    if not activity_go.is_file():
        pytest.skip("molecule-core not co-located; skipping cross-repo seq-projection contract")

    text = activity_go.read_text(encoding="utf-8", errors="ignore")
    assert '"seq": seq' in text, (
        "core List handler must project seq into the entry map "
        "(runtime ack derives max_seq from row['seq'])"
    )
    assert "`seq`" in text, "core List handler SELECT must project the seq column"
