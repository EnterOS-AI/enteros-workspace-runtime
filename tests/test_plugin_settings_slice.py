"""End-to-end slice: a delivered setting reaching a daemon's environment.

This is the executable answer to the question four RFC review rounds could not
settle by specification. Each test states what it proves.

THE QUESTION
------------
"Can a settings change reach a running daemon without a restart, and who writes
the file?"

WHAT THIS PROVES
----------------
* A plugin's declared default (layer 1) reaches the daemon env.
* A DELIVERED file overrides that default, and the daemon env changes.
* Changing the file and RE-RUNNING DISCOVERY moves the value — i.e. the value is
  read at discovery time, not baked at boot. `/internal/daemons/reload` already
  calls the same discovery path, so this is the no-restart claim, tested.
* A plugin with no configuration keeps a BYTE-IDENTICAL env (no silent change).
* Malformed / missing / oversize settings degrade to defaults and never drop the
  plugin ("drop bad keys, keep the plugin").

WHAT THIS DELIBERATELY DOES NOT PROVE
-------------------------------------
That anything WRITES the file post-provision. That is core's leg and remains the
RFC's open question — see test_the_writer_is_the_open_question, which documents
the gap rather than papering over it.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from molecule_runtime import plugin_settings  # noqa: E402
from molecule_runtime.plugin_daemons import discover_daemon_specs  # noqa: E402


PLUGIN_YAML = textwrap.dedent(
    """
    name: demo-scheduler
    version: 1.0.0
    description: smallest end-to-end slice
    kind: trigger
    contributes:
      configuration:
        title: Demo Scheduler
        properties:
          timezone:
            type: string
            default: UTC
          max_concurrent:
            type: integer
            default: 1
      daemons:
        - name: ticker
          command: python
          args: [ticker.py]
          env:
            SCHEDULER_TZ: "${config:timezone}"
            SCHEDULER_MAX: "${config:max_concurrent}"
            STATIC: unchanged
    """
).lstrip()

PLAIN_PLUGIN_YAML = textwrap.dedent(
    """
    name: plain-daemon
    version: 1.0.0
    description: declares no configuration at all
    contributes:
      daemons:
        - name: plain
          command: bash
          args: [run.sh]
          env:
            ONLY: value
    """
).lstrip()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """A fake workspace box: /configs/plugins/<name>/ + /configs/plugin-settings/."""
    configs = tmp_path / "configs"
    plugins = configs / "plugins"
    (plugins / "demo-scheduler").mkdir(parents=True)
    (plugins / "demo-scheduler" / "plugin.yaml").write_text(PLUGIN_YAML)
    (plugins / "plain-daemon").mkdir(parents=True)
    (plugins / "plain-daemon" / "plugin.yaml").write_text(PLAIN_PLUGIN_YAML)
    (configs / plugin_settings.PLUGIN_SETTINGS_DIRNAME).mkdir(parents=True)
    monkeypatch.setenv("CONFIGS_DIR", str(configs))
    monkeypatch.setenv("MOLECULE_MANIFEST_SSOT_ENFORCE", "off")
    return {
        "configs": configs,
        "plugins": plugins,
        "settings": configs / plugin_settings.PLUGIN_SETTINGS_DIRNAME / "demo-scheduler.json",
    }


def _spec(box, name="ticker"):
    specs = discover_daemon_specs(
        workspace_plugins_dir=str(box["plugins"]), shared_plugins_dir=str(box["plugins"])
    )
    match = [s for s in specs if s.name == name]
    assert match, f"daemon {name!r} not discovered; got {[s.name for s in specs]}"
    return match[0]


# ---- layer 1: the declared default reaches the daemon -----------------------

def test_declared_default_reaches_daemon_env(box):
    """Layer 1 lives in the manifest ON THE BOX. Core cannot supply it at
    provision time (it has no manifest then) — the runtime can, and does."""
    spec = _spec(box)
    assert spec.env["SCHEDULER_TZ"] == "UTC"
    assert spec.env["SCHEDULER_MAX"] == "1"
    assert spec.env["STATIC"] == "unchanged"


# ---- layers 2-5: a delivered file overrides it ------------------------------

def test_delivered_setting_overrides_default(box):
    box["settings"].write_text(json.dumps({"timezone": "America/Vancouver"}))
    spec = _spec(box)
    assert spec.env["SCHEDULER_TZ"] == "America/Vancouver"
    assert spec.env["SCHEDULER_MAX"] == "1", "undelivered keys must keep their default"


# ---- THE CENTRAL CLAIM: change the file, re-discover, value moves -----------

def test_changing_the_file_moves_the_daemon_env_without_restart(box):
    """The no-restart claim, executable.

    `/internal/daemons/reload` re-runs `load_plugins` and this same discovery
    path on every call. So if discovery re-reads the file, a reload delivers a
    settings change to a running workspace with no re-provision.
    """
    box["settings"].write_text(json.dumps({"timezone": "Europe/Berlin"}))
    assert _spec(box).env["SCHEDULER_TZ"] == "Europe/Berlin"

    box["settings"].write_text(json.dumps({"timezone": "Asia/Tokyo", "max_concurrent": 7}))
    after = _spec(box)
    assert after.env["SCHEDULER_TZ"] == "Asia/Tokyo"
    assert after.env["SCHEDULER_MAX"] == "7"


def test_the_config_file_path_is_handed_to_the_daemon(box):
    """The other consumption path: a plugin can read its own settings directly."""
    box["settings"].write_text(json.dumps({"timezone": "UTC"}))
    spec = _spec(box)
    assert spec.env[plugin_settings.PLUGIN_CONFIG_FILE_ENV] == str(box["settings"])


# ---- no silent behaviour change for plugins without settings ----------------

def test_plugin_without_configuration_keeps_byte_identical_env(box):
    """The regression this slice already caught once: injecting the config-file
    var into EVERY daemon changes every existing plugin's environment for no
    benefit."""
    spec = _spec(box, name="plain")
    assert spec.env == {"ONLY": "value"}
    assert plugin_settings.PLUGIN_CONFIG_FILE_ENV not in spec.env


# ---- drop bad keys, keep the plugin -----------------------------------------

@pytest.mark.parametrize(
    "content,label",
    [("{ not json", "malformed json"), ('["a","list"]', "wrong top-level type"), ("", "empty file")],
)
def test_broken_settings_degrade_to_defaults_and_keep_the_plugin(box, content, label):
    box["settings"].write_text(content)
    spec = _spec(box)
    assert spec.env["SCHEDULER_TZ"] == "UTC", f"{label} must fall back to the declared default"


def test_oversize_settings_are_refused_not_parsed(box):
    box["settings"].write_text(json.dumps({"timezone": "x" * (plugin_settings.MAX_SETTINGS_BYTES + 10)}))
    assert _spec(box).env["SCHEDULER_TZ"] == "UTC"


def test_unresolvable_reference_becomes_empty_not_literal(box):
    """Shipping a literal '${config:foo}' into a daemon env would be worse than
    empty — the process would treat it as a real value."""
    assert plugin_settings.interpolate("${config:nope}", {}) == ""
    assert plugin_settings.interpolate("a-${config:nope}-b", {}) == "a--b"


# ---- the honest gap ---------------------------------------------------------

def test_the_writer_is_the_open_question(box):
    """Nothing in the runtime creates or updates the settings file.

    This asserts the CURRENT boundary, so the RFC's open question stays visible
    instead of being assumed solved: discovery reads whatever is on disk, and
    if nobody writes it, the daemon simply sees the declared defaults. Which
    core path writes this file post-provision is still undecided.
    """
    assert not box["settings"].exists()
    assert _spec(box).env["SCHEDULER_TZ"] == "UTC"
    assert not box["settings"].exists(), "the runtime must never author this file"
