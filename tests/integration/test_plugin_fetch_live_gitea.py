"""LIVE plugin-fetch e2e — real git, real Gitea, real commit SHA.

The regression this exists for (staging test5, 2026-07-13):

  MOLECULE_DECLARED_PLUGINS pinned the Lark plugin to commit 973a35b7…, the
  fetcher ran `git clone --depth 1 --branch 973a35b7…`, and real git answered
  "Remote branch 973a35b7… not found in upstream origin" — `--branch` resolves
  ref NAMES, never a bare object id. The plugin failed to install; the failed
  source aborted the whole tree swap; the concierge's own management MCP was
  staged in that same swap and went down with it; `register` fail-closed on
  mcp_server_present=false; the agent parked in `failed` and refused every user
  message.

The unit tests cover this with a fake git that models the real one. This module
does NOT mock anything: it drives the real fetch against the real forge over the
network, so it fails for exactly the reason production did. If the fetcher ever
regresses to a ref-name-only fetch, this goes red.

Skips (loudly, via `-rs`) when the forge is unreachable — a network-dead runner
must not silently turn the gate green.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import molecule_runtime.plugin_sources as ps

# The exact repo + commit that bricked test5. Public repo — fetched anonymously,
# no token needed (and none is sent).
GITEA_BASE = os.environ.get("MOLECULE_GITEA_BASE_URL", "https://git.moleculesai.app")
LARK_REPO = "molecule-ai/lark-channel-molecule"
LARK_SHA = "973a35b70d17694c6412b40fe963689fae2a353f"
MGMT_MCP = "molecule-ai/molecule-ai-plugin-molecule-platform-mcp"

pytestmark = pytest.mark.integration


def _forge_reachable() -> bool:
    """Probe with GIT, not urllib.

    urllib is the wrong prober here: Cloudflare fronts the forge and answers a
    bot-challenge to a bare python HTTP client, so a urllib probe reports
    "unreachable" on a host where git clones perfectly well — and the gate would
    silently SKIP itself into permanent greenness. Probe with the same tool the
    code under test actually uses.
    """
    if shutil.which("git") is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", f"{GITEA_BASE}/{LARK_REPO}.git"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Reachability is probed via an AUTOUSE fixture, not a module-level
# `pytest.mark.skipif`, so the 30s `git ls-remote` runs at TEST SETUP (only when a
# live test actually executes) rather than at COLLECTION time. A module-level
# skipif's condition is evaluated on import, so every `pytest tests/integration`
# collection paid this blocking network probe — and ci.yml collects this module
# twice (review [4]). Module scope caches the probe to one call per run.
@pytest.fixture(scope="module")
def _forge_ok() -> bool:
    return _forge_reachable()


@pytest.fixture(autouse=True)
def _require_forge(_forge_ok):
    if not _forge_ok:
        pytest.skip(f"{GITEA_BASE} unreachable or git missing — LIVE gate inert")


def test_live_fetch_of_the_sha_that_bricked_test5(tmp_path):
    """The literal incident: fetch the Lark plugin at its pinned commit."""
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": f"gitea://{LARK_REPO}#{LARK_SHA}",
            "MOLECULE_GITEA_BASE_URL": GITEA_BASE,
        },
    )

    assert report.failed == [], (
        f"the SHA-pinned plugin did not install: {report.failed}. This is the "
        f"test5 regression — `clone --branch <sha>` cannot resolve a commit id."
    )
    assert report.swapped is True
    installed = tmp_path / "plugins" / "lark-channel-molecule"
    assert installed.is_dir(), "plugin tree not materialized"
    assert any(installed.iterdir()), "plugin tree is empty"
    # git metadata must never ship inside the plugins tree.
    assert not (installed / ".git").exists()


def test_live_a_broken_plugin_does_not_take_the_mgmt_mcp_down(tmp_path):
    """THE blast radius, live. A bogus source must not stop the management MCP
    from installing — that is what turned an unfetchable plugin into a dead
    agent."""
    bogus = f"gitea://{LARK_REPO}#0000000000000000000000000000000000000000"
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": f"{bogus},gitea://{MGMT_MCP}#main",
            "MOLECULE_GITEA_BASE_URL": GITEA_BASE,
        },
    )

    assert report.failed == [bogus], "the bogus pin should be the ONLY failure"
    # The whole point: the mgmt-MCP went live anyway.
    assert report.swapped is True, (
        "the swap was aborted by an unrelated failing plugin — this is exactly "
        "the failure that starved the concierge of its management MCP"
    )
    mgmt = tmp_path / "plugins" / "molecule-ai-plugin-molecule-platform-mcp"
    assert mgmt.is_dir() and any(mgmt.iterdir()), (
        "the management MCP was NOT installed because a third-party plugin "
        "failed — the concierge would fail-closed on mcp_server_present=false"
    )


def _extra_provider_sources() -> "list[str]":
    """Additional LIVE provider sources to exercise, from config — NOT hardcoded.

    The fetch + credential layers are provider-agnostic, so the live gate must be
    able to prove that on a forge that is not ours without a code change. Set
    ``MOLECULE_PLUGIN_E2E_SOURCES`` to a comma-separated list of declared-plugin
    sources, each pinned to a COMMIT SHA — e.g.

        MOLECULE_PLUGIN_E2E_SOURCES=https://github.com/o/r#<sha>,https://gitlab.com/o/r#<sha>

    Each is fetched for real. Private repos authenticate through the same
    per-host token map the runtime uses (MOLECULE_GIT_TOKENS /
    MOLECULE_GIT_TOKEN__<HOST>), so this also exercises multi-forge credentials.
    Empty by default: an unset matrix means "no extra providers configured", not
    "provider-agnostic is proven".
    """
    raw = (os.environ.get("MOLECULE_PLUGIN_E2E_SOURCES") or "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not on PATH")
@pytest.mark.parametrize("source", _extra_provider_sources())
def test_live_sha_pin_on_any_configured_provider(source, tmp_path):
    """Fetch a SHA-pinned plugin from a NON-default forge (github/gitlab/self-
    hosted), proving the fetch is genuinely provider-agnostic and not just
    Gitea-shaped. Parameterized from config — add a forge without touching code.
    """
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={**os.environ, "MOLECULE_DECLARED_PLUGINS": source},
    )
    assert report.failed == [], f"SHA-pinned fetch failed on {source}: {report.failed}"
    assert report.swapped is True
    installed = list((tmp_path / "plugins").iterdir())
    assert installed and any(p.iterdir() for p in installed if p.is_dir())


def test_live_git_really_does_reject_branch_sha(tmp_path):
    """Pins the PREMISE of the fix against the real forge.

    If git ever learns to resolve a bare SHA via `--branch`, the fix is
    unnecessary and this test tells us so — rather than us carrying plumbing on
    a stale assumption. It asserts the failure mode is real, not folklore.
    """
    clone_url = f"{GITEA_BASE}/{LARK_REPO}.git"
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--single-branch",
         "--branch", LARK_SHA, clone_url, str(tmp_path / "c")],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    assert proc.returncode != 0, (
        "git accepted a bare SHA as --branch — the premise of the SHA fetch "
        "path no longer holds; revisit _git_fetch_tree"
    )
    assert "not found in upstream origin" in proc.stderr.lower() or \
           "remote branch" in proc.stderr.lower(), proc.stderr[:300]
