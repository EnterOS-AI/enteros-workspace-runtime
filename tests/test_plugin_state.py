"""Durable per-plugin state directory — runtime leg (RFC sdk#181, #360 defect A).

The load-bearing test in this file is
``test_probe_saying_durable_never_upgrades_an_undeclared_root``. It is the one
that would have caught defect A: on local-docker ``/workspace`` IS a distinct
named volume and ``probe_durability`` classifies it DURABLE, yet the teardown
path removes it on every restart. Anything that lets the probe GRANT durability
re-opens the exact hole this contract closes.

Everything else here defends the other three decisions the RFC argued:
durability is provisioner-DECLARED, the state dir is ALREADY namespaced (not a
root the plugin appends to), and degradation is loud but never fail-closed.
"""
from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

import pytest

from molecule_runtime import plugin_state


# ---------------------------------------------------------------------------
# the vendored contract is the SSOT — every literal is pinned against it
# ---------------------------------------------------------------------------
def _vendored_contract() -> dict:
    raw = (
        resources.files("molecule_runtime")
        .joinpath("contracts/plugin-state.contract.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def test_the_vendored_contract_resolves_as_a_package_resource():
    """It must ship IN THE WHEEL, not merely exist in the source tree.

    plugin_state reads it at runtime inside a workspace container, so a
    package-data glob that silently stops matching would break the seam
    everywhere at once while every source-tree test still passed.
    """
    assert plugin_state.CONTRACT_AVAILABLE is True
    assert _vendored_contract()["contract_layer"] == "plugin-state"


def test_every_runtime_literal_is_pinned_to_the_contract():
    """No re-typed strings. #360 produced three 'fixed in one place, not the
    other' regressions in a day; its closing note named the remedy as a shared
    constant PLUS a test that asserts both sides use it. This is that test."""
    c = _vendored_contract()
    assert plugin_state.STATE_DIR_ENV == c["daemon_env"]["state_dir"]
    assert plugin_state.DURABLE_ENV == c["daemon_env"]["durable"]
    assert plugin_state.DURABLE_TRUE == c["daemon_env"]["durable_true"]
    assert plugin_state.DURABLE_FALSE == c["daemon_env"]["durable_false"]
    assert plugin_state.STATE_ROOT_ENV == c["box_env"]["state_root"]
    assert plugin_state.DEFAULT_ROOT == c["paths"]["default_root"]
    assert plugin_state.FALLBACK_ROOT == c["paths"]["fallback_root"]
    assert plugin_state.DIR_TEMPLATE == c["paths"]["dir_template"]
    assert plugin_state.DIR_MODE == int(c["paths"]["mode"], 8)


def test_both_daemon_env_vars_are_reserved():
    """Reserved == stripped from the inherited env before a manifest's own env
    map is applied. If either drops off this tuple, a manifest can forge it."""
    c = _vendored_contract()
    assert c["daemon_env"]["reserved"] is True
    assert set(plugin_state.reserved_env_names()) == {
        c["daemon_env"]["state_dir"],
        c["daemon_env"]["durable"],
    }


def test_the_state_dir_is_already_namespaced_not_a_root(monkeypatch, tmp_path):
    """MOLECULE_PLUGIN_STATE_DIR is the plugin's OWN directory.

    Letting the plugin join its own name onto a root hands directory identity
    back to the untrusted side — which is the defect, not the fix.
    """
    c = _vendored_contract()
    assert c["daemon_env"]["state_dir_is_already_namespaced"] is True
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    directory, _ = plugin_state.resolve_plugin_state_dir("acme-channel")
    assert directory == tmp_path / "acme-channel"
    assert directory.name == "acme-channel"


# ---------------------------------------------------------------------------
# durability is DECLARED by the provisioner
# ---------------------------------------------------------------------------
@pytest.fixture
def distinct_mount(monkeypatch):
    """Make the probe see a distinct persistent mount.

    A pytest ``tmp_path`` lives on the SAME device as ``/``, so the real probe
    classifies it EPHEMERAL and correctly DOWNGRADES a declared root — see
    ``test_a_declared_root_on_the_root_device_is_downgraded_for_real``, which
    exercises that unstubbed. Tests about the *declared-durable* path therefore
    have to stub the mount away; that is a property of the test filesystem, not
    a softening of the assertion.
    """
    import molecule_runtime.mailbox_dir as mailbox_dir

    monkeypatch.setattr(
        mailbox_dir, "probe_durability", lambda base=None: mailbox_dir.DURABILITY_DURABLE
    )


def test_declared_root_is_credited_durable(monkeypatch, tmp_path, distinct_mount):
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    directory, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is True
    assert directory.is_dir()


def test_a_declared_root_on_the_root_device_is_downgraded_for_real(
    monkeypatch, tmp_path
):
    """No stubbing anywhere — the real probe, the real filesystem.

    This is the ``why_not_declaration_alone`` leg: a provisioner that declares a
    root it accidentally wired to the root filesystem gets corrected rather than
    believed. tmp_path is on the same device as /, which is exactly that case.
    """
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    _, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is False


def test_no_declared_root_falls_back_and_reports_not_durable(monkeypatch, tmp_path):
    """Every local-docker box and every dataVolumeGB==0 EC2 workspace today."""
    monkeypatch.delenv(plugin_state.STATE_ROOT_ENV, raising=False)
    monkeypatch.setattr(plugin_state, "FALLBACK_ROOT", str(tmp_path / "fb"))
    directory, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is False
    assert directory == tmp_path / "fb" / "p"
    assert directory.is_dir()


def test_directory_is_created_0700(monkeypatch, tmp_path):
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    directory, _ = plugin_state.resolve_plugin_state_dir("p")
    assert directory.stat().st_mode & 0o777 == plugin_state.DIR_MODE


# ---------------------------------------------------------------------------
# the probe may only DOWNGRADE — the anti-false-positive lane
# ---------------------------------------------------------------------------
def test_probe_saying_durable_never_upgrades_an_undeclared_root(
    monkeypatch, tmp_path
):
    """THE defect-A test.

    With NO provisioner declaration, a probe that classifies the directory
    DURABLE must still yield durable=False. On local-docker /workspace is a
    distinct named volume and probes DURABLE, yet workspaceTeardownVolumes
    removes it on every restart. Only the provisioner knows which of its own
    mounts its teardown preserves — so the probe can never grant durability.
    """
    import molecule_runtime.mailbox_dir as mailbox_dir

    monkeypatch.delenv(plugin_state.STATE_ROOT_ENV, raising=False)
    monkeypatch.setattr(plugin_state, "FALLBACK_ROOT", str(tmp_path / "fb"))
    monkeypatch.setattr(
        mailbox_dir, "probe_durability", lambda base=None: mailbox_dir.DURABILITY_DURABLE
    )
    _, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is False, "the probe must never UPGRADE an undeclared root"


def test_probe_saying_ephemeral_downgrades_a_declared_root(monkeypatch, tmp_path):
    """Declaration alone would be unfalsifiable; the probe is the corroboration."""
    import molecule_runtime.mailbox_dir as mailbox_dir

    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    monkeypatch.setattr(
        mailbox_dir,
        "probe_durability",
        lambda base=None: mailbox_dir.DURABILITY_EPHEMERAL,
    )
    _, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is False


def test_probe_saying_unwritable_downgrades_a_declared_root(monkeypatch, tmp_path):
    import molecule_runtime.mailbox_dir as mailbox_dir

    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    monkeypatch.setattr(
        mailbox_dir,
        "probe_durability",
        lambda base=None: mailbox_dir.DURABILITY_UNWRITABLE,
    )
    _, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is False


def test_a_probe_that_raises_keeps_the_declaration(monkeypatch, tmp_path):
    """A broken probe must not silently strip durability — it is corroboration,
    not the source of truth, and the provisioner already declared."""
    import molecule_runtime.mailbox_dir as mailbox_dir

    def _boom(base=None):
        raise OSError("probe exploded")

    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    monkeypatch.setattr(mailbox_dir, "probe_durability", _boom)
    _, durable = plugin_state.resolve_plugin_state_dir("p")
    assert durable is True


def test_the_default_root_is_not_under_the_fallback_root():
    """The declared root and the degraded root must be distinguishable paths —
    if they collapsed, DURABLE=1 and DURABLE=0 would name the same directory."""
    assert not str(plugin_state.DEFAULT_ROOT).startswith(
        str(plugin_state.FALLBACK_ROOT)
    )


# ---------------------------------------------------------------------------
# degrade loudly, never fail closed
# ---------------------------------------------------------------------------
def test_an_unwritable_declared_root_degrades_instead_of_raising(
    monkeypatch, tmp_path
):
    """Fail-closed would take a lossy-but-delivering channel plugin to
    not-running. The contract forbids it (never_fail_spawn)."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)  # r-x: cannot create a child dir
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(blocked))
    monkeypatch.setattr(plugin_state, "FALLBACK_ROOT", str(tmp_path / "fb"))
    try:
        directory, durable = plugin_state.resolve_plugin_state_dir("p")
    finally:
        blocked.chmod(0o700)
    assert durable is False
    assert directory == tmp_path / "fb" / "p"
    assert directory.is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_even_an_unusable_fallback_still_returns_a_path(monkeypatch, tmp_path):
    """Last resort: the daemon still starts. DURABLE=0 already told the plugin
    not to trust the directory; writes fail loudly inside the plugin."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(blocked))
    monkeypatch.setattr(plugin_state, "FALLBACK_ROOT", str(blocked / "fb"))
    try:
        directory, durable = plugin_state.resolve_plugin_state_dir("p")
    finally:
        blocked.chmod(0o700)
    assert durable is False
    assert isinstance(directory, Path)


def test_state_env_for_emits_the_contract_pair(monkeypatch, tmp_path, distinct_mount):
    monkeypatch.setenv(plugin_state.STATE_ROOT_ENV, str(tmp_path))
    env = plugin_state.state_env_for("p")
    assert env == {
        plugin_state.STATE_DIR_ENV: str(tmp_path / "p"),
        plugin_state.DURABLE_ENV: plugin_state.DURABLE_TRUE,
    }


def test_state_env_reports_zero_when_not_durable(monkeypatch, tmp_path):
    """The honesty flag. Silent degradation was the defect; this is the fix."""
    monkeypatch.delenv(plugin_state.STATE_ROOT_ENV, raising=False)
    monkeypatch.setattr(plugin_state, "FALLBACK_ROOT", str(tmp_path / "fb"))
    env = plugin_state.state_env_for("p")
    assert env[plugin_state.DURABLE_ENV] == plugin_state.DURABLE_FALSE
    assert plugin_state.DURABLE_FALSE == "0"


def test_required_degradation_warns_and_names_the_plugin(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    plugin_state.log_degradation(
        "gmail-channel-molecule",
        False,
        tmp_path,
        {"durability": "required", "description": "Gmail poll cursor."},
    )
    rec = [r for r in caplog.records if r.levelname == "WARNING"]
    assert rec, "durability:required degradation MUST be a WARNING"
    msg = rec[0].getMessage()
    assert "gmail-channel-molecule" in msg
    assert "Gmail poll cursor." in msg


def test_preferred_degradation_is_info_not_warning(tmp_path, caplog):
    caplog.set_level("INFO")
    plugin_state.log_degradation("cache-plugin", False, tmp_path, {"durability": "preferred"})
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert [r for r in caplog.records if r.levelname == "INFO"]


def test_durable_state_logs_nothing(tmp_path, caplog):
    caplog.set_level("INFO")
    plugin_state.log_degradation("p", True, tmp_path, {"durability": "required"})
    assert not caplog.records


def test_a_malformed_declaration_defaults_to_required(tmp_path, caplog):
    """Tolerance is contractual — a typo must not brick a plugin, and must not
    silently downgrade it to the quiet posture either."""
    caplog.set_level("INFO")
    plugin_state.log_degradation("p", False, tmp_path, "not-a-mapping")
    assert [r for r in caplog.records if r.levelname == "WARNING"]


def test_an_unknown_durability_value_defaults_to_required(tmp_path, caplog):
    caplog.set_level("INFO")
    plugin_state.log_degradation("p", False, tmp_path, {"durability": "whenever"})
    assert [r for r in caplog.records if r.levelname == "WARNING"]


# ---------------------------------------------------------------------------
# workspace-data must not be read as a durability source (RFC §8)
# ---------------------------------------------------------------------------
def test_the_default_root_is_not_in_workspace_data_persisted_paths():
    """Mirrors the SDK-side assertion. The moment workspace-data starts covering
    the plugin-state root, the two mechanisms must be reconciled deliberately —
    this test fails then, which is the intent."""
    raw = (
        resources.files("molecule_runtime")
        .joinpath("contracts/workspace-data.contract.json")
        .read_text(encoding="utf-8")
    )
    persisted = json.loads(raw).get("persisted_paths") or []
    root = str(plugin_state.DEFAULT_ROOT)
    for p in persisted:
        assert not (root == p or root.startswith(str(p).rstrip("/") + "/")), (
            f"plugin-state root {root} is now covered by workspace-data "
            f"persisted_paths entry {p} — reconcile the two mechanisms"
        )
