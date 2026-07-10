"""Tests for manifest_ssot — the plugin-manifest SSOT gate (molecule-core#3383).

PR-2 (advisory) locks in:
  * ``validate_manifest_ssot`` — parsed-value validation against the vendored
    molecule-ai-sdk schema (required name/version/description, dotted-numeric
    version, runtimes list + canonical enum incl. the ``claude_code`` legacy
    alias, additionalProperties:true forward-compat).
  * ``advisory_check`` — file-level entry (missing file / bad YAML reported as
    violations, never raised) with the caller-supplied log prefix.
  * jsonschema-unavailable — the gate turns OFF with ONE loud logger.error,
    never a silent skip (the pyyaml/sha256 lesson).

PR-4 (FAIL-CLOSED promotion, enforce-by-default) locks in:
  * ``enforcement_enabled`` — default ON; ``MOLECULE_MANIFEST_SSOT_ENFORCE``
    = off/false/0 (case-insensitive) disables it with ONE loud warning.
  * Hook A (``plugins.load_plugin_manifest``) — a violating (or unparseable)
    EXISTING manifest refuses to load: the plugin is SKIPPED by
    ``load_plugins``; with enforcement off, the PR-2 advisory behaviour
    (warn-but-load, garbage YAML → name-only degrade) is byte-identical.
  * Hook B (``plugin_sources.install_declared_plugins``) — a violating
    EXISTING staged manifest is rejected exactly like a failed fetch: the
    source lands in ``report.failed`` and the swap is blocked, preserving the
    previous live tree; with enforcement off, advisory warn-but-swap.
  * CARVE-OUT: a MISSING plugin.yaml (bare-SKILL.md dirs) stays advisory-only
    under enforcement — still loads, still swaps.
"""
from __future__ import annotations

import logging

import pytest

import molecule_runtime.manifest_ssot as manifest_ssot
import molecule_runtime.plugin_sources as ps
from molecule_runtime.plugins import load_plugin_manifest, load_plugins

from tests.test_plugin_sources import _make_repo, _patch_git

_ENFORCE_ENV = "MOLECULE_MANIFEST_SSOT_ENFORCE"


@pytest.fixture(autouse=True)
def _default_enforcement_env(monkeypatch):
    """Pin the default (env unset → enforcement ON) for every test.

    Tests that exercise the advisory escape hatch set the env var themselves.
    """
    monkeypatch.delenv(_ENFORCE_ENV, raising=False)


def _valid_manifest() -> dict:
    return {
        "name": "test-plugin",
        "version": "1.2.0",
        "description": "A conformant test plugin.",
        "runtimes": ["claude-code", "codex"],
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


def test_retired_google_adk_runtime_is_enum_violation():
    manifest = _valid_manifest()
    manifest["runtimes"] = ["google-adk"]
    violations = manifest_ssot.validate_manifest_ssot(manifest)
    assert len(violations) == 1
    assert "google-adk" in violations[0]


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
# enforcement_enabled + ManifestSSOTViolation (PR-4 primitives)
# ---------------------------------------------------------------------------
def test_enforcement_enabled_default_on():
    # Env unset (autouse fixture) → enforcement is ON by default.
    assert manifest_ssot.enforcement_enabled() is True


@pytest.mark.parametrize("value", ["off", "OFF", "false", "False", "FALSE", "0"])
def test_enforcement_disabled_values(monkeypatch, value):
    monkeypatch.setenv(_ENFORCE_ENV, value)
    assert manifest_ssot.enforcement_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "banana"])
def test_enforcement_stays_on_for_non_off_values(monkeypatch, value):
    monkeypatch.setenv(_ENFORCE_ENV, value)
    assert manifest_ssot.enforcement_enabled() is True


def test_enforcement_disabled_warns_once_per_process(monkeypatch, caplog):
    monkeypatch.setenv(_ENFORCE_ENV, "off")
    monkeypatch.setattr(manifest_ssot, "_disabled_logged", False)
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.manifest_ssot"):
        assert manifest_ssot.enforcement_enabled() is False
        assert manifest_ssot.enforcement_enabled() is False
    warnings = [
        r for r in caplog.records
        if "SSOT manifest enforcement DISABLED via env" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "advisory mode" in warnings[0].getMessage()


def test_manifest_ssot_violation_carries_details():
    exc = manifest_ssot.ManifestSSOTViolation("some-plugin", ["a: bad", "b: worse"])
    assert exc.plugin_name == "some-plugin"
    assert exc.violations == ["a: bad", "b: worse"]
    assert "some-plugin" in str(exc)
    assert "2" in str(exc)


# ---------------------------------------------------------------------------
# Hook A — plugins.load_plugin_manifest
# Enforce ON (default): violating EXISTING manifest → plugin skipped.
# Enforce OFF: PR-2 advisory behaviour (warn but still load) byte-identical.
# ---------------------------------------------------------------------------
def test_load_plugin_manifest_enforce_refuses_violating_manifest(tmp_path, caplog):
    plugin_dir = tmp_path / "tampered-plugin"
    plugin_dir.mkdir()
    # Valid YAML, non-conformant manifest: no description, bad version.
    (plugin_dir / "plugin.yaml").write_text(
        "name: tampered-plugin\nversion: 1.0-beta\n"
    )
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugins"):
        manifest = load_plugin_manifest(str(plugin_dir))
    # Fail-closed: no manifest comes back — the caller must skip the plugin.
    assert manifest is None
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR
        and "SSOT manifest ENFORCEMENT: refusing to load plugin" in r.getMessage()
    ]
    assert len(errors) == 1
    assert "tampered-plugin" in errors[0].getMessage()
    # The advisory observability line still fired alongside the enforcement.
    assert any(
        "SSOT manifest validation (advisory)" in r.getMessage()
        for r in caplog.records
    )


def test_load_plugins_enforce_skips_violating_plugin(tmp_path, caplog):
    ws = tmp_path / "ws-plugins"
    bad = ws / "bad-plugin"
    bad.mkdir(parents=True)
    (bad / "plugin.yaml").write_text("name: bad-plugin\nversion: 1.0-beta\n")
    (bad / "notes.md").write_text("# should never be loaded")
    good = ws / "good-plugin"
    good.mkdir()
    (good / "plugin.yaml").write_text(
        "name: good-plugin\nversion: 1.2.0\ndescription: fine\n"
        "runtimes: [claude-code]\n"
    )
    with caplog.at_level(logging.ERROR, logger="molecule_runtime.plugins"):
        result = load_plugins(
            workspace_plugins_dir=str(ws),
            shared_plugins_dir=str(tmp_path / "no-shared"),
        )
    assert "bad-plugin" not in result.plugin_names
    assert "good-plugin" in result.plugin_names
    # Nothing from the rejected plugin leaked into the aggregate.
    assert result.prompt_fragments == []
    assert any(
        "SSOT manifest ENFORCEMENT: refusing to load plugin" in r.getMessage()
        for r in caplog.records
    )


def test_load_plugin_manifest_enforce_off_warns_but_still_loads(
    monkeypatch, tmp_path, caplog
):
    # The PR-2 advisory behaviour, now behind MOLECULE_MANIFEST_SSOT_ENFORCE=off.
    monkeypatch.setenv(_ENFORCE_ENV, "off")
    plugin_dir = tmp_path / "tampered-plugin"
    plugin_dir.mkdir()
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


def test_load_plugin_manifest_missing_manifest_carveout_under_enforcement(tmp_path):
    # CARVE-OUT: manifest-less plugins (bare-SKILL.md dirs) are common and
    # legal — a MISSING plugin.yaml stays advisory even with enforcement ON.
    plugin_dir = tmp_path / "bare-skill-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "SKILL.md").write_text("# bare skill")
    manifest = load_plugin_manifest(str(plugin_dir))
    assert manifest is not None
    assert manifest.name == "bare-skill-plugin"
    result = load_plugins(
        workspace_plugins_dir=str(tmp_path),
        shared_plugins_dir=str(tmp_path / "no-shared"),
    )
    assert "bare-skill-plugin" in result.plugin_names


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


def test_load_plugin_manifest_garbage_yaml_enforce_skips(tmp_path, caplog):
    # An EXISTING but unparseable plugin.yaml IS an invalid manifest document:
    # fail-closed under enforcement (default) — the plugin must not load.
    plugin_dir = tmp_path / "broken-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text("name: [unclosed\n  ]: {")
    with caplog.at_level(logging.ERROR, logger="molecule_runtime.plugins"):
        manifest = load_plugin_manifest(str(plugin_dir))
    assert manifest is None
    errors = [
        r for r in caplog.records
        if "SSOT manifest ENFORCEMENT: refusing to load plugin" in r.getMessage()
    ]
    assert len(errors) == 1
    assert "broken-plugin" in errors[0].getMessage()
    # And load_plugins skips the whole plugin dir.
    result = load_plugins(
        workspace_plugins_dir=str(tmp_path),
        shared_plugins_dir=str(tmp_path / "no-shared"),
    )
    assert "broken-plugin" not in result.plugin_names


def test_load_plugin_manifest_garbage_yaml_degrade_unchanged_enforce_off(
    monkeypatch, tmp_path, caplog
):
    # The PRE-EXISTING degrade path: unparseable YAML → warning + name-only
    # manifest — byte-identical when enforcement is disabled via env.
    monkeypatch.setenv(_ENFORCE_ENV, "off")
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
# Hook B — install_declared_plugins staging hook
# Enforce ON (default): violating EXISTING staged manifest → source rejected
# like a failed fetch → NO swap, previous live tree preserved.
# Enforce OFF: PR-2 advisory behaviour (warn, still swap).
# Missing manifest: carve-out — advisory-only either way.
# ---------------------------------------------------------------------------
def test_install_enforce_rejects_violating_manifest_no_swap(
    monkeypatch, tmp_path, caplog
):
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo(
            {"plugin.yaml": b"name: bad-manifest\n", "SKILL.md": b"# s"}
        ),
    )
    plugins_dir = tmp_path / "plugins"
    # A previous boot's live tree — it must survive the rejected install.
    prior = plugins_dir / "prior-plugin"
    prior.mkdir(parents=True)
    (prior / "SKILL.md").write_text("# prior")
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = ps.install_declared_plugins(
            plugins_dir=plugins_dir,
            env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
        )
    enforcement = [
        r for r in caplog.records
        if "[plugins] SSOT manifest ENFORCEMENT: rejecting" in r.getMessage()
    ]
    assert len(enforcement) == 1
    assert "gitea://owner/repo" in enforcement[0].getMessage()
    assert "violation(s)" in enforcement[0].getMessage()
    # The advisory observability line still fired too.
    assert any(
        r.getMessage().startswith("[plugins] SSOT manifest validation (advisory)")
        for r in caplog.records
    )
    # Rejected exactly like a failed fetch: failed + NO swap.
    assert report.failed == ["gitea://owner/repo"]
    assert report.installed == []
    assert report.swapped is False
    # Previous live tree intact; the violating plugin never landed.
    assert (prior / "SKILL.md").read_text() == "# prior"
    assert not (plugins_dir / "repo").exists()


def test_install_enforce_off_warns_on_invalid_manifest_but_still_swaps(
    monkeypatch, tmp_path, caplog
):
    # The PR-2 advisory behaviour, now behind MOLECULE_MANIFEST_SSOT_ENFORCE=off:
    # a non-conformant plugin.yaml warns with the "[plugins] " prefix AND the
    # swap still happens — validation doesn't feed the swap decision.
    monkeypatch.setenv(_ENFORCE_ENV, "off")
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo(
            {"plugin.yaml": b"name: bad-manifest\n", "SKILL.md": b"# s"}
        ),
    )
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
    assert not any(
        "SSOT manifest ENFORCEMENT" in r.getMessage() for r in caplog.records
    )
    # Swap decision unaffected: the plugin landed in the live tree.
    assert report.swapped is True
    assert report.installed == ["gitea://owner/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "# s"
    assert (plugins_dir / "repo" / "plugin.yaml").exists()


def test_install_missing_manifest_carveout_still_swaps_under_enforcement(
    monkeypatch, tmp_path, caplog
):
    # CARVE-OUT: a staged plugin WITHOUT plugin.yaml (bare-SKILL.md) is
    # advisory-only even with enforcement ON — it still installs and swaps.
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"# bare"}))
    plugins_dir = tmp_path / "plugins"
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        report = ps.install_declared_plugins(
            plugins_dir=plugins_dir,
            env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
        )
    # The advisory missing-manifest warning fired, but NO enforcement.
    assert any(
        "plugin.yaml missing" in r.getMessage() for r in caplog.records
    )
    assert not any(
        "SSOT manifest ENFORCEMENT" in r.getMessage() for r in caplog.records
    )
    assert report.failed == []
    assert report.swapped is True
    assert report.installed == ["gitea://owner/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "# bare"


def test_install_conformant_manifest_no_ssot_warning(monkeypatch, tmp_path, caplog):
    _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo(
            {
                "plugin.yaml": (
                    b"name: repo\nversion: 1.2.0\ndescription: fine\n"
                    b"runtimes: [claude-code]\n"
                ),
                "SKILL.md": b"# s",
            }
        ),
    )
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
