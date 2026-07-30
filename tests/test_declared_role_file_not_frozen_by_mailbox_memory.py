"""A DECLARED prompt_files entry must never be frozen by the durable mailbox copy.

Context — why this exists (cp#672 / molecule-controlplane #2778):
``/workspace`` is becoming DURABLE BY DEFAULT on the Enter OS Server volume.
Once it is, ``/workspace/.molecule/memory/<name>`` survives every restart, and
it is written **skip-if-exists** (``mailbox_dir._copy_0600``, ``dst.exists() ->
return``) with the reconcile arm deliberately skipping the memory dir
(``mailbox_dir`` reconcile: ``if dst.parent.name == "memory": continue``).

``build_system_prompt`` used to REDIRECT any ``prompt_files`` entry whose
basename is in ``DEFAULT_MEMORY_SNAPSHOT_FILES`` to that never-refreshed mailbox
copy. The shipped openclaw template declares ``SOUL.md`` / ``AGENTS.md`` /
``USER.md`` in ``prompt_files``, so a durable ``/workspace`` PINNED an openclaw
workspace's persona to its FIRST-BOOT content forever: no re-provision, no
template change, no param re-render could ever land again.

THE MECHANISM (rewritten after review — the first cut deduped by resolved PATH,
which cannot see that the two paths hold the SAME BYTES, so it injected every
declared persona TWICE and left the frozen v1 permanently trailing the fresh
v2). A declared memory basename occupies two slots that the kernel migration
welded together, and they are separated by asking which bytes a WRITER put in
the durable copy — see ``prompt._evolved_memory_residue``:

* **DECLARED** (named in ``prompt_files``) -> a provisioner-authored ROLE file.
  ``/configs`` is authoritative for that slot, is re-rendered every provision,
  and is the ONLY copy injected when the mailbox copy is just a snapshot of it.
* **Writer-produced content** in the mailbox copy of a declared basename (the
  tail an ``append_to_memory`` / consolidation / plugin write added, or a full
  rewrite) is still injected, layered after the role. Nothing a writer wrote is
  ever dropped.
* **AUTO-LOADED** (a memory basename NOT named in ``prompt_files``) -> genuine
  durable memory. The mailbox copy still wins, completely unchanged.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime.prompt import build_system_prompt  # noqa: E402

V1 = "SOUL-V1-FIRST-BOOT-PERSONA-MUST-NOT-WIN"
V2 = "SOUL-V2-REDELIVERED-PERSONA-MUST-LAND"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.delenv(mailbox_dir.MAILBOX_DIR_ENV, raising=False)
    yield


def _kernel_on(monkeypatch, tmp_path):
    base = tmp_path / "workspace" / ".molecule"
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(base))
    mem = base / "memory"
    mem.mkdir(parents=True)
    return mem


def _record_seed(mem: Path, name: str, seed_bytes: bytes) -> None:
    """Write the provenance the migrator writes when it seeds <mailbox>/memory/<name>
    from the param-rendered /configs root copy."""
    path = mem / mailbox_dir._SEED_MANIFEST_NAME
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data[name] = {"sha256": hashlib.sha256(seed_bytes).hexdigest(), "size": len(seed_bytes)}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_declared_role_file_is_not_frozen_by_stale_mailbox_copy(tmp_path, monkeypatch):
    """THE persona-freeze test (openclaw shape).

    ``SOUL.md`` is DECLARED in prompt_files. ``/configs`` has the freshly
    re-rendered v2 persona; the durable mailbox memory dir still holds the
    first-boot v1. The v2 role file MUST land — that is the whole point of
    re-provisioning — and v1 must not be anywhere in the prompt: a REVOKED
    instruction that permanently trails the live one is not a fix (B-R2).

    ``SOUL.md`` has no writer at all (``mailbox_dir.ACCUMULATING_MEMORY_BASENAMES``
    is the inventory), so the mailbox copy can only be the first-boot snapshot.
    """
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "SOUL.md").write_text(V2, encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "SOUL.md").write_text(V1, encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-openclaw",
        loaded_skills=[],
        peers=[],
        prompt_files=["SOUL.md"],
        a2a_mcp=False,
    )

    assert V2 in out, (
        "REDELIVERED /configs/SOUL.md did not land — a DECLARED prompt_files entry "
        "was resolved through the never-refreshed durable mailbox copy, so the "
        "persona is frozen at first boot and no re-provision can ever change it"
    )
    assert V1 not in out, (
        "the FROZEN first-boot persona is still injected. A persona change that "
        "REVOKES an instruction never takes effect if the revoked text keeps "
        "trailing the fresh one — SOUL.md has no writer, so the mailbox copy is "
        "by construction a snapshot of the role file, not memory"
    )


def test_stale_seeded_snapshot_of_an_older_role_version_is_dropped(tmp_path, monkeypatch):
    """Same freeze, closed by recorded PROVENANCE rather than by the writer
    inventory — this is the leg that protects a basename that DOES have a
    writer (here ``CLAUDE.md``) from its own first-boot snapshot."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "CLAUDE.md").write_text(V2, encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "CLAUDE.md").write_text(V1, encoding="utf-8")
    _record_seed(mem, "CLAUDE.md", V1.encode("utf-8"))  # seeded from /configs v1

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-seeded",
        loaded_skills=[],
        peers=[],
        prompt_files=["CLAUDE.md"],
        a2a_mcp=False,
    )
    assert V2 in out
    assert V1 not in out, (
        "the mailbox copy is byte-for-byte the recorded first-boot seed of role "
        "file v1 — no writer ever touched it, so it is a stale snapshot and must "
        "not be injected next to v2"
    )


def test_identical_mailbox_snapshot_is_never_injected_twice(tmp_path, monkeypatch):
    """B-R1, the normal case: the migrator seeds <mailbox>/memory/<name> as a
    byte COPY of /configs/<name>, so on an unchanged template the two paths hold
    the SAME BYTES. Exactly one copy may reach the prompt."""
    configs = tmp_path / "configs"
    configs.mkdir()
    persona = "PERSONA-BODY-INJECT-ME-EXACTLY-ONCE"
    (configs / "SOUL.md").write_text(persona, encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "SOUL.md").write_text(persona, encoding="utf-8")  # the seeded copy

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-dup",
        loaded_skills=[],
        peers=[],
        prompt_files=["SOUL.md"],
        a2a_mcp=False,
    )
    assert out.count(persona) == 1, (
        "the durable mailbox copy is a byte-identical SNAPSHOT of the declared "
        "role file, not a second document — injecting both duplicates the entire "
        "persona in every prompt, on every turn"
    )


def test_declared_role_file_still_carries_evolved_mailbox_memory(tmp_path, monkeypatch):
    """Guard against OVER-correcting: content a WRITER appended to the durable
    mailbox copy of a DECLARED basename must still reach the prompt
    (``append_to_memory`` appends to exactly this file), and the baseline it was
    appended to must not be duplicated."""
    configs = tmp_path / "configs"
    configs.mkdir()
    baseline = "USER-ROLE-TEMPLATE-V2"
    (configs / "USER.md").write_text(baseline, encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    # What the disk really looks like after append_to_memory ran once: the
    # seeded baseline, plus the appended block.
    (mem / "USER.md").write_text(baseline + "\n\nEVOLVED-USER-FACT-KEEP-ME", encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-evolved",
        loaded_skills=[],
        peers=[],
        prompt_files=["USER.md"],
        a2a_mcp=False,
    )
    assert baseline in out, "re-rendered declared role file must land"
    assert "EVOLVED-USER-FACT-KEEP-ME" in out, (
        "evolved durable memory for a DECLARED basename was dropped — the fix "
        "over-corrected and clobbered the agent's accumulated memory"
    )
    assert out.count(baseline) == 1, "the baseline the writer appended to must not be duplicated"


def test_evolved_memory_survives_a_role_file_re_render(tmp_path, monkeypatch):
    """The hard case both blockers meet in: the agent evolved the mailbox copy
    on top of role file v1 AND the template has since been re-rendered to v2.
    v2 lands, the evolved tail survives, the frozen v1 baseline is gone."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "CLAUDE.md").write_text(V2, encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "CLAUDE.md").write_text(V1 + "\n\nEVOLVED-FACT-SURVIVES", encoding="utf-8")
    _record_seed(mem, "CLAUDE.md", V1.encode("utf-8"))

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-evolved-rerender",
        loaded_skills=[],
        peers=[],
        prompt_files=["CLAUDE.md"],
        a2a_mcp=False,
    )
    assert V2 in out, "the re-rendered role file must land"
    assert "EVOLVED-FACT-SURVIVES" in out, "writer-appended memory must never be dropped"
    assert V1 not in out, "only the recorded SEED prefix is subtracted — and it must be"


def test_writer_rewritten_mailbox_copy_is_kept_whole(tmp_path, monkeypatch):
    """``agents_md.generate_agents_md`` force-WRITES <mailbox>/memory/AGENTS.md
    every boot, so its content shares no prefix with the seed. Diverged from a
    recorded seed == a writer produced it: keep all of it."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "AGENTS.md").write_text("AGENTS-STUB-CLEARED-BY-PERSONA", encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "AGENTS.md").write_text("AAIF-DISCOVERY-CARD-WRITTEN-BY-agents_md", encoding="utf-8")
    _record_seed(mem, "AGENTS.md", b"AGENTS-STUB-CLEARED-BY-PERSONA")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-rewritten",
        loaded_skills=[],
        peers=[],
        prompt_files=["AGENTS.md"],
        a2a_mcp=False,
    )
    assert "AAIF-DISCOVERY-CARD-WRITTEN-BY-agents_md" in out, (
        "a mailbox copy that diverged from its recorded seed was written by a "
        "writer and must be kept in full"
    )


def test_undeclared_memory_snapshot_still_resolves_to_mailbox_copy(tmp_path, monkeypatch):
    """The AUTO-LOAD leg is UNCHANGED: a memory basename that is NOT declared in
    prompt_files still reads the durable mailbox copy, and a stale /configs copy
    must never shadow it."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")
    (configs / "MEMORY.md").write_text("STALE-CONFIGS-MEMORY-MUST-NOT-SHOW", encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "MEMORY.md").write_text("FRESH-MAILBOX-MEMORY-SHOW-THIS", encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-undeclared",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md"],  # MEMORY.md NOT declared
        a2a_mcp=False,
    )
    assert "BASE-ROLE" in out
    assert "FRESH-MAILBOX-MEMORY-SHOW-THIS" in out, "durable memory must still be auto-loaded"
    assert "STALE-CONFIGS-MEMORY-MUST-NOT-SHOW" not in out, (
        "an UNDECLARED memory snapshot must still resolve to the mailbox copy — "
        "a stale /configs copy may never shadow it"
    )


def test_undeclared_snapshot_of_a_no_writer_basename_is_untouched(tmp_path, monkeypatch):
    """The writer inventory is consulted ONLY for a DECLARED basename. An
    UNDECLARED SOUL.md in the mailbox is durable memory and is injected whole,
    seed manifest or not."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "SOUL.md").write_text("UNDECLARED-MAILBOX-SOUL", encoding="utf-8")
    _record_seed(mem, "SOUL.md", b"UNDECLARED-MAILBOX-SOUL")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-undeclared-soul",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md"],  # SOUL.md NOT declared
        a2a_mcp=False,
    )
    assert "UNDECLARED-MAILBOX-SOUL" in out


def test_declared_basename_falls_back_to_mailbox_when_configs_absent(tmp_path, monkeypatch):
    """If /configs has no copy of a declared memory basename, the durable mailbox
    copy is still the fallback — never drop the section entirely."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("BASE-ROLE", encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "SOUL.md").write_text("MAILBOX-ONLY-SOUL", encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-fallback",
        loaded_skills=[],
        peers=[],
        prompt_files=["system-prompt.md", "SOUL.md"],
        a2a_mcp=False,
    )
    assert "MAILBOX-ONLY-SOUL" in out
    assert out.count("MAILBOX-ONLY-SOUL") == 1, "must not be injected twice"


def test_declared_basename_with_empty_configs_copy_keeps_mailbox_content(tmp_path, monkeypatch):
    """An EMPTY /configs copy occupies no role slot, so there is no snapshot to
    subtract — the durable copy is injected whole, exactly as before."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "SOUL.md").write_text("   \n", encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    (mem / "SOUL.md").write_text("MAILBOX-SOUL-NOT-DROPPED", encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-empty-configs",
        loaded_skills=[],
        peers=[],
        prompt_files=["SOUL.md"],
        a2a_mcp=False,
    )
    assert "MAILBOX-SOUL-NOT-DROPPED" in out


def test_kernel_off_declared_memory_basename_byte_identical(tmp_path, monkeypatch):
    """Kernel OFF: memory_source IS config_path, so a declared memory basename
    loads the /configs copy exactly once, exactly as before."""
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "0")
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "SOUL.md").write_text("KERNEL-OFF-SOUL", encoding="utf-8")

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws-off",
        loaded_skills=[],
        peers=[],
        prompt_files=["SOUL.md"],
        a2a_mcp=False,
    )
    assert out.count("KERNEL-OFF-SOUL") == 1


def test_shipped_openclaw_template_shape_injects_each_file_once(tmp_path, monkeypatch):
    """REGRESSION PIN for the reviewer's measured probe (B-R1).

    Reproduces the shipped openclaw template exactly: ``prompt_files`` =
    SOUL/BOOTSTRAP/AGENTS/HEARTBEAT/TOOLS/USER, with ``<mailbox>/memory`` seeded
    the way ``mailbox_dir._legacy_pairs`` seeds it on the first kernel-on boot
    (a byte copy of each ``/configs`` root copy). Measured on the PR's
    path-keyed dedup: prompt 6190 -> 7020 bytes, persona body 40 -> 80
    occurrences, the whole persona duplicated verbatim. Every declared file must
    appear exactly once.
    """
    import shutil

    configs = tmp_path / "configs"
    configs.mkdir()
    soul = "# SOUL\nYou are the Org Concierge. " + ("PERSONA-BODY-XXXX " * 40)
    (configs / "SOUL.md").write_text(soul, encoding="utf-8")
    (configs / "BOOTSTRAP.md").write_text("# BOOTSTRAP\n(Cleared by persona materialization)", encoding="utf-8")
    (configs / "AGENTS.md").write_text("# AGENTS\n(Cleared by persona materialization)", encoding="utf-8")
    (configs / "HEARTBEAT.md").write_text("# HEARTBEAT\nbeat", encoding="utf-8")
    (configs / "TOOLS.md").write_text("# TOOLS\ntools", encoding="utf-8")
    (configs / "USER.md").write_text("# USER\nuser template baseline", encoding="utf-8")

    mem = _kernel_on(monkeypatch, tmp_path)
    for name in mailbox_dir._LEGACY_MEMORY_BASENAMES:
        src = configs / name
        if src.is_file():
            shutil.copyfile(src, mem / name)
            _record_seed(mem, name, src.read_bytes())

    out = build_system_prompt(
        config_path=str(configs),
        workspace_id="ws",
        loaded_skills=[],
        peers=[],
        prompt_files=["SOUL.md", "BOOTSTRAP.md", "AGENTS.md", "HEARTBEAT.md", "TOOLS.md", "USER.md"],
        a2a_mcp=False,
    )
    assert out.count("PERSONA-BODY-XXXX") == 40, (
        "the openclaw persona is injected TWICE — the mailbox copy is a seeded "
        "snapshot of the same bytes, not a second document"
    )
    assert out.count("# SOUL") == 1
    assert out.count("user template baseline") == 1
    assert out.count("(Cleared by persona materialization)") == 2
