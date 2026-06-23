"""Context-budget awareness for runtime#133.

The runtime-side scaffolding for "compact-and-continue" behavior on
context overflow. This module provides the **detection** and
**decision** layer (smallest scope first) — the actual compaction
algorithm (steps 2-3 of the runtime#133 spec) is intentionally
deferred to a follow-up ticket. A future workspace-agent ticket will
consume the signal emitted here and decide what to compact; the
runtime's job is to notice the budget pressure exists, in a way that
is testable in isolation.

The runtime#133 spec (workspace-runtime#133) lists four steps:

  1. Detect proactively (token budget watermark) BEFORE the hard 400.
  2. Compact the conversation: preserve task/goal/decisions/blockers/
     key facts; drop or summarize verbose tool outputs.
  3. Continue on the compacted context.
  4. Surface a brief notice ("context compacted — continuing")
     instead of silent amnesia.

This module implements (1) detection + (4) the runtime-side
structured log that signals "context budget warning" — a deterministic,
testable building block. The actual compaction logic (2, 3) lives in
the workspace agent (core) where the conversation state is owned; the
runtime here is A2A orchestration and does not own the conversation.

Design notes (smallest-scope-first, deliberately minimal):

- ``should_compact_context`` is a pure function over four primitives
  (input_tokens, context_window, threshold_pct, headroom_tokens). Easy
  to unit-test, easy to reason about, easy to compose with whatever the
  future workspace-agent side decides to do.
- ``get_model_context_window`` is a small SSOT for per-model context
  windows. The runtime does not currently have one — the workspace
  agent (core) likely has its own, but the runtime here needs at
  least a best-effort lookup to make the budget check meaningful. This
  module ships a conservative initial set: the models the runtime
  actually invokes (Kimi 256K, Anthropic 200K, OpenAI GPT-4o 128K,
  etc.). Unknown models fall back to a configurable default; the
  default itself is conservative (smaller-window) so we over-trigger
  the warning rather than miss one.
- The hook in ``a2a_executor.py`` emits a structured log line (NOT an
  A2A status event yet — the workspace-agent ticket will own the
  user-visible notice). The log carries enough fields for the future
  compaction step to filter on (model, input_tokens, threshold, pct).

Why heuristic-threshold and not LLM-driven: the spec proposes
"85% of the model's context" as the watermark, and heuristic
threshold-vs-watermark is a deterministic primitive that can be
tested in isolation. The "what to drop" question (the actual
compaction algorithm) is the design decision the workspace-agent
ticket will own; this module does not pre-judge it.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Per-model context-window SSOT. Conservative initial set: the models
# the runtime actually invokes today. Unknown model -> DEFAULT
# (smaller of the well-known windows) so we over-trigger rather than
# miss. Once the workspace-agent (core) has its own per-model SSOT,
# we will defer to it; for now this is the runtime's best-effort
# answer to "how big is this model's context?" — better than hard-
# coding a single global default that is wrong for every other model.
#
# Sources (2026-06-23):
#   - Kimi/Moonshot kimi-for-coding: 256K (moonshot docs)
#   - Anthropic Claude (haiku/sonnet/opus 4.5): 200K
#   - OpenAI GPT-4o / GPT-4-turbo: 128K
#   - Google Gemini 2.5 Pro: ~2M (1M input + 1M output, conservative 1M)
#   - Groq Llama 3.3 70B: 128K
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Kimi / Moonshot
    "kimi-for-coding": 256_000,
    "moonshot": 256_000,
    # Anthropic
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    # Google
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    # Groq
    "llama-3.3-70b-versatile": 128_000,
}

# Conservative fallback when a model is not in the SSOT. We use 128K
# (the OpenAI GPT-4o size) rather than 200K (Anthropic) so an
# unknown Anthropic-class model will trigger the warning earlier
# than strictly necessary — over-warning is the safer default for a
# runtime that is asked to surface budget pressure to the agent.
DEFAULT_CONTEXT_WINDOW = 128_000

# Default watermark per the runtime#133 spec: "85% of the model's
# context". Exposed as a module constant (not a parameter) because
# the spec fixes this number; if it ever needs to change, change it
# here and the unit tests catch unintended drift.
DEFAULT_COMPACT_THRESHOLD_PCT = 0.85

# Minimum headroom: if the model's window is so small that the
# threshold is within `MIN_HEADROOM_TOKENS` of zero, skip the
# warning (the agent's already at the wall — emitting a "you're
# approaching the limit" notice at 0 tokens of headroom is noise).
# This guards the (hypothetical) tiny-window edge case (e.g. a
# 4K-context test stub) where threshold * window < MIN_HEADROOM.
MIN_HEADROOM_TOKENS = 256


def get_model_context_window(model: Optional[str]) -> int:
    """Return the per-model context window in tokens, with a conservative
    fallback for unknown models.

    Strips any ``provider:`` prefix (e.g. ``openai:gpt-4o`` -> ``gpt-4o``)
    so a single canonical lookup table can serve any
    ``model_str.split(":", 1)[1]`` shape the executor uses elsewhere
    (see ``gen_ai_system_from_model`` in builtin_tools/telemetry.py).
    """
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    bare = model.split(":", 1)[1] if ":" in model else model
    return MODEL_CONTEXT_WINDOWS.get(bare, DEFAULT_CONTEXT_WINDOW)


def should_compact_context(
    input_tokens: int,
    context_window: int,
    threshold_pct: float = DEFAULT_COMPACT_THRESHOLD_PCT,
    headroom_tokens: int = MIN_HEADROOM_TOKENS,
) -> bool:
    """Return True if the current input-token count has crossed the
    compact-context watermark for the given model.

    The threshold is "X% of the context window" (per the runtime#133
    spec). Returns False in degenerate cases (non-positive window,
    threshold outside (0, 1]) so the runtime never emits a spurious
    warning off a misconfigured model.

    Args:
        input_tokens: the current conversation's input-token count
            (typically the ``usage.input_tokens`` field of the most
            recent LLM response).
        context_window: the model's context-window size in tokens
            (use :func:`get_model_context_window`).
        threshold_pct: watermark as a fraction in (0, 1). Default
            0.85 per the spec.
        headroom_tokens: minimum headroom (in tokens) below the
            watermark before emitting the warning. Prevents the
            "approaching the limit" notice when the agent is already
            at the wall. Default 256.

    Returns:
        True iff the input has crossed the watermark AND there is
        at least ``headroom_tokens`` of headroom below the watermark
        for the future compaction to act on.
    """
    if context_window <= 0 or not (0.0 < threshold_pct < 1.0):
        return False
    if input_tokens < 0:
        return False
    watermark = int(context_window * threshold_pct)
    return input_tokens >= watermark and (context_window - input_tokens) >= headroom_tokens
