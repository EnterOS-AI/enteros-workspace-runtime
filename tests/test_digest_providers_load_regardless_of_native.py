"""Every declared digest provider loads — native or not (no env gate).

THE DECISION (operator, 2026-08): the platform is provider-agnostic. ``native``
means *the platform ships that capability built-in* — it is a capability-ORIGIN
marker, not a trust boundary. A customer's own plugin is the customer's choice
and must work BY DEFAULT. So ``MOLECULE_DIGEST_PROVIDER_PLUGINS`` is removed
outright and a non-native plugin's ``contributes.digestProviders`` load with no
env var, no opt-in, no kill switch.

The gate's own rationale ("a digest provider is in-process code, therefore
native-only") never held: this runtime ALREADY runs third-party plugin code with
no flag — ``molecule_runtime/plugin_daemons.py`` spawns manifest-declared
subprocesses, and ``molecule_runtime/plugins_registry/__init__.py::_instantiate``
imports adaptor modules IN-PROCESS. In-process third-party execution was the
norm everywhere else in the runtime.

What survives, and is proved here:

* **Reserved-id impersonation is still refused.** ``native`` no longer gates
  LOADING; it remains the (registry-derived, unforgeable) authority for CLAIMING
  a platform-reserved provider id or the ``official`` marker. That is
  name-ownership, not code trust — and it is the only thing standing between a
  third-party plugin and a silently spoofed system section. Refusal is
  PER-PROVIDER: the plugin's other providers still load.
* **Failure isolation.** A provider that explodes at import, at construction or
  at contribute-time takes only itself down.
* **Exactly once.** Supersession stays once-only, so no id is ever rendered
  twice.

ANTI-VACUITY. Every fixture below uses ``from __future__ import annotations`` +
``@dataclass`` — the exact shape that died before #401 when the by-path loader
did not publish the module in ``sys.modules`` — and returns a class DEFINED IN
THE PLUGIN MODULE. Each load assertion checks ``type(p).__module__``, so
"loaded" cannot be satisfied by loading nothing (or by loading the runtime's own
baked class).
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from molecule_runtime.idle_digest import (
    DigestProviderContext,
    build_default_providers,
    load_digest_provider_plugins,
)
from molecule_runtime.idle_digest import plugin_loader as pl
from molecule_runtime.idle_digest.plugin_loader import NATIVE_NAMES_ENV
from molecule_runtime.plugins import LoadedPlugins, Plugin, PluginManifest

# The env var this change DELETES. Referenced as a literal on purpose: importing
# it would make the "it is gone" test unimportable rather than red.
REMOVED_ENV = "MOLECULE_DIGEST_PROVIDER_PLUGINS"

# --- fixtures ---------------------------------------------------------------

# A paying client's own reconciler, in the shape the real one ships: future
# annotations + @dataclass (the shape that was a silent no-op before #401), and
# a class defined HERE so type(p).__module__ proves plugin origin.
VENDOR_DATACLASS = textwrap.dedent(
    """
    from __future__ import annotations

    from dataclasses import dataclass, field


    @dataclass
    class VendorReconcilerProvider:
        provider_id: str = field(default="vendor-reconciler", init=False)
        official: bool = field(default=False, init=False)
        base_tier: int = field(default=7, init=False)

        async def contribute(self):
            return []

        def on_included(self, fired_at):
            pass


    def get_provider(context):
        return VendorReconcilerProvider()
    """
)

# The same, but the module ALSO writes a marker file at import time. "Was it
# refused?" is not the interesting question — "did its code actually run
# in-process?" is.
VENDOR_SIDE_EFFECT = textwrap.dedent(
    """
    from __future__ import annotations

    import os
    from dataclasses import dataclass, field

    with open(os.environ["DIGEST_IMPORT_MARKER"], "w", encoding="utf-8") as fh:
        fh.write("imported")


    @dataclass
    class VendorReconcilerProvider:
        provider_id: str = field(default="vendor-reconciler", init=False)
        official: bool = field(default=False, init=False)

        async def contribute(self):
            return []

        def on_included(self, fired_at):
            pass


    def get_provider(context):
        return VendorReconcilerProvider()
    """
)

# Two providers in ONE plugin — the reno-stars shape (2 digestProviders in one
# manifest). The second is used as the surviving sibling in refusal tests.
VENDOR_TWO = textwrap.dedent(
    """
    from __future__ import annotations

    from dataclasses import dataclass, field


    @dataclass
    class RogueGoalProvider:
        provider_id: str = field(default="goal-state", init=False)
        official: bool = field(default=True, init=False)

        async def contribute(self):
            return []

        def on_included(self, fired_at):
            pass


    @dataclass
    class VendorRosterProvider:
        provider_id: str = field(default="vendor-roster", init=False)
        official: bool = field(default=False, init=False)

        async def contribute(self):
            return []

        def on_included(self, fired_at):
            pass


    def rogue(context):
        return RogueGoalProvider()


    def roster(context):
        return VendorRosterProvider()
    """
)

# A non-native provider that self-grants `official` on a NON-reserved id.
VENDOR_SELF_OFFICIAL = textwrap.dedent(
    """
    from __future__ import annotations

    from dataclasses import dataclass, field


    @dataclass
    class VendorOfficialProvider:
        provider_id: str = field(default="vendor-notes", init=False)
        official: bool = field(default=True, init=False)

        async def contribute(self):
            return []

        def on_included(self, fired_at):
            pass


    def get_provider(context):
        return VendorOfficialProvider()
    """
)

# Blows up while the MODULE body executes.
BROKEN_AT_IMPORT = textwrap.dedent(
    """
    from __future__ import annotations

    raise RuntimeError("this vendor plugin is broken at import")
    """
)

# Imports fine, explodes in the constructor.
BROKEN_AT_CONSTRUCT = textwrap.dedent(
    """
    from __future__ import annotations


    def get_provider(context):
        raise ValueError("this vendor plugin is broken at construction")
    """
)

# A marked SUBCLASS of the runtime's own goal provider, defined in the plugin
# module — the native-plugin shape, kept here so supersession assertions can
# tell the plugin instance from the baked one.
MARKED_GOAL_SHIM = textwrap.dedent(
    """
    from __future__ import annotations

    from dataclasses import dataclass

    from molecule_runtime.idle_digest.providers.goal import GoalStateProvider


    @dataclass
    class MarkedGoalProvider(GoalStateProvider):
        came_from_plugin: bool = True


    def get_provider(context):
        return MarkedGoalProvider()
    """
)


def _make_plugin(tmp_path: Path, name: str, module_src: str, entries) -> Plugin:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "prov.py").write_text(module_src)
    return Plugin(
        name=name,
        path=str(root),
        manifest=PluginManifest(name=name, contributes={"digestProviders": entries}),
    )


def _loaded(*plugins) -> LoadedPlugins:
    lp = LoadedPlugins()
    lp.plugins = list(plugins)
    lp.plugin_names = [p.name for p in plugins]
    return lp


def _vendor_plugin(tmp_path, src=VENDOR_DATACLASS, name="reno-stars-coordinator"):
    return _make_plugin(
        tmp_path, name, src,
        [{"provider_id": "vendor-reconciler", "entrypoint": "prov:get_provider"}],
    )


def _assert_from_plugin(provider, plugin_name: str) -> None:
    """The anti-vacuity assertion: this object came from THAT plugin's module.

    ``_resolve_entrypoint`` names the synthetic module
    ``molecule_digest_plugin.<plugin-dir>.<module>``, so the plugin dir is in
    ``__module__``. A loader that silently returned a baked runtime provider —
    or nothing at all — cannot satisfy this.
    """
    mod = type(provider).__module__
    assert mod == f"molecule_digest_plugin.{plugin_name}.prov", mod


# --- the gate is GONE -------------------------------------------------------


def _executable_string_constants(path: Path):
    """Every string literal in ``path`` that is NOT a docstring.

    ``ast`` drops comments entirely, and docstrings are filtered here, so what
    is left is exactly the strings the module can ACT on — which is the only
    place a resurrected env gate could hide. Naming the removed variable in
    prose stays allowed (and is wanted: an operator who greps for it should find
    the paragraph saying it is inert).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def test_the_env_var_and_its_accessors_are_gone():
    """No vestigial kill switch. Neither accessor may survive as an importable
    name, and the variable must not appear anywhere the runtime could READ it."""
    assert not hasattr(pl, "FLAG_ENV")
    assert not hasattr(pl, "digest_provider_plugins_enabled")
    assert not hasattr(pl, "third_party_digest_providers_enabled")

    pkg = Path(pl.__file__).resolve().parent.parent
    offenders = sorted(
        str(p.relative_to(pkg))
        for p in pkg.rglob("*.py")
        if any(REMOVED_ENV in s for s in _executable_string_constants(p))
    )
    assert offenders == [], f"{REMOVED_ENV} is still executable in {offenders}"


def test_load_signature_has_no_third_party_switch():
    """The argument form of the gate is gone too — a caller must not be able to
    re-impose it."""
    import inspect

    params = inspect.signature(load_digest_provider_plugins).parameters
    assert "allow_third_party" not in params


# --- a NON-native provider loads with the env UNSET -------------------------


def test_non_native_provider_loads_with_the_env_var_UNSET(tmp_path, monkeypatch):
    """THE REQUIREMENT. Env var absent (the value every workspace has), plugin
    NOT in the native set, no injected override — the customer's provider is on
    the roster, and it is THEIR class."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _vendor_plugin(tmp_path)

    got = load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
    )

    assert [p.provider_id for p in got] == ["vendor-reconciler"]
    _assert_from_plugin(got[0], "reno-stars-coordinator")


def test_non_native_provider_module_is_actually_imported(tmp_path, monkeypatch):
    """Not merely present in a list — the plugin's module body RAN in-process."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    marker = tmp_path / "import-marker.txt"
    monkeypatch.setenv("DIGEST_IMPORT_MARKER", str(marker))
    plugin = _vendor_plugin(tmp_path, VENDOR_SIDE_EFFECT)

    got = load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
    )

    assert [p.provider_id for p in got] == ["vendor-reconciler"]
    assert marker.read_text(encoding="utf-8") == "imported"


def test_a_previously_falsy_value_no_longer_suppresses_anything(tmp_path, monkeypatch):
    """The var is INERT, not inverted. reno-stars has ``…=1`` set as an interim
    unblock and other tenants may have stale values; neither may change
    behaviour now."""
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _vendor_plugin(tmp_path)
    for value in ("0", "false", "off", "1", "true", ""):
        monkeypatch.setenv(REMOVED_ENV, value)
        got = load_digest_provider_plugins(
            _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
        )
        assert [p.provider_id for p in got] == ["vendor-reconciler"], value


def test_build_default_providers_loads_a_non_native_provider_by_default(tmp_path, monkeypatch):
    """THE PRODUCTION CALL PATH. build_default_providers reads the environment
    itself and passes nothing through; boot calls exactly this."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _vendor_plugin(tmp_path)

    providers = build_default_providers(loaded_plugins=_loaded(plugin))

    ids = [p.provider_id for p in providers]
    assert "vendor-reconciler" in ids, ids
    by_id = {p.provider_id: p for p in providers}
    _assert_from_plugin(by_id["vendor-reconciler"], "reno-stars-coordinator")


def test_provenance_is_still_reported_on_stdout(tmp_path, monkeypatch, capsys):
    """``native`` survives as METADATA. The evidence line the staging e2e greps
    ("(native=True)") must keep its shape, and a third-party load must be
    visibly labelled native=False rather than indistinguishable."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    plugin = _vendor_plugin(tmp_path)
    load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
    )
    out = capsys.readouterr().out
    assert "digest-provider: loaded 'vendor-reconciler'" in out
    assert "from plugin reno-stars-coordinator (native=False)" in out


# --- reserved-id impersonation is STILL refused -----------------------------


def test_non_native_plugin_cannot_claim_a_reserved_id_and_its_sibling_still_loads(
    tmp_path, monkeypatch
):
    """THE KEPT DEFENCE. A third-party plugin declaring the reserved
    ``goal-state`` id is refused THAT provider — and only that provider: the
    sibling contribution in the same manifest still loads. Refusing the whole
    plugin would punish a customer for one bad entry."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _make_plugin(
        tmp_path, "vendor-two", VENDOR_TWO,
        [
            {"provider_id": "goal-state", "entrypoint": "prov:rogue"},
            {"provider_id": "vendor-roster", "entrypoint": "prov:roster"},
        ],
    )

    got = load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
    )

    assert [p.provider_id for p in got] == ["vendor-roster"]
    _assert_from_plugin(got[0], "vendor-two")


def test_a_refused_reserved_claim_cannot_displace_the_built_in_section(tmp_path, monkeypatch):
    """End to end at the roster: the baked ``goal-state`` survives, unspoofed."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _make_plugin(
        tmp_path, "vendor-two", VENDOR_TWO,
        [
            {"provider_id": "goal-state", "entrypoint": "prov:rogue"},
            {"provider_id": "vendor-roster", "entrypoint": "prov:roster"},
        ],
    )

    providers = build_default_providers(loaded_plugins=_loaded(plugin))

    by_id = {p.provider_id: p for p in providers}
    assert by_id["goal-state"].__class__.__module__.startswith("molecule_runtime.")
    assert "vendor-roster" in by_id


def test_non_native_plugin_cannot_self_grant_official(tmp_path, monkeypatch):
    """``official`` is the ONLY input to the assembler's ``check_reserved_id``
    and it is a self-declared class attribute — so a non-native class asserting
    it is refused at load, the only place the forgery is catchable."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _make_plugin(
        tmp_path, "vendor-official", VENDOR_SELF_OFFICIAL,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:get_provider"}],
    )

    assert load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
    ) == []


def test_a_native_plugin_may_still_claim_a_reserved_id(tmp_path, monkeypatch):
    """Non-vacuity control for the two refusals above: the SAME reserved id from
    a NATIVE plugin loads, so the refusals are about origin, not about the id
    being unloadable."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.setenv(NATIVE_NAMES_ENV, "molecule-ai-plugin-digest-goal")
    plugin = _make_plugin(
        tmp_path, "molecule-ai-plugin-digest-goal", MARKED_GOAL_SHIM,
        [{"provider_id": "goal-state", "entrypoint": "prov:get_provider"}],
    )

    got = load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(),
        native_plugin_names=pl.native_plugin_names(),
    )

    assert [p.provider_id for p in got] == ["goal-state"]
    _assert_from_plugin(got[0], "molecule-ai-plugin-digest-goal")


# --- failure isolation ------------------------------------------------------


def test_a_broken_third_party_provider_does_not_break_the_digest(tmp_path, monkeypatch):
    """One customer plugin exploding at import and another at construction must
    cost only themselves — the healthy provider still loads and the baked roster
    is intact."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugins = [
        _make_plugin(
            tmp_path, "broken-import", BROKEN_AT_IMPORT,
            [{"provider_id": "broken-a", "entrypoint": "prov:get_provider"}],
        ),
        _make_plugin(
            tmp_path, "broken-construct", BROKEN_AT_CONSTRUCT,
            [{"provider_id": "broken-b", "entrypoint": "prov:get_provider"}],
        ),
        _vendor_plugin(tmp_path),
    ]

    providers = build_default_providers(loaded_plugins=_loaded(*plugins))

    ids = [p.provider_id for p in providers]
    assert "vendor-reconciler" in ids, ids
    assert "broken-a" not in ids and "broken-b" not in ids
    # the built-ins are untouched
    for pid in ("identity-capabilities", "task-queue", "goal-state"):
        assert pid in ids, ids


@pytest.mark.asyncio
async def test_a_provider_that_raises_at_contribute_is_quarantined_not_fatal(tmp_path, monkeypatch):
    """Isolation at the OTHER seam: a provider that loads fine but throws on the
    idle tick must not take the digest down."""
    from molecule_runtime.idle_digest import Policy, ProviderRunner

    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    src = VENDOR_DATACLASS.replace("return []", 'raise RuntimeError("vendor boom")')
    plugins = [_vendor_plugin(tmp_path, src), _vendor_plugin(tmp_path, name="good-vendor")]
    # the second plugin's manifest reuses the same provider id; give it its own
    plugins[1].manifest.contributes["digestProviders"] = [
        {"provider_id": "vendor-reconciler", "entrypoint": "prov:get_provider"}
    ]

    providers = build_default_providers(loaded_plugins=_loaded(*plugins))
    gathered = await ProviderRunner(policy=Policy.default()).gather(providers)

    ids = [c.provider_id for c in gathered.contributions]
    assert "identity-capabilities" in ids, ids
    assert "vendor-reconciler" in gathered.failed


# --- exactly once -----------------------------------------------------------


def test_every_provider_id_appears_exactly_once(tmp_path, monkeypatch):
    """The dedup that must survive the gate removal: a native plugin SUPERSEDES
    its baked twin exactly once, and a third-party provider is appended once."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.setenv(NATIVE_NAMES_ENV, "molecule-ai-plugin-digest-goal")
    plugins = [
        _make_plugin(
            tmp_path, "molecule-ai-plugin-digest-goal", MARKED_GOAL_SHIM,
            [{"provider_id": "goal-state", "entrypoint": "prov:get_provider"}],
        ),
        _vendor_plugin(tmp_path),
    ]

    providers = build_default_providers(loaded_plugins=_loaded(*plugins))

    ids = [p.provider_id for p in providers]
    # non-vacuous: they really are all present
    for pid in ("identity-capabilities", "task-queue", "goal-state", "vendor-reconciler"):
        assert ids.count(pid) == 1, f"{pid} appears {ids.count(pid)}x in {ids}"
    assert len(ids) == len(set(ids)), ids
    by_id = {p.provider_id: p for p in providers}
    assert getattr(by_id["goal-state"], "came_from_plugin", False) is True


def test_a_second_plugin_claiming_a_third_party_id_is_dropped(tmp_path, monkeypatch):
    """Plugin-vs-plugin collision on a NON-reserved id, now reachable for the
    first time. FIRST WINS, second dropped — appending would render the section
    twice, which is the failure supersession exists to prevent."""
    monkeypatch.delenv(REMOVED_ENV, raising=False)
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugins = [
        _vendor_plugin(tmp_path, name="vendor-one"),
        _vendor_plugin(tmp_path, name="vendor-two"),
    ]

    providers = build_default_providers(loaded_plugins=_loaded(*plugins))

    ids = [p.provider_id for p in providers]
    assert ids.count("vendor-reconciler") == 1, ids
    by_id = {p.provider_id: p for p in providers}
    _assert_from_plugin(by_id["vendor-reconciler"], "vendor-one")
