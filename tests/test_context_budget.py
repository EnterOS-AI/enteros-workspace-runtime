"""Unit tests for molecule_runtime.context_budget (runtime#133 detection layer).

Smallest-scope-first: tests the pure decision function
``should_compact_context`` + the per-model context-window SSOT
``get_model_context_window``. The actual compaction algorithm lives
in a follow-up workspace-agent ticket; this module is the runtime-
side detection scaffolding (step 1 of the runtime#133 spec) plus a
structured-log signal (step 4's runtime contribution).

The tests are intentionally narrow: they pin the threshold semantics
so a future change to the watermark %, the headroom rule, or the
per-model SSOT must update these tests in lock-step. The
runtime#133 spec fixes the 85% watermark and the 256-token headroom;
deviating from either without updating the spec is a regression.
"""
from __future__ import annotations

import pytest

from molecule_runtime.context_budget import (
    DEFAULT_COMPACT_THRESHOLD_PCT,
    DEFAULT_CONTEXT_WINDOW,
    MIN_HEADROOM_TOKENS,
    MODEL_CONTEXT_WINDOWS,
    get_model_context_window,
    should_compact_context,
    should_emit_budget_warning,
)


class TestGetModelContextWindow:
    """Pin the per-model SSOT and the conservative-fallback rule."""

    def test_known_models_have_their_documented_window(self) -> None:
        # Pin a representative slice so a future SSOT edit must update
        # this test in lock-step. The runtime#133 spec does not require
        # us to ship a per-model SSOT (the workspace agent likely owns
        # the canonical one); these numbers are the runtime's best-
        # effort initial set, and the test acts as a tripwire if
        # someone quietly changes them.
        cases = {
            "kimi-for-coding": 256_000,
            "moonshot": 256_000,
            "claude-haiku-4-5": 200_000,
            "claude-sonnet-4-5": 200_000,
            "gpt-4o": 128_000,
            "gemini-2.5-pro": 1_000_000,
            "llama-3.3-70b-versatile": 128_000,
        }
        for model, expected in cases.items():
            assert MODEL_CONTEXT_WINDOWS[model] == expected, (
                f"per-model SSOT changed for {model!r}: got "
                f"{MODEL_CONTEXT_WINDOWS[model]}, expected {expected}. "
                f"If this is intentional, update this test in lock-step "
                f"and the runtime#133 spec's per-model section."
            )

    def test_provider_prefix_is_stripped(self) -> None:
        # The executor's gen_ai_system_from_model strips
        # ``provider:`` prefixes; this function must do the same so
        # the SSOT lookup hits a single canonical key. If this
        # contract changes, update both call sites + this test.
        assert get_model_context_window("openai:gpt-4o") == 128_000
        assert get_model_context_window("anthropic:claude-haiku-4-5") == 200_000
        assert get_model_context_window("kimi:kimi-for-coding") == 256_000

    def test_unknown_model_falls_back_to_conservative_default(self) -> None:
        # The default is 128K (OpenAI GPT-4o size) — smaller than
        # Anthropic 200K so an unknown Anthropic-class model
        # triggers the warning EARLIER than strictly necessary.
        # Over-warning is the safer default for a runtime that is
        # asked to surface budget pressure. If the default changes,
        # this test fails and forces a deliberate review.
        assert get_model_context_window("totally-unknown-model-9000") == DEFAULT_CONTEXT_WINDOW
        assert get_model_context_window("totally-unknown-model-9000") == 128_000

    def test_empty_or_none_model_returns_default(self) -> None:
        # Defensive: never let a missing model name crash the
        # budget check (the executor may be in a startup state where
        # the model isn't resolved yet).
        assert get_model_context_window("") == DEFAULT_CONTEXT_WINDOW
        assert get_model_context_window(None) == DEFAULT_CONTEXT_WINDOW  # type: ignore[arg-type]


class TestShouldCompactContext:
    """Pin the COMPACTION decision semantics (CR2 RC 13423 fix).

    Post-fix semantics: ``should_compact_context`` is the URGENT
    COMPACTION check. When the previous turn crossed the watermark
    (input_tokens >= context_window * threshold_pct), the NEXT turn
    is at imminent overflow risk and must be compacted — regardless
    of how close to the wall we already are. The previous version
    had a ``headroom_tokens`` floor (default 256) that suppressed
    this case, which was the CR2 RC 13423 correctness bug: a
    near-wall previous turn returned False (no compaction) when
    compaction is most needed. The headroom floor moved to the
    WARNING-emission helper
    (:func:`should_emit_budget_warning`) — the warning is
    suppressable at the wall (it's noise), but compaction is not.
    """

    def test_default_watermark_is_85pct_per_spec(self) -> None:
        # The runtime#133 spec fixes the watermark at 85% of the
        # model's context. If you change this number, also update
        # the spec — the test pins the contract.
        assert DEFAULT_COMPACT_THRESHOLD_PCT == 0.85

    def test_below_watermark_does_not_trigger(self) -> None:
        # 84% of 200K = 168_000, but use 167_999 to clearly under
        # the boundary. Must NOT trigger.
        assert should_compact_context(
            input_tokens=167_999, context_window=200_000
        ) is False

    def test_at_watermark_triggers(self) -> None:
        # Exactly 85% of 200K = 170_000. Must trigger (we are AT the
        # watermark, not below it).
        assert should_compact_context(
            input_tokens=170_000, context_window=200_000
        ) is True

    def test_above_watermark_triggers(self) -> None:
        # 90% of 200K = 180_000. Well past the watermark — must
        # trigger.
        assert should_compact_context(
            input_tokens=180_000, context_window=200_000
        ) is True

    def test_at_wall_triggers(self) -> None:
        # CR2 RC 13423 regression guard: at the model wall
        # (input_tokens == context_window, 0 headroom), the
        # COMPACTION decision MUST be True. The previous version
        # suppressed this case via the headroom_tokens floor and
        # returned False here — exactly when the next turn WILL
        # overflow and compaction is most needed. The headroom
        # floor was moved to should_emit_budget_warning (the
        # warning is suppressable at the wall; compaction is not).
        assert should_compact_context(
            input_tokens=200_000, context_window=200_000
        ) is True

    def test_just_below_wall_triggers(self) -> None:
        # Same regression guard as test_at_wall_triggers, with
        # 1 token of headroom. Pre-fix: returned False. Post-fix:
        # True (compaction is urgent).
        assert should_compact_context(
            input_tokens=199_999, context_window=200_000
        ) is True

    def test_invalid_threshold_is_fail_closed(self) -> None:
        # A bad threshold (0, 1, negative, > 1) means the operator
        # has misconfigured the model/runtime. The detection
        # function returns False rather than triggering on garbage
        # — over-triggering on a misconfig would be a noisy
        # regression. Real configs use DEFAULT_COMPACT_THRESHOLD_PCT.
        for bad in (0.0, -0.1, 1.0, 1.1, 2.0):
            assert should_compact_context(
                input_tokens=180_000, context_window=200_000, threshold_pct=bad
            ) is False

    def test_invalid_window_is_fail_closed(self) -> None:
        # Zero or negative context window is nonsense; do not
        # trigger. (Test the executor's path where context_window
        # might be 0 for a stub model.)
        for bad in (0, -1, -100_000):
            assert should_compact_context(
                input_tokens=180_000, context_window=bad
            ) is False

    def test_negative_input_is_fail_closed(self) -> None:
        # A bad token count from a misbehaving telemetry source
        # should not trigger the warning. Real telemetry is
        # non-negative; negative is a sign of a metadata-parsing
        # bug, not a real budget pressure event.
        assert should_compact_context(
            input_tokens=-1, context_window=200_000
        ) is False

    def test_custom_threshold_pct(self) -> None:
        # The threshold is parameterized so future per-model
        # overrides (e.g. 70% for cheaper models) work without
        # touching the function. Pin that contract.
        # 70% of 200K = 140_000.
        assert should_compact_context(
            input_tokens=139_999, context_window=200_000, threshold_pct=0.70
        ) is False
        assert should_compact_context(
            input_tokens=140_000, context_window=200_000, threshold_pct=0.70
        ) is True


class TestShouldEmitBudgetWarning:
    """Pin the WARNING-emission semantics (CR2 RC 13423 fix).

    The warning IS suppressable at the wall: when the agent is
    already at the model wall, emitting "you're approaching the
    limit" is noise (the next call WILL overflow regardless, and
    compaction has already fired from should_compact_context). This
    helper keeps the headroom_tokens floor that the prior combined
    function had, so the warning contract stays: "you have room to
    compact, do it now" — never "you have zero room, sorry."
    """

    def test_below_watermark_does_not_warn(self) -> None:
        assert should_emit_budget_warning(
            input_tokens=167_999, context_window=200_000
        ) is False

    def test_above_watermark_with_headroom_warns(self) -> None:
        # 90% of 200K = 180_000, 20K headroom — well above the 256
        # floor. Must warn.
        assert should_emit_budget_warning(
            input_tokens=180_000, context_window=200_000
        ) is True

    def test_at_wall_does_not_warn(self) -> None:
        # At the wall (0 headroom), the warning is suppressed —
        # compaction has already fired, and "you're approaching the
        # limit" is just noise.
        assert should_emit_budget_warning(
            input_tokens=200_000, context_window=200_000
        ) is False

    def test_just_below_wall_does_not_warn(self) -> None:
        # 1 token of headroom is below the 256-token floor — must
        # NOT warn.
        assert should_emit_budget_warning(
            input_tokens=199_999, context_window=200_000
        ) is False

    def test_just_above_min_headroom_warns(self) -> None:
        # 257 tokens of headroom — just above the 256 floor. Must
        # warn.
        assert should_emit_budget_warning(
            input_tokens=199_743, context_window=200_000
        ) is True

    def test_min_headroom_constant_pinned(self) -> None:
        # The 256-token headroom floor is a deliberate choice
        # (small enough to be a real budget pressure, large enough
        # to be a meaningful "you have room to compact" notice).
        # If you change this, update TestShouldCompactContext
        # fixtures that depend on the threshold-vs-headroom gap.
        assert MIN_HEADROOM_TOKENS == 256
