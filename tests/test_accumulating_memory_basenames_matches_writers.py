"""``mailbox_dir.ACCUMULATING_MEMORY_BASENAMES`` must equal the real writer set.

``prompt._evolved_memory_residue`` uses that tuple as its LAST-RESORT evidence:
for a memory basename DECLARED in ``prompt_files`` on a workspace that carries
no seed provenance, a mailbox copy is kept only when some writer could have
produced it, and dropped as a stale first-boot role-file snapshot when none
could. That inference is only sound while the tuple is the actual inventory of
writers into ``<mailbox>/memory``.

So pin it against the source of truth — every ``mailbox_dir.memory_file(...)``
call site in the tree — rather than against a hand-kept list. If someone adds a
writer for ``SOUL.md`` or ``USER.md``, this test fails and forces the tuple (and
the reasoning above it) to be updated in the same change.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
sys.path.insert(0, str(_REPO))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402
from molecule_runtime.adapter_base import BaseAdapter  # noqa: E402
from molecule_runtime.plugins_registry.protocol import DEFAULT_MEMORY_FILENAME  # noqa: E402


def _memory_file_call_sites() -> tuple[set[str], list[str]]:
    """(literal basenames passed to memory_file(), call sites with a dynamic arg)."""
    literals: set[str] = set()
    dynamic: list[str] = []
    for path in sorted((_REPO / "molecule_runtime").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "memory_file" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literals.add(arg.value)
            else:
                dynamic.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    return literals, dynamic


def test_accumulating_set_is_exactly_the_writer_inventory():
    literals, dynamic = _memory_file_call_sites()
    assert literals, "no memory_file('<name>') call site found — the scan is broken"
    # The dynamic call sites (append_to_memory, the plugin memory adaptor) both
    # resolve their filename from BaseAdapter.memory_filename() / a plugin's
    # memory.filename, whose default is DEFAULT_MEMORY_FILENAME.
    assert dynamic, "expected the dynamic append_to_memory / plugin call sites"
    expected = literals | {DEFAULT_MEMORY_FILENAME}
    assert set(mailbox_dir.ACCUMULATING_MEMORY_BASENAMES) == expected, (
        "the writer inventory changed. prompt._evolved_memory_residue drops the "
        "durable copy of a DECLARED memory basename that NO writer can target, "
        "on the grounds that it can only be the first-boot role-file snapshot. "
        f"Writers now target {sorted(expected)}; the tuple says "
        f"{sorted(mailbox_dir.ACCUMULATING_MEMORY_BASENAMES)}."
    )


def test_no_writer_targets_the_role_only_basenames():
    """SOUL.md / USER.md are openclaw ROLE files that merely share a basename
    with the memory-snapshot list. Nothing may write them under <mailbox>/memory."""
    literals, _ = _memory_file_call_sites()
    assert not ({"SOUL.md", "USER.md"} & literals)
    assert BaseAdapter.memory_filename(None) == DEFAULT_MEMORY_FILENAME
    overrides = [
        f"{p.relative_to(_REPO)}"
        for p in sorted(_REPO.glob("tmpl/*/*.py"))
        if "def memory_filename" in p.read_text(encoding="utf-8")
    ]
    assert not overrides, (
        "a template adapter now overrides memory_filename(); if it points at a "
        f"basename outside ACCUMULATING_MEMORY_BASENAMES, update the tuple: {overrides}"
    )


def test_accumulating_set_is_a_subset_of_the_snapshot_files():
    from molecule_runtime.prompt import DEFAULT_MEMORY_SNAPSHOT_FILES

    assert set(mailbox_dir.ACCUMULATING_MEMORY_BASENAMES) <= set(DEFAULT_MEMORY_SNAPSHOT_FILES)
