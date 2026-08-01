"""installed_refs: the commit the box ACTUALLY installed, reported to the platform.

core#5007. `workspace_plugins.installed_sha` is a claim BY the control plane, not
an observation OF the box, and nothing the box reports carries a commit —
`installed`/`skipped`/`failed` are SOURCE STRINGS, so two commits of the same
`#main` pin are indistinguishable.

The cost was measured twice: throughout core#5009 the CP reported
`installed_sha = 3d41a34, drift = 0` for a workspace whose disk matched and whose
behaviour did not, and every step of that diagnosis — and of core#5011 — needed
shell access to a customer's container to answer "what is this box running".
"""

from __future__ import annotations

import json

from molecule_runtime import plugin_sources as ps
from molecule_runtime import plugin_install_report as pir

from tests.test_plugin_sources import _make_repo

_MANIFEST = b"name: repo\ndescription: resolved-refs fixture\nversion: 0.1.0\n"
_SHA = "abc123def456abc123def456abc123def456abcd"


def _patch_git_head(monkeypatch, head):
    """git fake that answers `rev-parse HEAD` (or fails, when head is None)."""
    from pathlib import Path as _P

    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            if head is None:
                raise ps.subprocess.CalledProcessError(128, cmd, output="", stderr="no HEAD")
            return ps.subprocess.CompletedProcess(cmd, 0, head + "\n", "")
        if "clone" not in cmd:
            return ps.subprocess.CompletedProcess(cmd, 0, "", "")
        dest = _P(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir(exist_ok=True)
        for rel, data in _make_repo({"plugin.yaml": _MANIFEST}).items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return ps.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ps.subprocess, "run", fake_run)


def test_install_records_the_resolved_commit(monkeypatch, tmp_path):
    _patch_git_head(monkeypatch, _SHA)
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo#main"},
    )
    assert report.swapped is True
    assert report.installed_refs.get("gitea://owner/repo#main") == _SHA, (
        "the box resolved a commit and did not record it — the control plane is "
        "left unable to tell what this workspace actually installed"
    )


def test_unresolvable_head_is_omitted_not_faked(monkeypatch, tmp_path):
    """'unknown' must not be reported as if it were a commit.

    A consumer comparing a reported ref against its own record must be able to
    trust that a PRESENT value is real. Sending the literal 'unknown' would make
    every un-resolvable fetch look like a drift.
    """
    _patch_git_head(monkeypatch, None)
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo#main"},
    )
    assert report.swapped is True
    assert "gitea://owner/repo#main" not in report.installed_refs, (
        "an unresolved HEAD was reported as a commit; absent must mean absent"
    )


def test_payload_sends_the_mapping_not_a_bool(monkeypatch, tmp_path):
    """report_payload coerces unknown fields with bool() — a dict would send True."""
    _patch_git_head(monkeypatch, _SHA)
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo#main"},
    )
    payload = pir.report_payload(report)
    assert "installed_refs" in payload, "the contract names it but the payload omits it"
    assert payload["installed_refs"] == {"gitea://owner/repo#main": _SHA}, (
        f"installed_refs was not sent as a mapping: {payload['installed_refs']!r}"
    )
    json.dumps(payload)  # must remain wire-serialisable


def test_contract_names_installed_refs():
    """The vendored contract must carry the field, or the payload silently drops it."""
    assert "installed_refs" in pir.field_names(), (
        "the vendored contract predates installed_refs — re-vendor from the SDK"
    )
