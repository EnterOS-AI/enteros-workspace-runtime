"""RC #203 (durability): the delegation-results QUEUE must be as durable as the
harvest tombstone, so ``commit tombstone after append`` genuinely survives a
container restart.

THE BUG (pre-fix): ``_check_delegations`` appended the harvested result to
``DELEGATION_RESULTS_FILE`` and only THEN committed the durable ``(id, status)``
tombstone — but that queue defaulted to ``/tmp/delegation_results.jsonl`` (tmpfs).
A restart AFTER the append but BEFORE the executor consumed the queue lost ``/tmp``
while the durable tombstone survived, so the restart re-harvest saw the tombstone
and PERMANENTLY suppressed the result — defeating the ordering fix.

THE FIX: kernel-ON the queue lives on the DURABLE mailbox volume beside the
tombstone (``mailbox_dir.delegation_results_file``). The writer, the executor
reader (``read_delegation_results``) and the idle-loop guard all resolve the SAME
durable path, so a restart re-DELIVERS the queued result instead of dropping it.
Kernel-OFF keeps the legacy ``/tmp`` default (byte-identical).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime import heartbeat as hb_mod  # noqa: E402
from molecule_runtime.heartbeat import HeartbeatLoop  # noqa: E402
from molecule_runtime.executor_helpers import read_delegation_results  # noqa: E402

_WS = "00000000-0000-0000-0000-0000deadbeef"
_PEER = "7ca64ad4-0000-0000-0000-000000000000"


def _make_resp(json_payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _make_client(delegations_payload, post_recorder):
    client = MagicMock()

    async def _get(url, params=None, headers=None):
        if url.endswith("/delegations"):
            return _make_resp(delegations_payload)
        return _make_resp({"parent_id": ""})

    async def _post(url, json=None, headers=None, timeout=None):
        post_recorder.append((url, json))
        return _make_resp({}, status_code=200)

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    return client


def _row(did, status="completed", summary="done"):
    return {
        "delegation_id": did,
        "target_id": _PEER,
        "source_id": _WS,
        "status": status,
        "summary": summary,
        "response_preview": "APPROVED",
        "error": "",
    }


def _self_msgs(posts):
    return [p for p in posts if p[0].endswith(f"/workspaces/{_WS}/a2a")]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hb_mod, "auth_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "self_source_headers", lambda *a, **kw: {})
    monkeypatch.setattr(hb_mod, "_in_flight_delegation_ids", lambda: set())
    hb_mod.reset_awaited_delegations_for_test()
    yield
    hb_mod.reset_awaited_delegations_for_test()


@pytest.mark.asyncio
async def test_kernel_on_queue_is_durable_and_redelivers_after_restart(tmp_path, monkeypatch):
    """Kernel-ON with the DEFAULT queue (no override): the harvested result lands
    on the durable mailbox volume, so after a simulated restart the executor
    reader RE-DELIVERS it even though the durable tombstone is committed."""
    # Mailbox kernel ON, durable state under tmp. Crucially: NO explicit
    # DELEGATION_RESULTS_FILE override and the module attribute left at its
    # import-time legacy default, so resolution falls through to the durable
    # mailbox queue (the fix under test).
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / ".molecule"))
    monkeypatch.delenv("DELEGATION_RESULTS_FILE", raising=False)
    monkeypatch.setattr(
        hb_mod, "DELEGATION_RESULTS_FILE", hb_mod._LEGACY_DELEGATION_RESULTS_FILE
    )

    durable_queue = tmp_path / ".molecule" / "delegation_results.jsonl"
    tombstones = tmp_path / ".molecule" / ".delegation_tombstones"

    # The writer must resolve the queue ONTO the durable mailbox volume, not /tmp.
    assert Path(hb_mod._delegation_results_file()) == durable_queue
    assert Path(mailbox_dir.delegation_results_file()) == durable_queue

    # Session A: harvest d1 -> append to durable queue, THEN commit tombstone.
    hb_a = HeartbeatLoop("http://p", _WS)
    hb_mod.register_awaited_delegation("d1")
    posts_a: list = []
    await hb_a._check_delegations(_make_client([_row("d1")], posts_a))

    assert len(_self_msgs(posts_a)) == 1, "the harvested result should have woken the agent"
    assert durable_queue.exists(), "kernel-ON result queue MUST be on the durable mailbox volume"
    assert json.loads(durable_queue.read_text().strip())["delegation_id"] == "d1"
    # The durable tombstone IS committed — this is exactly what WOULD suppress the
    # result forever on restart if the queue had been the (lost) /tmp file.
    assert "d1\tcompleted" in tombstones.read_text()

    # SIMULATE RESTART: a fresh container loses /tmp (tmpfs) but KEEPS the mailbox
    # volume. The executor reader consumes the durable queue on the next turn and
    # RE-DELIVERS the result — the tombstone did NOT suppress it because the queue
    # survived. (read_delegation_results resolves the SAME durable path.)
    out = read_delegation_results()
    assert "[completed]" in out and "done" in out, "queued result must be re-delivered after restart"
    # Consumed atomically — no duplicate on the next read.
    assert not durable_queue.exists()
    assert read_delegation_results() == ""


def test_kernel_off_queue_is_legacy_tmp_byte_identical(monkeypatch):
    """Kernel-OFF resolution is the legacy /tmp default — byte-identical."""
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.delenv("DELEGATION_RESULTS_FILE", raising=False)
    assert mailbox_dir.delegation_results_file() == "/tmp/delegation_results.jsonl"


def test_explicit_env_override_wins_in_both_modes(tmp_path, monkeypatch):
    """An explicit DELEGATION_RESULTS_FILE env wins whether the kernel is on or off."""
    custom = str(tmp_path / "custom_results.jsonl")
    monkeypatch.setenv("DELEGATION_RESULTS_FILE", custom)

    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    assert mailbox_dir.delegation_results_file() == custom

    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / ".molecule"))
    assert mailbox_dir.delegation_results_file() == custom
