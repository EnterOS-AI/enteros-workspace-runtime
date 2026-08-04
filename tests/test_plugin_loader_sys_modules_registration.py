"""Regression gate: a by-path loaded module MUST be in ``sys.modules`` while its
own body executes.

THE PRODUCTION FAILURE (measured on all 8 prod workspaces, runtime 0.4.81).
Every ``install: default`` digest plugin failed to import, so the digest-provider
plugin path was 100% dead in production while CI was green::

    digest-provider: failed importing prov:get_provider from
      /configs/plugins/molecule-ai-plugin-digest-goal/prov.py:
      'NoneType' object has no attribute '__dict__'
    digest-provider: entrypoint object None is not callable
    ... import-fail=5, loaded=0, supersedes=0

MECHANISM. ``importlib.util.module_from_spec()`` does NOT publish the module in
``sys.modules``; the import system does that separately, and a by-path loader
that calls ``exec_module`` directly must do it itself. CPython's ``dataclasses``
resolves *string* annotations through the defining module's globals with an
UNGUARDED lookup (``_is_type``: ``sys.modules.get(cls.__module__).__dict__``).
Under ``from __future__ import annotations`` every annotation is a string, so
ANY ``@dataclass`` in an unregistered module raises ``AttributeError: 'NoneType'
object has no attribute '__dict__'`` — and both loaders swallow import errors as
skip-not-crash, so the whole feature reads as "no providers" rather than as a
crash.

All four first-party digest plugins (``molecule-ai-plugin-digest-{identity,
task-queue,mail,goal}``) are exactly this shape.

WHY THE EXISTING SUITE MISSED IT. Every fixture shim in
``test_idle_provider_plugin_loader.py`` is a thin wrapper that only *imports*
already-registered runtime provider classes and defines no ``@dataclass`` of its
own, and the parity goldens construct provider classes directly rather than
through the loader. Nothing ever executed a ``@dataclass`` body *inside* a
by-path loaded module. These tests do, through the real public entrypoints.

RE-REGRESSION. ``molecule_runtime/plugins_registry/test_resolve_plugin.py`` is
titled "sys.modules injection fix (issue #296)" — the sibling adaptor loader hit
this class of bug already. Its fix registered the ``plugins_registry.*`` package
ALIASES but never the loaded module itself, so it stayed vulnerable to the
``@dataclass`` half. Both loaders are pinned here.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from molecule_runtime.idle_digest import (
    DigestProviderContext,
    build_default_providers,
    load_digest_provider_plugins,
)
from molecule_runtime.idle_digest.plugin_loader import NATIVE_NAMES_ENV
from molecule_runtime.plugins import LoadedPlugins, Plugin, PluginManifest
from molecule_runtime.plugins_registry import _load_module_from_path

# --- fixture sources --------------------------------------------------------

# The production shape: `from __future__ import annotations` + a module-level
# @dataclass, exactly like molecule-ai-plugin-digest-goal/prov.py (GoalDoc) and
# its three siblings. The dataclass is what dies when the module is missing from
# sys.modules; the provider below is a plain third-party class so the test is
# about the IMPORT, not about the trust gate.
DATACLASS_PROVIDER = textwrap.dedent(
    '''
    """Mirrors the real digest plugins: future-annotations + @dataclass."""
    from __future__ import annotations

    from dataclasses import dataclass, field
    from typing import Optional


    @dataclass
    class VendorDoc:
        text: str = ""
        tags: list = field(default_factory=list)
        updated_at: Optional[str] = None


    class NotesProvider:
        provider_id = "vendor-notes"
        official = False

        def __init__(self):
            self.doc = VendorDoc(text="hello")

        async def contribute(self):
            return []

        def on_included(self, fired_at):
            pass


    def get_provider(context):
        return NotesProvider()
    '''
)

# A module that blows up PART WAY through its body, AFTER the loader has had to
# publish it. Nothing half-initialised may survive in sys.modules.
EXPLODING_PROVIDER = textwrap.dedent(
    '''
    from __future__ import annotations

    from dataclasses import dataclass


    @dataclass
    class Partial:
        text: str = ""


    raise RuntimeError("boom during module body")


    def get_provider(context):  # pragma: no cover - never reached
        return None
    '''
)


def _dataclass_shim(runtime_import: str, base: str, ctor_args: str) -> str:
    """A native digest shim carrying its own @dataclass, like the real plugins.

    The shipped plugins are not thin wrappers any more (D3 moved the render half
    into the plugin), so the dedup/supersession invariant has to be proven on a
    module that actually executes a ``@dataclass`` body.

    The provider is a MARKED subclass defined IN this module, so a test can tell
    the plugin-contributed instance apart from the baked one by
    ``type(obj).__module__`` — without that, "no duplicates" would also pass on
    the broken loader, where the plugin never imports and only the baked roster
    survives.
    """
    return textwrap.dedent(
        f'''
        from __future__ import annotations

        from dataclasses import dataclass, field

        {runtime_import}


        @dataclass
        class ShimDoc:
            note: str = ""
            history: list = field(default_factory=list)


        class PluginProvider({base}):
            came_from_plugin = True
            doc = ShimDoc(note="shipped-by-plugin")


        def get_provider(context):
            return PluginProvider({ctor_args})
        '''
    )


IDENTITY_DC_SHIM = _dataclass_shim(
    "from molecule_runtime.idle_digest.providers.identity import "
    "IdentityCapabilitiesProvider",
    "IdentityCapabilitiesProvider",
    "\n                config_path=context.config_path,"
    "\n                prompt_files=context.prompt_files,"
    "\n                workspace_name=context.workspace_name,"
    "\n                runtime_kind=context.runtime_kind,\n            ",
)
TASK_QUEUE_DC_SHIM = _dataclass_shim(
    "from molecule_runtime.idle_digest.providers.task_queue import TaskQueueProvider",
    "TaskQueueProvider",
    "",
)
GOAL_DC_SHIM = _dataclass_shim(
    "from molecule_runtime.idle_digest.providers.goal import GoalStateProvider",
    "GoalStateProvider",
    "",
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


def _mod_name(plugin_name: str, mod: str = "prov") -> str:
    return f"molecule_digest_plugin.{plugin_name}.{mod}"


def _purge_synthetic_modules() -> None:
    for name in [
        n
        for n in sys.modules
        if n.startswith(("molecule_digest_plugin.", "sysmod_probe"))
    ]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _clean_synthetic_modules():
    """Keep the synthetic namespaces out of each other's way, both directions.

    Purged BEFORE as well as after: now that the loader publishes what it
    imports, a sibling test file that loaded a same-named fixture plugin leaves
    a live entry behind, and "the loader put it there" must not be satisfiable
    by test-ordering luck.
    """
    _purge_synthetic_modules()
    yield
    _purge_synthetic_modules()


# --- the digest loader ------------------------------------------------------


def test_dataclass_provider_imports_through_the_real_digest_loader(tmp_path, caplog):
    """PROVE-FAIL ANCHOR. Before the fix this returns [] and logs
    "'NoneType' object has no attribute '__dict__'" — the exact production line.
    """
    plugin = _make_plugin(
        tmp_path,
        "sysmodprobe-notes-plugin",
        DATACLASS_PROVIDER,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:get_provider"}],
    )

    with caplog.at_level("WARNING"):
        providers = load_digest_provider_plugins(
            _loaded(plugin),
            DigestProviderContext(),
            native_plugin_names=frozenset(),
        )

    assert "object has no attribute '__dict__'" not in caplog.text, caplog.text
    assert [p.provider_id for p in providers] == ["vendor-notes"]
    # the dataclass really did get built (non-vacuous: the class body ran)
    assert providers[0].doc.text == "hello"


def test_digest_loader_publishes_the_module_in_sys_modules(tmp_path):
    """The mechanism, pinned directly: the module is reachable at its own
    ``__name__`` — which is the only thing that makes dataclasses (and
    pickling, and ``typing.get_type_hints``) work inside a by-path module."""
    plugin = _make_plugin(
        tmp_path,
        "sysmodprobe-notes-plugin",
        DATACLASS_PROVIDER,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:get_provider"}],
    )
    name = _mod_name("sysmodprobe-notes-plugin")
    assert name not in sys.modules

    providers = load_digest_provider_plugins(
        _loaded(plugin),
        DigestProviderContext(),
        native_plugin_names=frozenset(),
    )

    assert providers, "provider must load"
    assert name in sys.modules
    assert sys.modules[name].__name__ == name
    assert type(providers[0]).__module__ == name


def test_failed_module_body_leaves_no_entry_in_sys_modules(tmp_path):
    """Cleanup path: a module that raises mid-body must not leave a
    half-initialised object behind for the next import to find."""
    plugin = _make_plugin(
        tmp_path,
        "sysmodprobe-exploding-plugin",
        EXPLODING_PROVIDER,
        [{"provider_id": "vendor-boom", "entrypoint": "prov:get_provider"}],
    )
    name = _mod_name("sysmodprobe-exploding-plugin")

    providers = load_digest_provider_plugins(
        _loaded(plugin),
        DigestProviderContext(),
        native_plugin_names=frozenset(),
    )

    assert providers == []  # skip-not-crash still holds
    assert name not in sys.modules, "half-initialised module leaked into sys.modules"


def test_failed_module_body_restores_a_pre_existing_entry(tmp_path):
    """A failing load must not evict an unrelated module that already owned the
    name (the loader publishes into a namespace it does not exclusively own)."""
    import types

    plugin = _make_plugin(
        tmp_path,
        "sysmodprobe-exploding-plugin",
        EXPLODING_PROVIDER,
        [{"provider_id": "vendor-boom", "entrypoint": "prov:get_provider"}],
    )
    name = _mod_name("sysmodprobe-exploding-plugin")
    sentinel = types.ModuleType(name)
    sentinel.sentinel = True
    sys.modules[name] = sentinel

    load_digest_provider_plugins(
        _loaded(plugin),
        DigestProviderContext(),
        native_plugin_names=frozenset(),
    )

    assert sys.modules.get(name) is sentinel


def test_two_plugins_shipping_prov_py_stay_isolated(tmp_path):
    """The namespacing that made registration safe in the first place: two
    plugins both shipping ``prov.py`` get distinct sys.modules keys, so
    publishing one can never shadow the other."""
    a = _make_plugin(
        tmp_path,
        "sysmodprobe-vendor-a",
        DATACLASS_PROVIDER.replace("vendor-notes", "vendor-a-notes"),
        [{"provider_id": "vendor-a-notes", "entrypoint": "prov:get_provider"}],
    )
    b = _make_plugin(
        tmp_path,
        "sysmodprobe-vendor-b",
        DATACLASS_PROVIDER.replace("vendor-notes", "vendor-b-notes"),
        [{"provider_id": "vendor-b-notes", "entrypoint": "prov:get_provider"}],
    )

    providers = load_digest_provider_plugins(
        _loaded(a, b),
        DigestProviderContext(),
        native_plugin_names=frozenset(),
    )

    assert sorted(p.provider_id for p in providers) == ["vendor-a-notes", "vendor-b-notes"]
    assert _mod_name("sysmodprobe-vendor-a") in sys.modules
    assert _mod_name("sysmodprobe-vendor-b") in sys.modules
    assert sys.modules[_mod_name("sysmodprobe-vendor-a")] is not sys.modules[_mod_name("sysmodprobe-vendor-b")]


# --- dedup must survive the fix ---------------------------------------------


def _native_dataclass_plugins(tmp_path):
    return [
        _make_plugin(
            tmp_path,
            "molecule-ai-plugin-digest-identity",
            IDENTITY_DC_SHIM,
            [{"provider_id": "identity-capabilities", "entrypoint": "prov:get_provider"}],
        ),
        _make_plugin(
            tmp_path,
            "molecule-ai-plugin-digest-task-queue",
            TASK_QUEUE_DC_SHIM,
            [{"provider_id": "task-queue", "entrypoint": "prov:get_provider"}],
        ),
        _make_plugin(
            tmp_path,
            "molecule-ai-plugin-digest-goal",
            GOAL_DC_SHIM,
            [{"provider_id": "goal-state", "entrypoint": "prov:get_provider"}],
        ),
    ]


def test_dataclass_native_plugins_supersede_exactly_once(tmp_path, monkeypatch):
    """PRODUCTION SCENARIO, now actually reachable. The registry's
    install:"default" digest plugins installed and IMPORTING: each provider id
    appears exactly once — the plugin supersedes its baked twin, never doubles
    it."""
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)

    baseline = [p.provider_id for p in build_default_providers(loaded_plugins=_loaded())]
    providers = build_default_providers(
        loaded_plugins=_loaded(*_native_dataclass_plugins(tmp_path))
    )
    ids = [p.provider_id for p in providers]

    for pid in ("identity-capabilities", "task-queue", "goal-state"):
        assert ids.count(pid) == 1, f"{pid} appears {ids.count(pid)}x in {ids}"
    assert len(ids) == len(set(ids)) == len(baseline)
    # NON-VACUOUS. Exactly-once also holds when nothing imports at all (the
    # broken loader left only the baked roster). Pin that the survivor is the
    # object the PLUGIN MODULE built.
    for pid in ("identity-capabilities", "task-queue", "goal-state"):
        survivor = next(p for p in providers if p.provider_id == pid)
        assert getattr(survivor, "came_from_plugin", False) is True, pid
        assert type(survivor).__module__.startswith("molecule_digest_plugin."), (
            pid,
            type(survivor).__module__,
        )
        assert survivor.doc.note == "shipped-by-plugin"


@pytest.mark.asyncio
async def test_assembled_digest_has_no_duplicate_envelope_with_dataclass_plugins(
    tmp_path, monkeypatch
):
    """End to end over the production roster: one envelope per provider id."""
    from molecule_runtime.idle_digest import Policy, ProviderRunner, assemble

    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)

    providers = build_default_providers(
        loaded_plugins=_loaded(*_native_dataclass_plugins(tmp_path))
    )
    policy = Policy.default()
    gathered = await ProviderRunner(policy=policy).gather(providers)
    ids = [c.provider_id for c in gathered.contributions]

    assert "identity-capabilities" in ids, ids
    for pid in set(ids):
        assert ids.count(pid) == 1, f"DUPLICATE {pid} in {ids}"
    digest = assemble(gathered.contributions, policy)
    assert digest.text.count("You are this workspace") == 1, digest.text


# --- the sibling adaptor loader (issue #296, same class of bug) --------------


ADAPTOR_WITH_DATACLASS = textwrap.dedent(
    '''
    from __future__ import annotations

    from dataclasses import dataclass, field

    from plugins_registry.protocol import PluginAdaptor


    @dataclass
    class AdaptorConfig:
        name: str = ""
        extras: list = field(default_factory=list)


    class Adaptor:
        protocol = PluginAdaptor
        config = AdaptorConfig(name="demo")
    '''
)

EXPLODING_ADAPTOR = textwrap.dedent(
    '''
    from __future__ import annotations

    from dataclasses import dataclass


    @dataclass
    class Half:
        x: str = ""


    raise RuntimeError("boom during adaptor body")
    '''
)


def test_plugins_registry_loader_imports_a_dataclass_adaptor(tmp_path):
    """The #296 fix registered the ``plugins_registry.*`` ALIASES but never the
    loaded module itself, leaving the sibling loader open to the identical
    ``@dataclass`` failure. A plugin-shipped adaptor carrying a dataclass must
    import."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text(ADAPTOR_WITH_DATACLASS)

    module = _load_module_from_path("sysmod_probe.adapter", adapter)

    assert module is not None, "adaptor module must load"
    assert module.Adaptor.config.name == "demo"
    assert sys.modules.get("sysmod_probe.adapter") is module


def test_plugins_registry_loader_cleans_up_after_a_failed_adaptor(tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(EXPLODING_ADAPTOR)

    module = _load_module_from_path("sysmod_probe.broken", adapter)

    assert module is None
    assert "sysmod_probe.broken" not in sys.modules
