"""Swap stability: an unchanged rebuild must NOT replace the live tree's inode.

core#5009 — a workspace served STALE plugin code while the files on disk were
byte-identical to the pinned commit.

WHY THE INODE IS THE THING THAT MATTERS
---------------------------------------
`_atomic_swap_dir` promotes a staged build with `os.replace`, i.e. a directory
RENAME. The live path keeps working, but it points at a NEW inode; the previous
directory is unlinked. Any process that already imported modules out of the old
directory keeps running that old code for its entire lifetime, and nothing on
disk shows it — `sha256(live file) == upstream` while the serving process
answers from the replaced tree.

That is not hypothetical. boot-install runs TWICE per boot (the `prepare` pass
that pre-materializes config before the gateway launches, then the real serve),
and hermes spawns the plugin MCP servers BETWEEN those two passes. Measured on
a live tenant:

    22:53:53  gateway starts
    22:53:56  reno-work MCP server spawned from /configs/plugins/...
    22:53:59  second boot-install pass begins swapping
    22:54:01  coordinator tree replaced   <-- under the running server

The second pass rebuilds an IDENTICAL tree, so the swap buys nothing and costs
the running process its module identity. Skipping a no-change swap removes the
orphaning entirely; when content genuinely differs the swap must still happen,
and must say so loudly, because that is the case a human needs to know about
(`mcp-reconcile-watch.sh` deliberately compares the `mcp_servers` mapping
structurally and will NOT restart on a code-only change).

`swapped` semantics are preserved: it answers "is the staged tree now live",
which stays True when we skip a swap precisely because the tree is already live.
"""

from __future__ import annotations

import logging
from pathlib import Path

from molecule_runtime import plugin_sources as ps

from tests.test_plugin_sources import _make_repo, _patch_git

# A manifest the SSOT validator accepts — an invalid one is REJECTED before
# the swap, so every assertion below would pass/fail for the wrong reason.
_MANIFEST = b"name: repo\ndescription: swap-stability fixture\nversion: 0.1.0\n"


def _inode(p: Path) -> int:
    return p.stat().st_ino


def _install(plugins_dir: Path, source: str = "gitea://owner/repo"):
    return ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": source},
    )


def test_identical_rebuild_keeps_the_live_inode(monkeypatch, tmp_path):
    """The core#5009 fix: rebuilding the same content must not re-point the tree.

    NON-VACUITY: revert the no-change check and this fails — `os.replace`
    always yields a fresh inode.
    """
    files = {"plugin.yaml": _MANIFEST, "mcp/server.py": b"print('v1')\n"}
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo(dict(files)))
    plugins_dir = tmp_path / "plugins"

    first = _install(plugins_dir)
    assert first.swapped is True
    live = plugins_dir / "repo"
    before = _inode(live)
    before_file = _inode(live / "mcp" / "server.py")

    second = _install(plugins_dir)

    assert second.swapped is True, (
        "the staged tree IS live, so `swapped` must stay True — callers gate "
        "`live iff declared && swapped` on it"
    )
    assert _inode(live) == before, (
        "the live plugin directory was replaced by an identical rebuild — a "
        "process that imported from it is now orphaned on unlinked files"
    )
    assert _inode(live / "mcp" / "server.py") == before_file, (
        "the served MCP entrypoint was re-pointed with no content change"
    )


def test_identical_rebuild_says_it_skipped(monkeypatch, tmp_path, caplog):
    """Operators must be able to SEE that no swap happened, not infer it."""
    files = {"plugin.yaml": _MANIFEST, "SKILL.md": b"# s\n"}
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo(dict(files)))
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)

    with caplog.at_level(logging.INFO, logger="molecule_runtime.plugin_sources"):
        _install(plugins_dir)

    assert any(
        "live tree already matches" in r.getMessage() for r in caplog.records
    ), f"no skip line logged; got {[r.getMessage() for r in caplog.records]}"


def test_changed_content_still_swaps_and_warns_loudly(monkeypatch, tmp_path, caplog):
    """The dangerous case must still swap — and must be visible.

    A code-only change does not alter the `mcp_servers` mapping, so
    mcp-reconcile-watch.sh will not restart the gateway: an already-running MCP
    server keeps serving the previous code. Silence here is what let core#5009
    reach a customer.
    """
    version = {"v": b"print('v1')\n"}
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo(
            {"plugin.yaml": _MANIFEST, "mcp/server.py": version["v"]}
        ),
    )
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)
    live = plugins_dir / "repo"
    before = _inode(live)

    version["v"] = b"print('v2')\n"  # a real update lands
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = _install(plugins_dir)

    assert report.swapped is True
    assert _inode(live) != before, "a real content change MUST be promoted"
    assert (live / "mcp" / "server.py").read_bytes() == b"print('v2')\n"

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("REPLACED" in m for m in warnings), (
        f"a live-tree replacement must be logged at WARNING; got {warnings}"
    )
    assert any("repo" in m for m in warnings), (
        "the warning must name WHICH plugin changed, or an operator cannot act"
    )


def test_first_install_is_not_reported_as_unchanged(monkeypatch, tmp_path, caplog):
    """No live tree => a genuine promotion, never a 'skipped' claim."""
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo({"plugin.yaml": _MANIFEST}),
    )
    with caplog.at_level(logging.INFO, logger="molecule_runtime.plugin_sources"):
        report = _install(tmp_path / "plugins")
    assert report.swapped is True
    assert not any(
        "live tree already matches" in r.getMessage() for r in caplog.records
    ), "a first install has nothing to compare against and must not claim a skip"


# ---------------------------------------------------------------------------
# Resolved-commit logging (core#5009 / core#5007)
#
# The runtime cloned plugin trees and never recorded WHICH COMMIT it got. That
# is the blind spot behind both issues: when a box serves unexpected behaviour
# there is no way, from the box, to tell what it actually installed — the
# control plane's `installed_sha` is a claim about the control plane, not an
# observation of the box. One line per plugin at install time is the cheapest
# possible fix and needs no protocol change.
# ---------------------------------------------------------------------------
_SHA = "c0ffee1234567890c0ffee1234567890c0ffee12"


def _patch_git_with_head(monkeypatch, files: dict, head: str | None):
    """git fake that also answers `rev-parse HEAD` (or fails, when head=None)."""
    from pathlib import Path as _P

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            if head is None:
                raise ps.subprocess.CalledProcessError(128, cmd, output="", stderr="no HEAD")
            return ps.subprocess.CompletedProcess(cmd, 0, head + "\n", "")
        if "clone" not in cmd:
            return ps.subprocess.CompletedProcess(cmd, 0, "", "")
        dest = _P(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir(exist_ok=True)
        for rel, data in files.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return ps.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ps.subprocess, "run", fake_run)


def test_install_logs_the_resolved_commit_sha(monkeypatch, tmp_path, caplog):
    _patch_git_with_head(monkeypatch, {"plugin.yaml": _MANIFEST}, _SHA)
    with caplog.at_level(logging.INFO, logger="molecule_runtime.plugin_sources"):
        report = _install(tmp_path / "plugins")
    assert report.swapped is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any(_SHA[:12] in m and "repo" in m for m in msgs), (
        f"no line names the plugin and the commit it resolved to; got {msgs}"
    )


def test_unresolvable_head_does_not_break_the_install(monkeypatch, tmp_path, caplog):
    """Logging is observability, never a new way to fail a boot."""
    _patch_git_with_head(monkeypatch, {"plugin.yaml": _MANIFEST}, None)
    with caplog.at_level(logging.INFO, logger="molecule_runtime.plugin_sources"):
        report = _install(tmp_path / "plugins")
    assert report.swapped is True, "a failed rev-parse must not fail the install"
    assert report.installed, "the plugin must still be installed"


def test_replacement_warning_names_the_changed_FILES(monkeypatch, tmp_path, caplog):
    """Naming the plugin is not enough to diagnose a repeating replacement.

    Deployed to production, the plugin-level warning fired on every boot for one
    plugin and left the obvious question unanswered: WHICH file keeps differing?
    Without that, the next step is guesswork (bytecode? a delivery marker? a
    carry-forward path?) — each of which costs a full release cycle to test.
    The changed paths are already computed to decide the swap, so logging them
    is free.
    """
    body = {"v": b"print('v1')\n"}
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo({
            "plugin.yaml": _MANIFEST,
            "mcp/server.py": body["v"],
            "stable.txt": b"unchanged\n",
        }),
    )
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)

    body["v"] = b"print('v2')\n"
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        _install(plugins_dir)

    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    joined = "\n".join(warns)
    assert "repo/mcp/server.py" in joined, (
        f"the warning must name the file that changed; got {warns}"
    )
    assert "stable.txt" not in joined, (
        "only CHANGED files may be listed — dumping the whole tree buries the signal"
    )


# ---------------------------------------------------------------------------
# Generated bytecode must not count as tree content (core#5009, the root cause)
#
# Measured on the customer workspace once the file-level warning shipped:
#
#   01:19:53  MCP server spawns, imports renowork.* -> Python writes
#             __pycache__/*.pyc INTO the live tree
#   01:19:58  serve pass compares live (28 .pyc) vs a fresh clone (none)
#             -> "changed" -> SWAP -> the tree is replaced under the server
#                that just created those .pyc
#
# The running server's own bytecode is what makes the tree look changed, which
# triggers the swap that orphans it. Self-sustaining, so it recurred on EVERY
# boot. It also hid itself: the .pyc exist only between spawn and swap, so
# anyone inspecting afterwards — the customer and me both — finds none and
# wrongly rules bytecode out.
# ---------------------------------------------------------------------------
def test_generated_bytecode_does_not_trigger_a_swap(monkeypatch, tmp_path, caplog):
    files = {"plugin.yaml": _MANIFEST, "mcp/server.py": b"print('v1')\n"}
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo(dict(files)))
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)
    live = plugins_dir / "repo"
    before = _inode(live)

    # Exactly what the spawned MCP server does on import.
    cache = live / "mcp" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "server.cpython-311.pyc").write_bytes(b"\x00compiled\x00")

    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = _install(plugins_dir)

    assert report.swapped is True
    assert _inode(live) == before, (
        "bytecode written by the RUNNING server made the tree look changed, so "
        "it was replaced underneath that server — the core#5009 feedback loop"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a .pyc-only difference must not warn about a replacement that should "
        "never happen"
    )


def test_real_source_change_still_swaps_even_with_bytecode_present(
    monkeypatch, tmp_path
):
    """Ignoring bytecode must not blind us to a genuine update sitting beside it."""
    body = {"v": b"print('v1')\n"}
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo(
            {"plugin.yaml": _MANIFEST, "mcp/server.py": body["v"]}
        ),
    )
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)
    live = plugins_dir / "repo"
    before = _inode(live)
    cache = live / "mcp" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "server.cpython-311.pyc").write_bytes(b"\x00compiled\x00")

    body["v"] = b"print('v2')\n"
    report = _install(plugins_dir)

    assert report.swapped is True
    assert _inode(live) != before, "a real source change must still be promoted"
    assert (live / "mcp" / "server.py").read_bytes() == b"print('v2')\n"


# ---------------------------------------------------------------------------
# What the CONTENT fingerprint cannot see (code-review findings, 2026-08-01)
#
# Both of these produce a FALSE "already matches" skip, which is strictly worse
# than the bug this file exists to fix: the swap is skipped, `swapped=True` is
# reported, and the real update never lands — silently, and self-sustainingly,
# because the difference can never appear as a content difference on any future
# boot either.
# ---------------------------------------------------------------------------
import os
import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit is not modelled on Windows")
def test_exec_bit_only_change_is_promoted(monkeypatch, tmp_path):
    """git tracks 100644 vs 100755. A `chmod +x` fix is a real update.

    Without the mode in the fingerprint this skips forever: the MCP server keeps
    failing to spawn with EACCES while the install report says the tree is live
    and correct, and no later boot can repair it.
    """
    mode = {"m": 0o644}
    def repo(url, ref, cmd, env):
        return _make_repo({"plugin.yaml": _MANIFEST, "run.sh": b"#!/bin/sh\necho hi\n"})
    _patch_git(monkeypatch, repo)
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)
    live_script = plugins_dir / "repo" / "run.sh"
    os.chmod(live_script, 0o644)
    before = _inode(plugins_dir / "repo")

    # Upstream lands the exec bit; content byte-identical.
    orig = ps._make_repo_mode if hasattr(ps, "_make_repo_mode") else None
    real_copy = ps.shutil.copytree
    def copy_with_exec(src, dst, **kw):
        r = real_copy(src, dst, **kw)
        p = os.path.join(dst, "run.sh")
        if os.path.exists(p):
            os.chmod(p, 0o755)
        return r
    monkeypatch.setattr(ps.shutil, "copytree", copy_with_exec)

    _install(plugins_dir)

    assert _inode(plugins_dir / "repo") != before, (
        "an exec-bit-only upstream change was never promoted — the plugin's "
        "entrypoint stays non-executable forever and no future boot repairs it"
    )
    assert os.stat(plugins_dir / "repo" / "run.sh").st_mode & 0o111, "exec bit not live"


def test_unreadable_file_never_reports_a_match(monkeypatch, tmp_path, caplog):
    """Two identically-unreadable files must NOT compare equal.

    The sentinel was symmetric, so a file the runtime cannot read hashed the
    same on both sides and a genuine change to it was silently never promoted.
    "I could not read this" is not evidence of equality.
    """
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo(
            {"plugin.yaml": _MANIFEST, "mcp/server.py": b"print('v1')\n"}
        ),
    )
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir)
    before = _inode(plugins_dir / "repo")

    # The real scenario is a file shipped mode 000: it is unreadable in BOTH
    # the staged copy and the live copy, so the symmetric sentinel made them
    # compare EQUAL. Making only one side unreadable would pass for the wrong
    # reason — the sides would simply differ. (Proved: with only the live side
    # patched, removing the readability gate did NOT fail this test.)
    #
    # Reads only: shutil.copytree opens the destination for WRITING, and reads
    # its source from the clone dir, so neither is affected.
    real_open = open
    def refuse(path, *a, **kw):
        mode = a[0] if a else kw.get("mode", "r")
        p = str(path).replace("\\", "/")
        if "r" in str(mode) and "molecule-plugin-" not in p and p.endswith("repo/mcp/server.py"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", refuse)

    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = _install(plugins_dir)

    assert report.swapped is True
    assert _inode(plugins_dir / "repo") != before, (
        "an unreadable file compared EQUAL to itself, so the tree was reported "
        "as already matching and a real change to it could never be promoted"
    )
    assert any(
        "could not be read" in r.getMessage() for r in caplog.records
    ), f"the reason for forcing the swap must be logged; got {[r.getMessage() for r in caplog.records]}"
