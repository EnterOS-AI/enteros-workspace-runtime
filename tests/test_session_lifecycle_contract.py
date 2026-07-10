"""Phase-1 contract tests for the session-lifecycle adapter-socket seam.

Guards that the read-side surface + honest single-session defaults are ADDITIVE:
the base defaults never claim multi-session support they don't have, they report
the real active session when one exists, and the new capability flag is off by
default (so every existing adapter stays single-session — no routing change).
The write side (session_start / session_resume actually re-routing) is a later
phase; here they must be honest no-ops.
"""

from __future__ import annotations

import pytest

from molecule_runtime.adapter_base import (
    AdapterConfig,
    BaseAdapter,
    RuntimeCapabilities,
    SessionRef,
)


class _SessionAdapter(BaseAdapter):
    @staticmethod
    def name() -> str:
        return "claude-code"

    @staticmethod
    def display_name() -> str:
        return "Session Fake"

    @staticmethod
    def description() -> str:
        return "Fake adapter for session-lifecycle contract tests"

    async def setup(self, config: AdapterConfig) -> None:
        return None

    async def create_executor(self, config: AdapterConfig):
        return None


class _LifecycleAdapter(_SessionAdapter):
    """Declares the lifecycle capability (as claude-code will once it reads
    its native JSONL store) — used to check `supported` tracks the flag."""

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(provides_native_session_lifecycle=True)


def test_session_ref_to_dict_shape():
    ref = SessionRef(id="sess-1", label="hi", message_count=3, is_active=True)
    assert ref.to_dict() == {
        "id": "sess-1",
        "label": "hi",
        "created_at": None,
        "last_active_at": None,
        "message_count": 3,
        "is_active": True,
    }


def test_capability_flag_is_additive_and_off_by_default():
    caps = RuntimeCapabilities()
    assert caps.provides_native_session_lifecycle is False
    # New key present in the (open) capability map, distinct from durability.
    d = caps.to_dict()
    assert d["session_lifecycle"] is False
    assert d["session"] is False  # provides_native_session unchanged
    # Every other existing flag still defaults False — no behavior flipped.
    assert not any(d.values())


@pytest.mark.asyncio
async def test_defaults_no_session_yet():
    """No executor/session → current is None, list is empty, active_id None."""
    a = _SessionAdapter()
    cur = await a.session_current()
    assert cur == {"runtime": "claude-code", "supported": False, "session": None}
    lst = await a.session_list()
    assert lst == {
        "runtime": "claude-code",
        "supported": False,
        "sessions": [],
        "active_id": None,
    }


@pytest.mark.asyncio
async def test_defaults_report_real_active_session():
    """With a stable executor session id, current + list report it honestly."""
    a = _SessionAdapter()
    a._executor = type("E", (), {"_session_id": "ws-stable-42"})()

    cur = await a.session_current()
    assert cur["session"]["id"] == "ws-stable-42"
    assert cur["session"]["is_active"] is True

    lst = await a.session_list()
    assert lst["active_id"] == "ws-stable-42"
    assert [s["id"] for s in lst["sessions"]] == ["ws-stable-42"]
    # Read side is honest: a single-session base never claims multi-session.
    assert lst["supported"] is False


@pytest.mark.asyncio
async def test_write_side_is_honest_noop_in_phase1():
    """session_start / session_resume must not pretend to route yet."""
    a = _SessionAdapter()
    started = await a.session_start(label="fresh")
    assert started == {
        "runtime": "claude-code",
        "supported": False,
        "session": None,
        "started": False,
    }
    resumed = await a.session_resume("whatever")
    assert resumed["supported"] is False
    assert resumed["resumed"] is False


@pytest.mark.asyncio
async def test_supported_tracks_the_capability_flag():
    """An adapter that declares the lifecycle capability reports supported=True
    from session_current (the flag is the single source of truth the canvas
    gates the switcher on)."""
    a = _LifecycleAdapter()
    a._executor = type("E", (), {"_session_id": "s1"})()
    cur = await a.session_current()
    assert cur["supported"] is True
    assert cur["session"]["id"] == "s1"
