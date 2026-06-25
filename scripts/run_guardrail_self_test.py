#!/usr/bin/env python3
"""Single runnable artifact: prove the de-bake guardrails (G0-G6) catch their bug.

Task #80. The user asked "how do we test that the guardrails actually work?" — run
this. It executes the guardrail SELF-TEST: for each guardrail it (1) confirms the
guardrail passes on pristine code, then (2) injects that guardrail's KNOWN
regression and asserts the guardrail goes RED, then reverts. If every self-test
case passes, every guardrail demonstrably fails on its own bug.

This drives the runtime half (G0 fallback, G1 channel, G2 base-frame, G5 openclaw
fail-closed, G6 renderer-completeness) via tests/test_guardrail_self_test.py. The
core half (G0 subst/probe/default-config/allowlist) is the Go meta-test
``go test -run TestGuardrailSelfTest ./internal/handlers/`` in molecule-core; this
script prints the command to run it so the full proof is one obvious sequence.

Usage:
    python3 scripts/run_guardrail_self_test.py
Exit code 0 iff every runtime guardrail self-test case is GREEN (i.e. every
guardrail went RED on its injected regression as required).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_TEST = "tests/test_guardrail_self_test.py"

# The runtime guardrails proved by the self-test, for the human-readable report.
GUARDRAILS = {
    "test_selftest_g0_fallback_filename": "G0  filename SSOT (system-prompt.md fallback)",
    "test_selftest_g1_channel_vs_file_reread": "G1  single prompt-delivery channel (config.system_prompt)",
    "test_selftest_g2_base_frame_always_present": "G2  base platform frame always present",
    "test_selftest_g5_openclaw_not_claude_fallback": "G5  openclaw fail-closed (not claude fallback)",
    "test_selftest_g6_renderer_completeness": "G6  renderer-completeness for kind=platform allowlist",
}


def main() -> int:
    print("=" * 70)
    print("GUARDRAIL SELF-TEST — proving each guardrail goes RED on its regression")
    print("=" * 70)
    cmd = [
        sys.executable, "-m", "pytest", "-v", SELF_TEST,
        # one line per case so the report maps test -> guardrail.
        "-p", "no:cacheprovider",
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    print()
    print("Runtime guardrails exercised by this self-test:")
    for _, label in GUARDRAILS.items():
        print(f"  - {label}")
    print()
    if proc.returncode == 0:
        print("RESULT: GREEN — every runtime guardrail above FAILED on its injected")
        print("        regression (and passed on pristine code). The guardrails work.")
        print()
        print("Core half (G0 subst/probe/default-config/allowlist) — run in molecule-core:")
        print("  cd workspace-server && go test -run TestGuardrailSelfTest ./internal/handlers/")
    else:
        print("RESULT: RED — a guardrail did NOT fail on its regression (or broke on")
        print("        pristine code). A guardrail that can't catch its bug is broken.")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
