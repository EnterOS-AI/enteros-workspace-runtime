"""Schedules delivered as a trigger plugin's own config (the M3 location).

Schedules are moving off a core-owned top-level `schedules:` key in the
delivered config.yaml and onto the owning plugin's configuration, so a
different scheduler implementation can be swapped in and bring its own grid
without core knowing anything about cron:

    plugins:
      - source: gitea://molecule-ai/molecule-ai-plugin-scheduler#v0.2.0
        config:
          schedules:
            - {name: nightly-audit, cron: "0 3 * * *", prompt: ...}

Core renders that to /configs/plugin-settings/<install-name>.json — the same
per-install settings channel every other plugin setting rides — and the runtime
reads it back here.

THE TRAP THIS FILE EXISTS TO PIN
--------------------------------
A plugin has TWO names and they differ:

    plugin.name           the install/checkout DIRECTORY, derived from the
                          source repo -> "molecule-ai-plugin-scheduler"
    plugin.manifest.name  the manifest's own `name:` -> "molecule-scheduler"

Core keys the settings FILE on the install directory. Keying on the manifest
name would read a file that is never written — and a missing settings file is a
CLEAN NO-OP here, so the failure would be silent: no error, no schedules, no
clue. test_settings_are_keyed_on_the_install_dir_not_the_manifest_name is the
guard, and it is the reason this seam gets a dedicated test file.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from molecule_runtime import plugin_settings, schedule_seed  # noqa: E402
from molecule_runtime.schedule_store import ScheduleStore  # noqa: E402

# The install directory and the manifest name are deliberately DIFFERENT here,
# exactly as they are for the real scheduler plugin.
INSTALL_DIR = "molecule-ai-plugin-scheduler"
MANIFEST_NAME = "molecule-scheduler"

TRIGGER_MANIFEST = textwrap.dedent(
    f"""
    name: {MANIFEST_NAME}
    version: 0.2.0
    description: trigger plugin under test
    kind: trigger
    contributes:
      configuration:
        title: Scheduler
        properties:
          poll_seconds:
            type: integer
            default: 30
      daemons:
        - name: scheduler
          command: python
          args: [scheduler.py]
    """
).lstrip()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    plugins = configs / "plugins"
    (plugins / INSTALL_DIR).mkdir(parents=True)
    (plugins / INSTALL_DIR / "plugin.yaml").write_text(TRIGGER_MANIFEST)
    (configs / plugin_settings.PLUGIN_SETTINGS_DIRNAME).mkdir(parents=True)
    monkeypatch.setenv("CONFIGS_DIR", str(configs))
    grid = tmp_path / "grid"
    grid.mkdir()
    return {
        "configs": configs,
        "plugins": plugins,
        "store": ScheduleStore(str(grid / "schedules.yaml")),
        "settings": configs / plugin_settings.PLUGIN_SETTINGS_DIRNAME / f"{INSTALL_DIR}.json",
    }


def deliver(box, payload: dict) -> None:
    """Write settings exactly as core's renderPluginSettingsFiles would."""
    box["settings"].write_text(json.dumps(payload, indent=2) + "\n")


def seed(box) -> int:
    return schedule_seed.seed_schedules_from_plugin_settings(
        box["configs"],
        box["store"],
        workspace_plugins_dir=str(box["plugins"]),
        shared_plugins_dir=str(box["plugins"]),
    )


def names(box):
    return sorted(e["name"] for e in box["store"].load())


# --- the happy path ----------------------------------------------------------

def test_schedules_in_plugin_config_reach_the_grid(box):
    deliver(box, {"schedules": [
        {"name": "nightly-audit", "cron": "0 3 * * *", "prompt": "audit"},
        {"name": "hourly-sweep", "cron": "0 * * * *", "prompt": "sweep"},
    ]})
    assert seed(box) == 2
    assert names(box) == ["hourly-sweep", "nightly-audit"]
    assert all(e["source"] == "template" for e in box["store"].load())


def test_cron_expr_authoring_alias_is_accepted(box):
    # Templates in the fleet author `cron_expr`; the grid contract is `cron`.
    deliver(box, {"schedules": [{"name": "aliased", "cron_expr": "0 4 * * *", "prompt": "x"}]})
    assert seed(box) == 1
    assert box["store"].load()[0]["cron"] == "0 4 * * *"


# --- THE IDENTITY TRAP -------------------------------------------------------

def test_settings_are_keyed_on_the_install_dir_not_the_manifest_name(box):
    """The silent-failure guard.

    A settings file written under the MANIFEST name must not be picked up —
    core never writes that filename, so honouring it here would mask a real
    delivery bug. The install-dir file is the only one that counts.
    """
    wrong = box["configs"] / plugin_settings.PLUGIN_SETTINGS_DIRNAME / f"{MANIFEST_NAME}.json"
    wrong.write_text(json.dumps({"schedules": [
        {"name": "from-wrong-key", "cron": "0 5 * * *", "prompt": "x"}
    ]}))
    assert seed(box) == 0, "a manifest-name-keyed file must not be read"
    assert names(box) == []

    # ...and the correctly-keyed file IS read, so the assertion above is about
    # the key and not about seeding being broken outright.
    deliver(box, {"schedules": [{"name": "from-install-dir", "cron": "0 5 * * *", "prompt": "x"}]})
    assert seed(box) == 1
    assert names(box) == ["from-install-dir"]


# --- inertness: this must change nothing until a template opts in ------------

def test_no_settings_file_is_a_clean_noop(box):
    assert seed(box) == 0
    assert names(box) == []


def test_settings_without_a_schedules_key_is_a_clean_noop(box):
    # Every plugin in the fleet today, including the scheduler after it began
    # declaring poll_seconds.
    deliver(box, {"poll_seconds": 5})
    assert seed(box) == 0
    assert names(box) == []


def test_non_trigger_plugins_are_ignored(box):
    """Only a kind:trigger plugin owns a schedule grid.

    A skill plugin that happens to carry a `schedules` key in its config must
    not be able to inject schedules.
    """
    other = box["plugins"] / "some-skill-plugin"
    other.mkdir()
    (other / "plugin.yaml").write_text(
        "name: some-skill-plugin\nversion: 1.0.0\ndescription: not a trigger\n"
    )
    (box["configs"] / plugin_settings.PLUGIN_SETTINGS_DIRNAME / "some-skill-plugin.json").write_text(
        json.dumps({"schedules": [{"name": "sneaky", "cron": "0 6 * * *", "prompt": "x"}]})
    )
    assert seed(box) == 0
    assert names(box) == []


# --- validate-and-skip, never raise -----------------------------------------

@pytest.mark.parametrize("payload,label", [
    ({"schedules": "not-a-list"}, "non-list"),
    ({"schedules": [{"cron": "0 3 * * *", "prompt": "x"}]}, "missing name"),
    ({"schedules": [{"name": "bad-cron", "cron": "not a cron", "prompt": "x"}]}, "invalid cron"),
    ({"schedules": [{"name": "no-prompt", "cron": "0 3 * * *"}]}, "missing prompt"),
    ({"schedules": ["not-a-mapping"]}, "entry not a mapping"),
])
def test_bad_entries_are_skipped_not_raised(box, payload, label):
    deliver(box, payload)
    assert seed(box) == 0, label
    assert names(box) == []


def test_one_bad_entry_does_not_take_down_its_siblings(box):
    deliver(box, {"schedules": [
        {"name": "good-one", "cron": "0 3 * * *", "prompt": "ok"},
        {"name": "bad-one", "cron": "nonsense", "prompt": "no"},
        {"name": "good-two", "cron": "0 4 * * *", "prompt": "ok"},
    ]})
    assert seed(box) == 2
    assert names(box) == ["good-two", "good-one"] or names(box) == ["good-one", "good-two"]


def test_malformed_settings_file_degrades_to_a_noop(box):
    box["settings"].write_text("{ not json")
    assert seed(box) == 0
    assert names(box) == []


# --- template semantics carry over ------------------------------------------

def test_a_user_owned_entry_with_the_same_name_is_preserved(box):
    box["store"].replace_all([
        {"name": "nightly-audit", "cron": "0 9 * * *", "prompt": "mine", "source": "runtime"}
    ])
    deliver(box, {"schedules": [{"name": "nightly-audit", "cron": "0 3 * * *", "prompt": "theirs"}]})
    seed(box)
    entry = [e for e in box["store"].load() if e["name"] == "nightly-audit"][0]
    assert entry["cron"] == "0 9 * * *", "a runtime-owned edit must survive re-seeding"
    assert entry["source"] == "runtime"


def test_reseeding_is_idempotent(box):
    deliver(box, {"schedules": [{"name": "nightly-audit", "cron": "0 3 * * *", "prompt": "x"}]})
    assert seed(box) == 1
    assert seed(box) == 1
    assert names(box) == ["nightly-audit"]


def test_a_changed_grid_is_picked_up_on_reseed(box):
    """The reload claim: /internal/daemons/reload re-runs this path."""
    deliver(box, {"schedules": [{"name": "a", "cron": "0 3 * * *", "prompt": "x"}]})
    seed(box)
    deliver(box, {"schedules": [
        {"name": "a", "cron": "0 3 * * *", "prompt": "x"},
        {"name": "b", "cron": "0 4 * * *", "prompt": "y"},
    ]})
    seed(box)
    assert names(box) == ["a", "b"]


# --- layer 1: a plugin may ship default schedules in its manifest ------------

def test_declared_default_schedules_seed_with_nothing_delivered(box):
    """Layer 1 lives in the manifest ON THE BOX, so a plugin can ship a grid.

    The platform scheduler deliberately ships an EMPTY one, but the mechanism
    has to work for a third-party trigger plugin that ships presets — and for
    those, nothing is ever delivered, so layer 1 is the only source.
    """
    (box["plugins"] / INSTALL_DIR / "plugin.yaml").write_text(
        textwrap.dedent(
            f"""
            name: {MANIFEST_NAME}
            version: 0.2.0
            description: trigger plugin shipping preset schedules
            kind: trigger
            contributes:
              configuration:
                properties:
                  schedules:
                    type: array
                    default:
                      - name: shipped-default
                        cron: "0 7 * * *"
                        prompt: preset
              daemons:
                - name: scheduler
                  command: python
                  args: [scheduler.py]
            """
        ).lstrip()
    )
    assert not box["settings"].exists(), "nothing delivered — layer 1 is the only source"
    assert seed(box) == 1
    assert names(box) == ["shipped-default"]


def test_delivered_schedules_replace_the_declared_default(box):
    """Layers 2-5 override layer 1 wholesale for this key (shallow per-key
    merge), so an install that sets `schedules` fully owns the grid it seeds."""
    (box["plugins"] / INSTALL_DIR / "plugin.yaml").write_text(
        textwrap.dedent(
            f"""
            name: {MANIFEST_NAME}
            version: 0.2.0
            description: trigger plugin shipping preset schedules
            kind: trigger
            contributes:
              configuration:
                properties:
                  schedules:
                    type: array
                    default:
                      - name: shipped-default
                        cron: "0 7 * * *"
                        prompt: preset
              daemons:
                - name: scheduler
                  command: python
                  args: [scheduler.py]
            """
        ).lstrip()
    )
    deliver(box, {"schedules": [{"name": "install-owned", "cron": "0 8 * * *", "prompt": "mine"}]})
    assert seed(box) == 1
    assert names(box) == ["install-owned"]
