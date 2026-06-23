"""Conversation compaction for runtime#133 (smallest-scope-first).

The runtime-side contribution to runtime#133 "compact-and-continue"
behavior on context overflow. This module implements the deterministic
compaction step (step 2 of the runtime#133 spec, smallest-scope
version) and the brief notice (step 4's runtime contribution). Step 1
(detection) is in :mod:`molecule_runtime.context_budget`; step 3
("continue on compacted context") happens automatically because the
compaction runs BEFORE the LLM call that would have overflowed.

Why a heuristic ("keep system + last N turns, drop middle") and
not an LLM-driven summarization:
  - Smallest scope first: the heuristic is a pure function with
    no LLM call, no I/O, fully testable in isolation. The LLM-driven
    summarization that would extract "task/goal/decisions/blockers"
    per the spec is a follow-up ticket — it lives where the
    conversation state is owned (workspace agent, core), not in the
    runtime's A2A-orchestration layer.
  - Durable memory is already preserved by the system prompt
    (``prompt.py:DEFAULT_MEMORY_SNAPSHOT_FILES`` re-injects
    MEMORY.md / CLAUDE.md / AGENTS.md / etc. on every session), so
    dropping older turns does not lose cross-session facts.
  - Last-N turns typically contain the active task + the most
    recent tool outputs the agent is reasoning over. Dropping the
    middle is the highest-leverage "context is bloated" target.
  - Heuristic is auditable + testable: a reviewer can read the
    function and the unit tests, see exactly which turns are kept
    vs dropped, and pin the contract with a regression test.

Design notes (deliberately minimal):
  - ``compact_messages`` is a pure function: input list -> (output
    list, stats). No I/O, no exceptions, no logging (the caller
    emits the brief notice from the stats).
  - Tool messages (role != "system"/"human"/"ai") are dropped
    along with the middle window. The recent window keeps the
    N most recent messages regardless of role, so a recent tool
    result inside the window survives.
  - The system message is preserved by contract; if there is no
    system message in the input, the function still works (just
    returns a no-system version of the heuristic).
  - ``CompactionStats`` is the structured record the executor
    uses to emit the brief notice (logger.info) and to surface the
    event in OTEL/telemetry in a follow-up ticket.
"""
from __future__ import annotations

from dataclasses import dataclass


# Default number of recent messages to preserve after compaction.
# A "message" here is any (role, content) tuple — human, ai, tool.
# 4 is a deliberately conservative starting point: it covers the
# most recent user/assistant exchange plus a couple of tool-result
# round-trips, which is usually enough to keep the active task in
# the agent's working memory. If the future workspace-agent
# summarization ticket lands, this default can be tuned.
DEFAULT_KEEP_RECENT_N = 4

# Roles that get special treatment (the system message is
# always preserved, never dropped, even if it falls outside the
# "recent N" window).
SYSTEM_ROLE = "system"


@dataclass(frozen=True)
class CompactionStats:
    """Structured record of what compact_messages did.

    Fields:
        original_count: total messages before compaction.
        compacted_count: total messages after compaction.
        dropped_count: original_count - compacted_count.
        system_preserved: True if a system-role message was found
            and preserved at the head of the output.
        recent_window_size: number of trailing messages kept (the
            ``keep_recent_n`` parameter, capped at the actual
            number of non-system messages available).
    """
    original_count: int
    compacted_count: int
    dropped_count: int
    system_preserved: bool
    recent_window_size: int


def compact_messages(
    messages: list[tuple[str, str]],
    keep_recent_n: int = DEFAULT_KEEP_RECENT_N,
) -> tuple[list[tuple[str, str]], CompactionStats]:
    """Compact a conversation: keep system message + last N messages, drop middle.

    The smallest-scope-first heuristic for runtime#133. Pure function:
    no I/O, no exceptions, no logging. Caller (a2a_executor) emits the
    brief notice from the returned :class:`CompactionStats`.

    Algorithm:
        1. Find the system message (if any) and split the input into
           ``(system_msg, non_system_msgs)``.
        2. Take the last ``keep_recent_n`` items from ``non_system_msgs``
           as the "recent window".
        3. Output = ``[system_msg] + recent_window`` (system preserved
           at the head, recent window at the tail).
        4. Stats: counts + whether system was preserved + actual recent
           window size used (capped at the number of non-system msgs
           available).

    Edge cases:
        - Empty input: returns ``([], stats_with_zero_counts)``.
        - Only system: returns ``([system], stats_dropped=0)``.
        - Only recent (no middle to drop): returns the input
          unchanged (compaction is a no-op).
        - ``keep_recent_n < 1``: clamped to 1 (a compaction that
          drops the entire conversation is a hard reset, which is
          exactly what we are NOT doing).
    """
    if keep_recent_n < 1:
        keep_recent_n = 1

    system_msg: tuple[str, str] | None = None
    non_system: list[tuple[str, str]] = []
    for m in messages:
        if m[0] == SYSTEM_ROLE and system_msg is None:
            system_msg = m
        else:
            # If multiple system messages are present (unusual but
            # defensive), keep only the first; subsequent ones are
            # folded into the "non-system" tail and may be dropped.
            if m[0] == SYSTEM_ROLE and system_msg is not None:
                non_system.append(m)
            else:
                non_system.append(m)

    if not messages:
        return [], CompactionStats(
            original_count=0,
            compacted_count=0,
            dropped_count=0,
            system_preserved=False,
            recent_window_size=0,
        )

    recent_window = non_system[-keep_recent_n:] if non_system else []
    recent_window_size = len(recent_window)
    output: list[tuple[str, str]] = []
    if system_msg is not None:
        output.append(system_msg)
    output.extend(recent_window)

    original_count = len(messages)
    compacted_count = len(output)
    return output, CompactionStats(
        original_count=original_count,
        compacted_count=compacted_count,
        dropped_count=original_count - compacted_count,
        system_preserved=system_msg is not None,
        recent_window_size=recent_window_size,
    )
