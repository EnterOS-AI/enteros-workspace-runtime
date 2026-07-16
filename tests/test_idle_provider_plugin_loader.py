"""D1 — runtime digest-provider plugin loader (RFC molecule-core#4413).

Proves the loader discovers `contributes.digestProviders` off installed plugins,
imports each provider IN-PROCESS, applies the LOAD-TIME trust gate (only native
plugins may load official/reserved providers), and degrades safely (every
malformed/broken case is skipped, never raised). The parity test proves a mail
provider loaded via a plugin renders byte-identically to the hardcoded one.

Hermetic: writes fixture plugin modules under tmp_path; no network/mailbox.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from molecule_runtime.idle_digest import (
    DigestProviderContext,
    build_default_providers,
    digest_provider_plugins_enabled,
    load_digest_provider_plugins,
    native_plugin_names,
    native_plugin_names_from_env,
    native_plugin_names_from_registry,
)
from molecule_runtime.idle_digest.plugin_loader import FLAG_ENV, NATIVE_NAMES_ENV
from molecule_runtime.idle_digest.providers.mail import MailSummary, SentMailProvider
from molecule_runtime.plugins import LoadedPlugins, Plugin, PluginManifest


# --- fixtures ---------------------------------------------------------------

# A native mail provider shim: exactly what molecule-ai-plugin-digest-mail ships
# in D2 — wrap the runtime's SentMailProvider, taking the source from the context.
MAIL_SHIM = textwrap.dedent(
    """
    from molecule_runtime.idle_digest.providers.mail import SentMailProvider

    def get_provider(context):
        return SentMailProvider(source=context.comms_source)
    """
)

# A third-party (non-official, non-reserved) provider class with a zero-arg ctor.
THIRD_PARTY = textwrap.dedent(
    """
    class NotesProvider:
        provider_id = "vendor-notes"
        official = False
        async def contribute(self):
            return []
        def on_included(self, fired_at):
            pass
    """
)

# A rogue third-party provider that lies: claims official + a reserved id.
ROGUE_OFFICIAL = textwrap.dedent(
    """
    class RogueProvider:
        provider_id = "goal-state"
        official = True
        async def contribute(self):
            return []
        def on_included(self, fired_at):
            pass
    """
)

# A rogue third-party provider claiming official=True with a NON-reserved id —
# isolates the `official` disjunct of the trust gate (no reserved-id involved).
OFFICIAL_NONRESERVED = textwrap.dedent(
    """
    class VendorOfficialProvider:
        provider_id = "vendor-notes"
        official = True
        async def contribute(self):
            return []
        def on_included(self, fired_at):
            pass
    """
)

# Entrypoint yields something that is not a DigestProvider.
NOT_A_PROVIDER = textwrap.dedent(
    """
    def get_provider(context):
        return object()
    """
)

# Class whose provider_id disagrees with the manifest's declared id.
ID_MISMATCH = textwrap.dedent(
    """
    class MismatchProvider:
        provider_id = "actually-this"
        official = False
        async def contribute(self):
            return []
        def on_included(self, fired_at):
            pass
    """
)


class FakeSource:
    """A CommsSummarySource returning a fixed summary (2 sent awaiting reply)."""

    async def fetch(self) -> MailSummary:
        return MailSummary(sent_awaiting_reply=2, overdue=())


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


# --- flag -------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    assert digest_provider_plugins_enabled() is False
    for on in ("1", "true", "yes"):
        monkeypatch.setenv(FLAG_ENV, on)
        assert digest_provider_plugins_enabled() is True
    for off in ("0", "false", ""):
        monkeypatch.setenv(FLAG_ENV, off)
        assert digest_provider_plugins_enabled() is False


def test_native_names_from_env(monkeypatch):
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    assert native_plugin_names_from_env() == frozenset()
    monkeypatch.setenv(NATIVE_NAMES_ENV, "a, b ,, c")
    assert native_plugin_names_from_env() == frozenset({"a", "b", "c"})


# --- loading + trust gate ---------------------------------------------------


def test_loads_third_party_non_reserved_provider(tmp_path):
    plugin = _make_plugin(
        tmp_path, "vendor-notes-plugin", THIRD_PARTY,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:NotesProvider"}],
    )
    got = load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset())
    assert [p.provider_id for p in got] == ["vendor-notes"]


def test_trust_gate_refuses_nonnative_official_reserved(tmp_path):
    plugin = _make_plugin(
        tmp_path, "rogue-plugin", ROGUE_OFFICIAL,
        [{"provider_id": "goal-state", "entrypoint": "prov:RogueProvider"}],
    )
    # not in the native set -> refused despite the class asserting official=True
    got = load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset())
    assert got == []


def test_trust_gate_refuses_nonnative_official_nonreserved(tmp_path):
    # Left disjunct in isolation: official=True alone (non-reserved id) from a
    # non-native plugin must still be refused — official is not self-grantable.
    plugin = _make_plugin(
        tmp_path, "vendor-plugin", OFFICIAL_NONRESERVED,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:VendorOfficialProvider"}],
    )
    got = load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset())
    assert got == []


@pytest.mark.parametrize("entrypoint", ["..:X", "../evil:X", "/etc/passwd:X", "a/b:X", "pkg..mod:X"])
def test_entrypoint_path_traversal_rejected(tmp_path, entrypoint):
    # A crafted entrypoint must never resolve a module outside the plugin dir.
    plugin = _make_plugin(
        tmp_path, "p", THIRD_PARTY,
        [{"provider_id": "vendor-notes", "entrypoint": entrypoint}],
    )
    assert load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()) == []


def test_native_plugin_may_load_official_reserved(tmp_path):
    plugin = _make_plugin(
        tmp_path, "molecule-ai-plugin-digest-mail", MAIL_SHIM,
        [{"provider_id": "sent-folder", "entrypoint": "prov:get_provider"}],
    )
    ctx = DigestProviderContext(comms_source=FakeSource())
    got = load_digest_provider_plugins(
        _loaded(plugin), ctx, native_plugin_names=frozenset({"molecule-ai-plugin-digest-mail"})
    )
    assert [p.provider_id for p in got] == ["sent-folder"]


@pytest.mark.asyncio
async def test_parity_mail_provider_via_plugin(tmp_path):
    """Prove-on-mail: a provider loaded via a native plugin renders byte-identically
    to the hardcoded SentMailProvider."""
    plugin = _make_plugin(
        tmp_path, "molecule-ai-plugin-digest-mail", MAIL_SHIM,
        [{"provider_id": "sent-folder", "entrypoint": "prov:get_provider"}],
    )
    ctx = DigestProviderContext(comms_source=FakeSource())
    loaded = load_digest_provider_plugins(
        _loaded(plugin), ctx, native_plugin_names=frozenset({"molecule-ai-plugin-digest-mail"})
    )
    assert len(loaded) == 1
    via_plugin = await loaded[0].contribute()
    baked = await SentMailProvider(source=FakeSource()).contribute()
    assert via_plugin == baked


# --- skip-not-reject semantics ---------------------------------------------


@pytest.mark.parametrize(
    "entries",
    [
        "not-a-list",
        None,
        [{"provider_id": "x"}],                       # missing entrypoint
        [{"entrypoint": "prov:NotesProvider"}],       # missing provider_id
        [{"provider_id": "  ", "entrypoint": "prov:NotesProvider"}],
        [{"provider_id": "x", "entrypoint": "no-colon"}],
        ["not-a-mapping"],
    ],
)
def test_malformed_entries_skipped(tmp_path, entries):
    plugin = _make_plugin(tmp_path, "p", THIRD_PARTY, entries)
    assert load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()) == []


def test_import_error_skipped(tmp_path):
    plugin = _make_plugin(
        tmp_path, "p", THIRD_PARTY,
        [{"provider_id": "vendor-notes", "entrypoint": "missing_module:X"}],
    )
    assert load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()) == []


def test_non_provider_result_skipped(tmp_path):
    plugin = _make_plugin(
        tmp_path, "p", NOT_A_PROVIDER,
        [{"provider_id": "x", "entrypoint": "prov:get_provider"}],
    )
    assert load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()) == []


def test_declared_id_mismatch_skipped(tmp_path):
    plugin = _make_plugin(
        tmp_path, "p", ID_MISMATCH,
        [{"provider_id": "declared-this", "entrypoint": "prov:MismatchProvider"}],
    )
    assert load_digest_provider_plugins(_loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()) == []


def test_discovery_never_raises_on_broken_input():
    class Broken:
        @property
        def plugins(self):
            raise RuntimeError("boom")

    # getattr(...) reads .plugins; a raising property must not escape.
    assert load_digest_provider_plugins(Broken(), DigestProviderContext(), native_plugin_names=frozenset()) == []


# --- integration with build_default_providers -------------------------------


def _baseline_len() -> int:
    # identity + task-queue + goal-state (no mail source in unit ctx) = 3
    return len(build_default_providers())


def test_build_default_providers_flag_off_is_baseline(tmp_path, monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    plugin = _make_plugin(
        tmp_path, "vendor-notes-plugin", THIRD_PARTY,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:NotesProvider"}],
    )
    providers = build_default_providers(loaded_plugins=_loaded(plugin))
    assert len(providers) == _baseline_len()
    assert "vendor-notes" not in [p.provider_id for p in providers]


def test_build_default_providers_flag_on_appends(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    plugin = _make_plugin(
        tmp_path, "vendor-notes-plugin", THIRD_PARTY,
        [{"provider_id": "vendor-notes", "entrypoint": "prov:NotesProvider"}],
    )
    providers = build_default_providers(loaded_plugins=_loaded(plugin))
    ids = [p.provider_id for p in providers]
    assert "vendor-notes" in ids
    assert len(providers) == _baseline_len() + 1


# --- native allow-list sourced from the vendored registry (D-trust) ---------


def test_native_plugin_names_from_registry_are_install_names(monkeypatch):
    """The registry-sourced native set is derived from each entry's SOURCE (the
    on-disk install name), NOT its `name` field. The four digest plugin repos
    must be present (the trust gate governs digest providers), and the scheduler
    resolves to its REPO name `molecule-ai-plugin-scheduler` — proving we key on
    the source, since its registry `name` is the different `molecule-scheduler`."""
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    names = native_plugin_names_from_registry()
    for repo in (
        "molecule-ai-plugin-digest-mail",
        "molecule-ai-plugin-digest-identity",
        "molecule-ai-plugin-digest-task-queue",
        "molecule-ai-plugin-digest-goal",
    ):
        assert repo in names, f"{repo} missing from registry native set: {sorted(names)}"
    # The scheduler is present under its INSTALL name (the repo), never the
    # registry `name` field — the whole reason we parse the source.
    assert "molecule-ai-plugin-scheduler" in names
    assert "molecule-scheduler" not in names


def test_native_plugin_names_unions_registry_and_env(monkeypatch):
    """native_plugin_names() = vendored registry (SSOT) UNION the env escape-hatch.
    The env can only EXTEND the trusted set, never remove a registry entry."""
    monkeypatch.setenv(NATIVE_NAMES_ENV, "self-host-private-plugin, another")
    combined = native_plugin_names()
    reg = native_plugin_names_from_registry()
    assert reg <= combined  # registry entries always trusted
    assert "self-host-private-plugin" in combined  # env extends
    assert "another" in combined


def test_trust_gate_uses_registry_source_endtoend(tmp_path, monkeypatch):
    """End-to-end with the REAL vendored registry (no injected set, no env): a
    plugin installed under a registry repo name loads its official/reserved
    provider, while the identical provider from a non-registry plugin is refused."""
    monkeypatch.delenv(NATIVE_NAMES_ENV, raising=False)
    ctx = DigestProviderContext(comms_source=FakeSource())

    native = _make_plugin(
        tmp_path, "molecule-ai-plugin-digest-mail", MAIL_SHIM,
        [{"provider_id": "sent-folder", "entrypoint": "prov:get_provider"}],
    )
    got = load_digest_provider_plugins(
        _loaded(native), ctx, native_plugin_names=native_plugin_names()
    )
    assert [p.provider_id for p in got] == ["sent-folder"]

    impostor = _make_plugin(
        tmp_path, "totally-not-a-native-plugin", MAIL_SHIM,
        [{"provider_id": "sent-folder", "entrypoint": "prov:get_provider"}],
    )
    refused = load_digest_provider_plugins(
        _loaded(impostor), ctx, native_plugin_names=native_plugin_names()
    )
    assert refused == [], "a non-registry plugin must not load a reserved provider"
