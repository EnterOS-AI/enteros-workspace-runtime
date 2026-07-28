"""The vendored manifest schema and this runtime's reference grammar must agree.

`contributes.configuration` (sdk#176) lets a plugin declare which per-install
keys it exposes. `$defs/configurationContribution` constrains declared property
names with a `propertyNames.pattern`; this runtime independently decides which
names a daemon env can actually REFERENCE, via
`plugin_settings._CONFIG_REF` (`${config:<key>}`).

Those are the same grammar expressed in two repos. If either side moves without
the other, a plugin can declare a key that no daemon could ever read — a silent
dead config, not a loud failure.

The SDK has a mirror of this test, but it hardcodes the runtime's character
class as a literal (the SDK cannot import this package). THIS test is the
stronger half: it reads the ACTUAL compiled `_CONFIG_REF` and the ACTUAL
vendored schema, so it cannot drift from either.

Hermetic: no network, no workspace, no plugin install.
"""

from __future__ import annotations

import json
import re
from importlib import resources

import pytest

from molecule_runtime import plugin_settings


def _vendored_schema() -> dict:
    with resources.files("molecule_runtime.contracts").joinpath(
        "plugin-manifest.schema.json"
    ).open("rb") as fh:
        return json.load(fh)


def _declared_name_pattern() -> str:
    return (
        _vendored_schema()["$defs"]["configurationContribution"]
        ["properties"]["properties"]["propertyNames"]["pattern"]
    )


def test_vendored_schema_declares_the_configuration_contribution():
    """Guards the re-vendor itself: an out-of-date copy has no such $def."""
    defs = _vendored_schema()["$defs"]
    assert "configurationContribution" in defs
    assert "configurationProperty" in defs
    assert "configuration" in defs["contributes"]["properties"]


def test_declared_name_pattern_matches_this_runtimes_reference_grammar():
    pattern = _declared_name_pattern()
    # _CONFIG_REF is `\$\{config:(<class>)\}` — lift the capture group out and
    # compare it to the schema's anchored whole-string pattern.
    inner = plugin_settings._CONFIG_REF.pattern
    m = re.search(r"\(([^)]+)\)", inner)
    assert m, f"could not extract the key character class from {inner!r}"
    assert pattern == f"^{m.group(1)}$", (
        f"schema propertyNames {pattern!r} disagrees with runtime "
        f"_CONFIG_REF class {m.group(1)!r} — a declarable-but-unreferenceable "
        f"key becomes possible"
    )


@pytest.mark.parametrize("key", ["timezone", "max_concurrent", "api.key", "a-b", "A0_", "9"])
def test_schema_declarable_keys_are_runtime_referenceable(key):
    """Every name the schema accepts must actually interpolate."""
    assert re.fullmatch(_declared_name_pattern(), key), "schema rejects it"
    out = plugin_settings.interpolate("${config:%s}" % key, {key: "value"})
    assert out == "value", f"runtime could not resolve a schema-legal key {key!r}"


@pytest.mark.parametrize("key", ["has space", "curly{}", "dollar$", "sla/sh"])
def test_schema_rejected_keys_are_also_unreferenceable(key):
    """And every name the schema rejects must be one the runtime cannot read
    either — otherwise the schema would be needlessly narrow."""
    assert not re.fullmatch(_declared_name_pattern(), key), "schema accepts it"
    # No substitution happens: the reference is not recognised at all, so the
    # literal survives rather than resolving to the value.
    out = plugin_settings.interpolate("${config:%s}" % key, {key: "value"})
    assert out != "value"


def test_configuration_is_tolerant_at_manifest_level():
    """The property must stay UNCONSTRAINED here.

    This repo is the one that validates: `manifest_ssot` runs this schema at
    plugin load and install. A strict `configuration` would turn a typo into a
    bricked plugin at the fail-closed install gate, instead of degrading to the
    declared defaults the way `load_delivered` does.
    """
    decl = _vendored_schema()["$defs"]["contributes"]["properties"]["configuration"]
    assert set(decl) <= {"description", "anyOf", "examples"}, (
        f"sibling assertions would apply unconditionally and defeat tolerance: {sorted(decl)}"
    )
    assert len(decl["anyOf"]) == 2, "expected a canonical branch plus an always-satisfiable one"
