"""Tests for manifest_ssot — the ADVISORY plugin-manifest SSOT gate (molecule-core#3383, PR-2).

Locks in:
  * ``validate_manifest_ssot`` — parsed-value validation against the vendored
    molecule-ai-sdk schema (required name/version/description, dotted-numeric
    version, runtimes list + canonical enum incl. the ``claude_code`` legacy
    alias, additionalProperties:true forward-compat).
  * ``advisory_check`` — file-level entry (missing file / bad YAML reported as
    violations, never raised) with the caller-supplied log prefix.
  * Hook A (``plugins.load_plugin_manifest``) — the advisory warning fires but
    the manifest STILL loads; the pre-existing degrade path (garbage YAML →
    name-only manifest) is unchanged.
  * Hook B (``plugin_sources.install_declared_plugins``) — the staged tree is
    validated with the ``"[plugins] "`` prefix and the swap decision is
    unaffected (advisory semantics).
  * jsonschema-unavailable — the gate turns OFF with ONE loud logger.error,
    never a silent skip (the pyyaml/sha256 lesson).
"""
from __future__ import annotations

import logging

import pytest

import molecule_runtime.manifest_ssot as manifest_ssot
import molecule_runtime.plugin_sources as ps
from molecule_runtime.plugins import load_plugin_manifest

from tests.test_plugin_sources import _FakeStream, _make_targz, _patch_stream


def _valid_manifest() -> dict:
    return {
        "name": "test-plugin",
        "version": "1.2.0",
        "description": "A conformant test plugin.",
        "runtimes": ["claude-code", "deepagents", "langgraph", "autogen"],
        "kind": "env-mutator",
    }


# ---------------------------------------------------------------------------
# validate_manifest_ssot — parsed-value validation
# ---------------------------------------------------------------------------
def test_valid_manifest_no_violations():
    assert manifest_ssot.validate_manifest_ssot(_valid_manifest()) == []


def test_missing_description_is_violation():
    manifest = _valid_manifest()
    del manifest["description"]
    violations = manifest_ssot.validate_manifest_ssot(manifest)
    assert len(violations) == 1
    assert "description" in violations[0]


def test_prerelease_version_is_violation():
    manifest = _valid_manifest()
    manifest["version"] = "1.0-beta"  # validate-plugin.py rejects non-[0-9.]
    violations = manifest_ssot.validate_manifest_ssot(manifest)
    assert len(violations) == 1
    assert "version" in violations[0]


def test_runtimes_scalar_string_is_violation():
    manifest = _valid_manifest()
    manifest["runtimes"] = "claude-code"  # must be a LIST
    violations = manifest_ssot.validate_manifest_ssot(manifest)
    assert len(violations) == 1
    assert "runtimes" in violations[0]


def test_bogus_runtime_is_enum_violation():
    manifest = _valid_manifest()
    manifest["runtimes"] = ["bogus-runtime"]
    violations = manifest_ssot.validate_manifest_ssot(manifest)
    assert len(violations) == 1
    assert "runtimes" in violations[0]
    assert "bogus-runtime" in violations[0]


def test_legacy_underscore_alias_accepted():
    # claude_code is an accepted legacy alias in the SSOT runtime enum.
    manifest = _valid_manifest()
    manifest["runtimes"] = ["claude_code"]
    assert manifest_ssot.validate_manifest_ssot(manifest) == []


def test_unknown_top_level_key_tolerated():
    # additionalProperties:true — forward-compat, additive keys never fail.
    manifest = _valid_manifest()
    manifest["some_future_key"] = {"anything": True}
    assert manifest_ssot.validate_manifest_ssot(manifest) == []


# ---------------------------------------------------------------------------
# Hook A — plugins.load_plugin_manifest (advisory: warn but still load)
# ---------------------------------------------------------------------------
def test_load_plugin_manifest_warns_on_violations_but_still_loads(tmp_path, caplog):
    plugin_dir = tmp_path / "tampered-plugin"
    plugin_dir.mkdir()
    # Valid YAML, non-conformant manifest: no description, bad version.
    (plugin_dir / "plugin.yaml").write_text(
        "name: tampered-plugin\nversion: 1.0-beta\n"
    )
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugins"):
        manifest = load_plugin_manifest(str(plugin_dir))
    # Advisory warning fired...
    ssot_warnings = [
        r for r in caplog.records
        if "SSOT manifest validation (advisory)" in r.getMessage()
    ]
    assert len(ssot_warnings) == 1
    assert "violation(s)" in ssot_warnings[0].getMessage()
    # ...but the manifest STILL loads (advisory semantics — nothing blocked).
    assert manifest.name == "tampered-plugin"
    assert manifest.version == "1.0-beta"


def test_load_plugin_manifest_valid_no_ssot_warning(tmp_path, caplog):
    plugin_dir = tmp_path / "good-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: good-plugin\nversion: 1.2.0\ndescription: fine\n"
        "runtimes: [claude-code]\n"
    )
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugins"):
        manifest = load_plugin_manifest(str(plugin_dir))
    assert not any(
        "SSOT manifest validation" in r.getMessage() for r in caplog.records
    )
    assert manifest.name == "good-plugin"
    assert manifest.version == "1.2.0"


def test_load_plugin_manifest_garbage_yaml_degrade_unchanged(tmp_path, caplog):
    # The PRE-EXISTING degrade path: unparseable YAML → warning + name-only
    # manifest. The advisory hook must not have changed this behaviour.
    plugin_dir = tmp_path / "broken-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text("name: [unclosed\n  ]: {")
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugins"):
        manifest = load_plugin_manifest(str(plugin_dir))
    assert any(
        "Failed to parse plugin manifest" in r.getMessage() for r in caplog.records
    )
    assert manifest.name == "broken-plugin"  # dir-basename fallback
    assert manifest.version == "0.0.0"


# ---------------------------------------------------------------------------
# advisory_check — file-level entry
# ---------------------------------------------------------------------------
def test_advisory_check_missing_file(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.manifest_ssot"):
        violations = manifest_ssot.advisory_check(
            "no-manifest", tmp_path / "plugin.yaml"
        )
    assert violations == ["plugin.yaml missing"]
    assert any(
        "SSOT manifest validation (advisory)" in r.getMessage()
        and "plugin=no-manifest" in r.getMessage()
        for r in caplog.records
    )


def test_advisory_check_invalid_yaml(tmp_path, caplog):
    manifest_file = tmp_path / "plugin.yaml"
    manifest_file.write_text("name: [unclosed\n  ]: {")
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.manifest_ssot"):
        violations = manifest_ssot.advisory_check("bad-yaml", manifest_file)
    assert len(violations) == 1
    assert violations[0].startswith("plugin.yaml is not valid YAML:")


# ---------------------------------------------------------------------------
# jsonschema unavailable — loud OFF, never a silent skip
# ---------------------------------------------------------------------------
def test_jsonschema_unavailable_is_loud_not_silent(monkeypatch, caplog):
    monkeypatch.setattr(manifest_ssot, "_JSONSCHEMA_AVAILABLE", False)
    monkeypatch.setattr(manifest_ssot, "_unavailable_logged", False)
    with caplog.at_level(logging.ERROR, logger="molecule_runtime.manifest_ssot"):
        # A manifest that WOULD have violations — the gate is off, so [].
        assert manifest_ssot.validate_manifest_ssot({"name": "x"}) == []
        assert manifest_ssot.validate_manifest_ssot({"name": "y"}) == []
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR
        and "SSOT manifest validation unavailable" in r.getMessage()
    ]
    # ONE loud error per process, not one per manifest.
    assert len(errors) == 1
    assert "advisory gate is OFF" in errors[0].getMessage()


# ---------------------------------------------------------------------------
# Hook B — install_declared_plugins staging hook (advisory: warn, still swap)
# ---------------------------------------------------------------------------
def test_install_warns_on_invalid_manifest_but_still_swaps(monkeypatch, tmp_path, caplog):
    # Stage a plugin whose plugin.yaml is valid YAML but non-conformant
    # (missing version+description). The advisory warning must fire with the
    # "[plugins] " prefix AND the swap must still happen — validation never
    # feeds the swap decision.
    archive = _make_targz(
        {"plugin.yaml": b"name: bad-manifest\n", "SKILL.md": b"# s"}
    )
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))
    plugins_dir = tmp_path / "plugins"
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = ps.install_declared_plugins(
            plugins_dir=plugins_dir,
            env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
        )
    ssot_warnings = [
        r for r in caplog.records
        if r.getMessage().startswith("[plugins] SSOT manifest validation (advisory)")
    ]
    assert len(ssot_warnings) == 1
    assert "plugin=repo" in ssot_warnings[0].getMessage()
    # Swap decision unaffected: the plugin landed in the live tree.
    assert report.swapped is True
    assert report.installed == ["gitea://owner/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "# s"
    assert (plugins_dir / "repo" / "plugin.yaml").exists()


def test_install_conformant_manifest_no_ssot_warning(monkeypatch, tmp_path, caplog):
    archive = _make_targz(
        {
            "plugin.yaml": (
                b"name: repo\nversion: 1.2.0\ndescription: fine\n"
                b"runtimes: [claude-code]\n"
            ),
            "SKILL.md": b"# s",
        }
    )
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))
    plugins_dir = tmp_path / "plugins"
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = ps.install_declared_plugins(
            plugins_dir=plugins_dir,
            env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
        )
    assert not any(
        "SSOT manifest validation" in r.getMessage() for r in caplog.records
    )
    assert report.swapped is True
    assert (plugins_dir / "repo" / "plugin.yaml").exists()
