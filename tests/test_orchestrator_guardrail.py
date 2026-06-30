"""Orchestrator-only guardrail injection (platform/concierge ONLY).

Durable runtime half of the two-layer fix that keeps the platform/concierge
agent an ORCHESTRATOR: it must never self-execute substantive work — it
delegates to an existing agent/workspace, or asks the user to create one when
none fits. The other half is the platform-agent template's system-prompt.md
persona; injecting here too means the guardrail holds even when a concierge
boots a STALE template that lacks it.

Root fix for two incidents:
  1. the concierge self-adopting a never-ending PR-review mission that
     duplicates the dedicated review agent (codex-reviewer);
  2. the earlier autonomous self-wake loop.

Critical scoping invariant: a NORMAL WORKER workspace must NOT be gagged — it
still has to do real work. So the guardrail is injected only when
platform_guardrail=True (driven at the boot call site by
platform_agent_identity.mcp_server_present()).
"""
import os
import tempfile

from molecule_runtime.prompt import ORCHESTRATOR_ONLY_GUARDRAIL, build_system_prompt


def _cfg(**files):
    d = tempfile.mkdtemp()
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as fh:
            fh.write(content)
    return d


def test_guardrail_constant_carries_full_intent():
    g = ORCHESTRATOR_ONLY_GUARDRAIL
    assert "never do the work yourself" in g.lower()
    assert "no coding, no pr reviews" in g.lower()
    assert "delegate" in g.lower()
    assert "ask the user to create" in g.lower()
    assert "open-ended or standing mission" in g.lower()
    assert "done-condition" in g.lower()
    # PR review is owned by the dedicated review agent — never self-run.
    assert "pr review is not yours" in g.lower()
    assert "review pass" in g.lower()
    assert "dedicated" in g.lower() and "review agent" in g.lower()
    # No self-wake into work.
    assert "self-wake" in g.lower()
    assert "never the worker" in g.lower()


def test_platform_agent_gets_guardrail_injected():
    d = _cfg(**{"system-prompt.md": "ROLE: org concierge"})
    out = build_system_prompt(
        config_path=d, workspace_id="w", loaded_skills=[], peers=[],
        platform_guardrail=True,
    )
    assert "never do the work yourself" in out.lower(), \
        "platform/concierge prompt missing the orchestrator-only guardrail"
    assert "pr review is not yours" in out.lower(), \
        "platform/concierge prompt missing the PR-review-not-yours clause"
    assert "ask the user to create" in out.lower()
    assert "self-wake" in out.lower()
    # The role doc still loads alongside the guardrail.
    assert "ROLE: org concierge" in out


def test_worker_is_NOT_gagged():
    """A normal worker workspace must keep doing real work — never inject the
    guardrail. This is the scoping invariant that makes the fix safe."""
    d = _cfg(**{"system-prompt.md": "ROLE: a coding worker that writes code"})
    out = build_system_prompt(
        config_path=d, workspace_id="w", loaded_skills=[], peers=[],
        platform_guardrail=False,
    )
    assert "never do the work yourself" not in out.lower(), \
        "worker workspace was wrongly gagged with the orchestrator-only guardrail"
    assert "pr review is not yours" not in out.lower()
    assert "ORCHESTRATOR_ONLY" not in out
    # The worker's own role is intact.
    assert "a coding worker that writes code" in out


def test_default_is_worker_safe():
    """Omitting platform_guardrail must default to the worker (no guardrail) so
    the gate fails safe — only an explicit True (a proven platform agent) injects."""
    d = _cfg(**{"system-prompt.md": "ROLE: worker"})
    out = build_system_prompt(
        config_path=d, workspace_id="w", loaded_skills=[], peers=[],
    )
    assert "never do the work yourself" not in out.lower()
