"""Unit tests for molecule_runtime.compact (runtime#133 compaction step).

Smallest-scope-first tests for the pure ``compact_messages`` function
(step 2 of the runtime#133 spec) + the :class:`CompactionStats`
record (step 4's runtime contribution). The integration into
``a2a_executor.py`` is exercised in the runtime's own test suite
(``test_a2a_executor.py``); this file pins the deterministic core.

The tests intentionally do NOT mock any LLM SDK or runtime —
``compact_messages`` is a pure function over a list of
``(role, content)`` tuples, and the tests assert exactly that
contract. If a future change to the heuristic is needed (LLM-driven
summarization, structured fact extraction, etc.), the existing
tests in this file act as the tripwire: a deliberate heuristic
change must update the tests in lock-step.
"""
from __future__ import annotations

import pytest

from molecule_runtime.compact import (
    DEFAULT_KEEP_RECENT_N,
    CompactionStats,
    compact_messages,
)


def _sys(content: str = "you are a helpful agent") -> tuple[str, str]:
    return ("system", content)


def _human(content: str) -> tuple[str, str]:
    return ("human", content)


def _ai(content: str) -> tuple[str, str]:
    return ("ai", content)


def _tool(content: str) -> tuple[str, str]:
    return ("tool", content)


class TestEmptyAndDegenerate:
    """Empty + degenerate inputs are no-ops, never crashes."""

    def test_empty_input_returns_empty(self) -> None:
        out, stats = compact_messages([])
        assert out == []
        assert stats == CompactionStats(
            original_count=0,
            compacted_count=0,
            dropped_count=0,
            system_preserved=False,
            recent_window_size=0,
        )

    def test_only_system_message_preserved(self) -> None:
        out, stats = compact_messages([_sys()])
        assert out == [_sys()]
        assert stats.original_count == 1
        assert stats.compacted_count == 1
        assert stats.dropped_count == 0
        assert stats.system_preserved is True
        assert stats.recent_window_size == 0

    def test_only_human_message_no_system(self) -> None:
        out, stats = compact_messages([_human("hi")])
        assert out == [_human("hi")]
        assert stats.system_preserved is False
        assert stats.recent_window_size == 1

    def test_keep_recent_n_zero_is_clamped_to_one(self) -> None:
        # A compaction that drops the entire conversation is a hard
        # reset, which is exactly what we are NOT doing. Clamp to
        # the smallest non-zero value to keep at least the most
        # recent message.
        out, stats = compact_messages(
            [_human("a"), _ai("b"), _human("c")],
            keep_recent_n=0,
        )
        # The clamped value is 1, so the most recent human("c") is
        # kept.
        assert out == [_human("c")]
        assert stats.recent_window_size == 1


class TestKeepsSystemAndRecentN:
    """The core heuristic: system preserved at head, last N at tail, middle dropped."""

    def test_system_at_head_recent_n_at_tail_middle_dropped(self) -> None:
        msgs = [
            _sys(),                     # 0: system
            _human("h1"), _ai("a1"),    # 1-2: turn 1
            _human("h2"), _ai("a2"),    # 3-4: turn 2
            _human("h3"), _ai("a3"),    # 5-6: turn 3
            _human("h4"), _ai("a4"),    # 7-8: turn 4
            _human("h5"), _ai("a5"),    # 9-10: turn 5 (recent)
        ]
        out, stats = compact_messages(msgs, keep_recent_n=4)
        # System + last 4 (a4, h5, a5) = 4 messages kept.
        # Wait — last 4 of the NON-SYSTEM messages: a3, h4, a4, h5,
        # a5 (5 messages). keep_recent_n=4 means take the last 4:
        # h4, a4, h5, a5.
        assert out == [
            _sys(),
            _human("h4"), _ai("a4"),
            _human("h5"), _ai("a5"),
        ]
        assert stats.system_preserved is True
        assert stats.recent_window_size == 4
        assert stats.original_count == 11
        assert stats.compacted_count == 5
        assert stats.dropped_count == 6

    def test_no_system_message_compaction_still_works(self) -> None:
        # The heuristic must work even when the input has no
        # system message (defensive — most A2A sessions DO have
        # one but boot or resume edge cases may not).
        msgs = [_human("h1"), _ai("a1"), _human("h2"), _ai("a2"), _human("h3"), _ai("a3")]
        out, stats = compact_messages(msgs, keep_recent_n=2)
        assert out == [_human("h3"), _ai("a3")]
        assert stats.system_preserved is False
        assert stats.recent_window_size == 2
        assert stats.dropped_count == 4

    def test_tool_messages_in_recent_window_are_kept(self) -> None:
        # Tool messages inside the recent window must survive; the
        # heuristic preserves by position, not by role (within the
        # last-N window).
        msgs = [
            _sys(),                                          # 0
            _human("h1"), _ai("a1"),                         # 1-2
            _tool("obsolete tool result in middle"),         # 3
            _human("h2"), _ai("a2"),                         # 4-5
            _tool("recent tool result"),                      # 6
            _human("h3"),                                    # 7
        ]
        # 8 input messages total. non_system list (7 items):
        # [h1, a1, tool_middle, h2, a2, tool_recent, h3].
        # Last 4 = [h2, a2, tool_recent, h3].
        out, stats = compact_messages(msgs, keep_recent_n=4)
        assert out == [
            _sys(),
            _human("h2"), _ai("a2"),
            _tool("recent tool result"),
            _human("h3"),
        ]
        assert stats.recent_window_size == 4
        # 8 input - 5 output = 3 dropped (h1, a1, tool_middle).
        assert stats.dropped_count == 3
        assert stats.original_count == 8
        assert stats.compacted_count == 5

    def test_tool_messages_in_middle_are_dropped(self) -> None:
        # Inverse of the above: a tool message that lives in the
        # middle (outside the recent window) is dropped along with
        # the rest of the middle.
        msgs = [
            _sys(),                                          # 0
            _human("h1"), _ai("a1"),                         # 1-2
            _tool("middle tool result - will be dropped"),   # 3
            _human("h2"), _ai("a2"),                         # 4-5
            _human("h3"), _ai("a3"),                         # 6-7
        ]
        # 8 input messages total. non_system list (7 items):
        # [h1, a1, tool_middle, h2, a2, h3, a3].
        # Last 2 (keep_recent_n=2) = [h3, a3].
        out, stats = compact_messages(msgs, keep_recent_n=2)
        assert out == [_sys(), _human("h3"), _ai("a3")]
        assert _tool("middle tool result - will be dropped") not in out
        # 8 input - 3 output = 5 dropped (h1, a1, tool_middle, h2, a2).
        assert stats.dropped_count == 5


class TestNoOpWhenNothingToDrop:
    """When the input is already small, compaction is a no-op."""

    def test_input_smaller_than_window_no_drop(self) -> None:
        msgs = [_sys(), _human("h1"), _ai("a1")]
        out, stats = compact_messages(msgs, keep_recent_n=4)
        # 3 input, 3 output — nothing to drop.
        assert out == msgs
        assert stats.dropped_count == 0
        assert stats.compacted_count == 3
        assert stats.recent_window_size == 2

    def test_input_exactly_at_window_size_no_drop(self) -> None:
        # 1 system + 4 non-system = 5 total. keep_recent_n=4 means
        # we keep all 4 non-system + the system = 5. No drop.
        msgs = [_sys(), _human("h1"), _ai("a1"), _human("h2"), _ai("a2")]
        out, stats = compact_messages(msgs, keep_recent_n=4)
        assert out == msgs
        assert stats.dropped_count == 0
        assert stats.recent_window_size == 4


class TestDefaultKeepRecentN:
    """The default is the spec-bounded conservative 4."""

    def test_default_is_four(self) -> None:
        # The spec does not pin a specific N; 4 is the runtime's
        # conservative starting point (a recent user/assistant
        # exchange plus a couple of tool-result round-trips). If
        # you change this number, also update the runtime#133
        # spec's "what to preserve" section + the brief-notice
        # text the executor emits.
        assert DEFAULT_KEEP_RECENT_N == 4


class TestMultipleSystemMessages:
    """Defensive: handle inputs with more than one system message gracefully."""

    def test_only_first_system_preserved(self) -> None:
        # Unusual but defensive: an A2A session with multiple
        # system messages (e.g. one from the agent's native
        # framework, one injected by the platform). We keep the
        # first and fold the rest into non-system (so they may be
        # dropped if outside the recent window).
        msgs = [
            _sys("first system"),            # 1
            _sys("second system"),           # 2
            _human("h1"), _ai("a1"),         # 3-4
            _human("h2"), _ai("a2"),         # 5-6
            _human("h3"), _ai("a3"),         # 7-8
        ]
        # 8 input total. non_system list: [second_sys, h1, a1,
        # h2, a2, h3, a3] = 7 items. Last 2 (keep_recent_n=2) =
        # [h3, a3]. Output: [first_sys, h3, a3] = 3 items.
        # Dropped = 8 - 3 = 5 (second_sys, h1, a1, h2, a2 — note
        # a2 is also dropped because it's outside the last-2
        # window).
        out, stats = compact_messages(msgs, keep_recent_n=2)
        assert out[0] == _sys("first system")
        assert _sys("second system") not in out
        assert out == [_sys("first system"), _human("h3"), _ai("a3")]
        assert stats.system_preserved is True
        assert stats.recent_window_size == 2
        assert stats.dropped_count == 5
