"""Durable-memory persistence: cross-framework snapshot auto-load (leg 3) +
the persistence-discipline injection in the HMA section (leg 2).

These guard the platform-level fix for "SaaS agents lose skills/settings/memory
across context resets" (RCA #2831): a fresh/reset session re-injects the durable
memory files via the system prompt, and every agent is instructed to write
durable facts (and secrets via set_secret) instead of trusting ephemeral context.
"""
import os
import tempfile

from molecule_runtime.prompt import DEFAULT_MEMORY_SNAPSHOT_FILES, build_system_prompt
from molecule_runtime.executor_helpers import get_hma_instructions


def test_snapshot_list_is_cross_framework_not_claude_only():
    # Platform-agnostic canonical store + each framework's native convention.
    for f in ("MEMORY.md", "USER.md", "CLAUDE.md", "AGENTS.md"):
        assert f in DEFAULT_MEMORY_SNAPSHOT_FILES, f"missing {f}"


def _cfg(**files):
    d = tempfile.mkdtemp()
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as fh:
            fh.write(content)
    return d


def test_build_system_prompt_loads_present_snapshot_files(monkeypatch):
    # Legacy (kernel opt-out) contract: snapshots load from config_path. The
    # kernel-on read path is pinned in test_memory_write_path_mailbox.py.
    monkeypatch.setenv("MOLECULE_MAILBOX_KERNEL", "0")
    d = _cfg(**{
        "system-prompt.md": "BASE",
        "CLAUDE.md": "DURABLE-CLAUDE: commit as jerry@jrsautocustoms.com",
        "MEMORY.md": "DURABLE-MEM: token lives in set_secret VERCEL_TOKEN",
    })
    out = build_system_prompt(config_path=d, workspace_id="w", loaded_skills=[], peers=[])
    assert "DURABLE-CLAUDE" in out, "CLAUDE.md durable memory not auto-injected"
    assert "DURABLE-MEM" in out, "MEMORY.md durable memory not auto-injected"


def test_absent_snapshot_files_are_not_loaded():
    d = _cfg(**{"system-prompt.md": "BASE"})
    out = build_system_prompt(config_path=d, workspace_id="w", loaded_skills=[], peers=[])
    # No GEMINI.md/AGENTS.md present → no spurious markers.
    assert "GEMINI" not in out
    assert "DURABLE" not in out


def test_snapshot_not_duplicated_when_in_prompt_files():
    # An agent whose framework lists AGENTS.md as a prompt file (openclaw) must
    # not get it loaded twice (once as prompt file, once as snapshot).
    d = _cfg(**{"AGENTS.md": "UNIQUE-AGENTS-BODY"})
    out = build_system_prompt(
        config_path=d, workspace_id="w", loaded_skills=[], peers=[],
        prompt_files=["AGENTS.md"],
    )
    assert out.count("UNIQUE-AGENTS-BODY") == 1, "AGENTS.md injected twice"


def test_hma_instructions_mandate_durable_persistence():
    h = get_hma_instructions()
    # Recall-on-reset promise + the discipline that prevents the "said I
    # remembered but didn't persist" failure.
    assert "PERSISTENCE DISCIPLINE" in h
    assert "commit_memory" in h
    assert "set_secret" in h          # secrets go to the durable store, not .env
    assert "MEMORY.md" in h           # durable file the discipline writes to
    assert "auto-heal" in h or "reset" in h  # context is ephemeral / re-recalled
