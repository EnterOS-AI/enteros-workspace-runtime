"""D1 — runtime digest-provider plugin loader (RFC molecule-core#4413).

Proves the loader discovers `contributes.digestProviders` off installed plugins,
imports each provider IN-PROCESS, applies the LOAD-TIME trust gate (only native
plugins may load official/reserved providers), and degrades safely (every
malformed/broken case is skipped, never raised). The parity tests prove each
provider loaded via its native plugin renders byte-identically to the hardcoded
one — the pre-D3 cutover gate (RFC §8: byte-identical digest output PER
provider before the baked roster is deleted).

Hermetic: writes fixture plugin modules under tmp_path; no network/mailbox.
"""
from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest

from molecule_runtime.idle_digest import (
    AgeBand,
    Band,
    Contribution,
    DigestProviderContext,
    PreviewItem,
    Urgency,
    build_default_providers,
    digest_provider_plugins_enabled,
    load_digest_provider_plugins,
    native_plugin_names,
    native_plugin_names_from_env,
    native_plugin_names_from_registry,
)
from molecule_runtime.idle_digest.plugin_loader import FLAG_ENV, NATIVE_NAMES_ENV
from molecule_runtime.idle_digest.providers.goal import GoalStateProvider
from molecule_runtime.idle_digest.providers.identity import IdentityCapabilitiesProvider
from molecule_runtime.idle_digest.providers.mail import MailSummary, SentMailProvider
from molecule_runtime.idle_digest.providers.task_queue import TaskQueueProvider
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

# The other three native shims: exactly what the D2 plugin repos ship in prov.py
# (molecule-ai-plugin-digest-task-queue / -identity / -goal @v0.1.0). The
# task-queue/goal shims ignore the context (their stores resolve via
# mailbox_dir); the identity shim forwards the persona/naming context fields.
TASK_QUEUE_SHIM = textwrap.dedent(
    """
    from molecule_runtime.idle_digest.providers.task_queue import TaskQueueProvider

    def get_provider(context):
        return TaskQueueProvider()
    """
)

IDENTITY_SHIM = textwrap.dedent(
    """
    from molecule_runtime.idle_digest.providers.identity import (
        IdentityCapabilitiesProvider,
    )

    def get_provider(context):
        return IdentityCapabilitiesProvider(
            config_path=context.config_path,
            prompt_files=context.prompt_files,
            workspace_name=context.workspace_name,
            runtime_kind=context.runtime_kind,
        )
    """
)

GOAL_SHIM = textwrap.dedent(
    """
    from molecule_runtime.idle_digest.providers.goal import GoalStateProvider

    def get_provider(context):
        return GoalStateProvider()
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


# --- per-provider parity goldens (pre-D3 cutover gate, RFC §8) ---------------
# One golden per remaining native provider: build the baked provider exactly as
# build_default_providers does, load the same provider through the plugin-loader
# path (the D2 shim), and assert the Contribution envelopes are byte-identical —
# every field, list order included (frozen-dataclass ==; the sensitivity control
# below proves == fails on any single-field change). Each golden first pins the
# expected envelope shape so it can never pass vacuously on [] == [].


def _load_one_native(tmp_path, repo: str, shim: str, provider_id: str, ctx):
    """Load exactly one provider the way the runtime does: through the manifest
    entry + trust gate + entrypoint import, from a plugin named as its repo."""
    plugin = _make_plugin(
        tmp_path, repo, shim,
        [{"provider_id": provider_id, "entrypoint": "prov:get_provider"}],
    )
    loaded = load_digest_provider_plugins(
        _loaded(plugin), ctx, native_plugin_names=frozenset({repo})
    )
    assert [p.provider_id for p in loaded] == [provider_id]
    return loaded[0]


@pytest.mark.asyncio
async def test_parity_task_queue_provider_via_plugin(tmp_path):
    """task-queue: baked TaskQueueProvider() vs the plugin-loaded one, over a
    fixed synthetic ledger exercising BOTH envelopes (E1 urgent + E2 base)."""
    from molecule_runtime import mailbox_dir

    # Seed the runtime-owned store at the path BOTH constructions resolve
    # (the conftest mailbox sandbox makes it per-test); fixed clock + fixed ids.
    store = (
        mailbox_dir.resolve() / "idle-prompt" / "providers" / "task-queue" / "state.sqlite"
    )
    writer = TaskQueueProvider(db_path=store, now_fn=lambda: 1_000_000.0)
    writer.add_task("current build", status="current", task_id="task-cur")
    writer.add_task(
        "fix the deploy", origin="user", status="queued",
        next_action="rerun the gate", task_id="task-user",
    )
    writer.add_task("resume nightly audit", origin="lifecycle", status="blocked", task_id="task-life")
    writer.upsert_awaiting_user("req-1", "need the API key")

    via_plugin = await _load_one_native(
        tmp_path, "molecule-ai-plugin-digest-task-queue", TASK_QUEUE_SHIM,
        "task-queue", DigestProviderContext(),
    ).contribute()
    baked = await TaskQueueProvider().contribute()  # as build_default_providers builds it

    # non-vacuous: the fixture must render the full two-envelope shape
    assert [c.band for c in baked] == [Band.URGENT, Band.BASE]
    assert "fix the deploy (next: rerun the gate)" in baked[0].summary
    assert "current: current build" in baked[1].summary
    assert "1 awaiting user" in baked[1].summary
    assert via_plugin == baked


@pytest.mark.asyncio
async def test_parity_identity_provider_via_plugin(tmp_path, monkeypatch):
    """identity-capabilities: baked vs plugin-loaded, persona read through the
    REAL reader off a fixture file; the tool inventory + platform check module
    seams are patched to fixed values (identically visible to both sides)."""
    (tmp_path / "persona.md").write_text(
        "Fleet infrastructure engineer for the molecule-ai org.\n"
        "Keep CI, deploys, and fleet infra healthy.\n",
        encoding="utf-8",
    )
    tools = [
        "mcp__molecule__task_list",
        "mcp__molecule__delegate_task",
        "mcp__molecule-platform__create_workspace",
        "mcp__gitea__list_prs",
    ]
    monkeypatch.setattr(
        "molecule_runtime.platform_agent_identity.loaded_mcp_tools", lambda: list(tools)
    )
    monkeypatch.setattr(
        "molecule_runtime.loaded_mcp_tools_probe._is_platform_agent", lambda: True
    )

    ctx = DigestProviderContext(
        config_path=str(tmp_path),
        prompt_files=("persona.md",),
        workspace_name="core-devops",
        runtime_kind="claude-code",
    )
    via_plugin = await _load_one_native(
        tmp_path, "molecule-ai-plugin-digest-identity", IDENTITY_SHIM,
        "identity-capabilities", ctx,
    ).contribute()
    baked = await IdentityCapabilitiesProvider(  # as build_default_providers builds it
        config_path=str(tmp_path),
        prompt_files=("persona.md",),
        workspace_name="core-devops",
        runtime_kind="claude-code",
    ).contribute()

    # non-vacuous: persona + all three tool groups actually rendered
    assert len(baked) == 1
    assert baked[0].summary.startswith("You are core-devops — Fleet infrastructure engineer")
    assert "Your responsibility: Keep CI, deploys" in baked[0].summary
    assert "workspace MCP (2)" in baked[0].summary
    assert "platform MCP (1)" in baked[0].summary
    assert "from plugins: gitea" in baked[0].summary
    assert via_plugin == baked


@pytest.mark.asyncio
async def test_parity_goal_state_provider_via_plugin(tmp_path):
    """goal-state: baked GoalStateProvider() vs the plugin-loaded one, over a
    fixed goal.yaml (never surfaced -> cadence band 'due' on both sides)."""
    from molecule_runtime import mailbox_dir

    state_dir = mailbox_dir.resolve() / "idle-prompt" / "providers" / "goal-state"
    writer = GoalStateProvider(state_dir=state_dir, now_fn=lambda: 1_000_000.0)
    writer.set("Work the labeled-ready backlog, smallest first.", cadence_seconds=1800)

    via_plugin = await _load_one_native(
        tmp_path, "molecule-ai-plugin-digest-goal", GOAL_SHIM,
        "goal-state", DigestProviderContext(),
    ).contribute()
    baked = await GoalStateProvider().contribute()  # as build_default_providers builds it

    # non-vacuous: the seeded goal must render, due (never surfaced)
    assert len(baked) == 1
    assert "Work the labeled-ready backlog, smallest first." in baked[0].summary
    assert baked[0].age_band is AgeBand.DUE
    assert via_plugin == baked


@pytest.mark.asyncio
async def test_parity_comparison_sensitive_to_every_field(tmp_path):
    """NEGATIVE CONTROL for the parity goldens: prove the == the goldens rely on
    fails on a one-step mutation of ANY Contribution field, over a REAL rendered
    envelope. Exhaustive by construction — a new envelope field without a mutant
    here fails the coverage assert, so the control can never silently thin."""
    writer = GoalStateProvider(state_dir=tmp_path / "goal", now_fn=lambda: 1_000_000.0)
    writer.set("Work the labeled-ready backlog, smallest first.")
    [c] = await writer.contribute()

    mutants = {
        "provider_id": c.provider_id + "x",
        "band": Band.URGENT,
        "tier": c.tier + 1,
        "urgency": Urgency.URGENT,
        "count": c.count + 1,
        "summary": c.summary + "\x00",  # one extra byte
        "age_band": AgeBand.JUST_INCLUDED,
        "item_ids": c.item_ids + ("extra",),
        "pull": None,
        "rearm_signals": ("extra",),
        "preview_items": (PreviewItem(item_id="x", status="open", summary="p"),),
    }
    assert set(mutants) == {f.name for f in dataclasses.fields(Contribution)}
    for fname, value in mutants.items():
        assert value != getattr(c, fname), f"mutant for {fname!r} is a no-op"
        mutated = dataclasses.replace(c, **{fname: value})
        assert [mutated] != [c], f"parity comparison is blind to field {fname!r}"


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


# --- gate-probed evidence lines (stdout — staging e2e sub-step 10e) ----------
# The workspace runtime process never configures Python logging (only mcp_cli
# does — a different entrypoint), so logger.info is dropped by the unconfigured
# root logger and can NEVER reach docker logs; the e2e gate's grep can only see
# print()ed stdout. These tests assert on capsys .out ON PURPOSE, never caplog:
# a caplog assertion passes against the broken logger.info code — the exact
# vacuity that blinded the first armed 10e run (gate run 527665).


def test_loaded_evidence_line_reaches_stdout(tmp_path, capsys):
    """SUCCESS path: loading a provider from a native plugin must print the
    evidence line the e2e gate greps — substring "from plugin <name>
    (native=True)" — to STDOUT. RED against the pre-fix logger.info code
    (negative control run recorded in the PR)."""
    plugin = _make_plugin(
        tmp_path, "molecule-ai-plugin-digest-mail", MAIL_SHIM,
        [{"provider_id": "sent-folder", "entrypoint": "prov:get_provider"}],
    )
    ctx = DigestProviderContext(comms_source=FakeSource())
    got = load_digest_provider_plugins(
        _loaded(plugin), ctx,
        native_plugin_names=frozenset({"molecule-ai-plugin-digest-mail"}),
    )
    assert [p.provider_id for p in got] == ["sent-folder"]  # the load really happened
    out = capsys.readouterr().out
    # the exact substring the gate greps (molecule-core test_staging_full_saas.sh)
    assert "from plugin molecule-ai-plugin-digest-mail (native=True)" in out
    assert "digest-provider: loaded 'sent-folder'" in out


def test_refused_plugin_prints_no_native_true_on_stdout(tmp_path, monkeypatch, capsys):
    """REFUSAL path: the trust-gate refusal stays on logging (stderr via the
    lastResort handler in the workspace process) — stdout must contain NO
    "(native=True)", so the gate's grep can never false-positive on a refused
    plugin. The scan-complete line must not print either: a refused
    contribution was FOUND, not nothing."""
    monkeypatch.setenv(FLAG_ENV, "1")  # as in the armed gate run
    plugin = _make_plugin(
        tmp_path, "rogue-plugin", ROGUE_OFFICIAL,
        [{"provider_id": "goal-state", "entrypoint": "prov:RogueProvider"}],
    )
    got = load_digest_provider_plugins(
        _loaded(plugin), DigestProviderContext(), native_plugin_names=frozenset()
    )
    assert got == []  # refused by the trust gate
    out = capsys.readouterr().out
    assert "(native=True)" not in out
    assert "scan complete" not in out


def _plugin_with_no_contributions(name: str) -> Plugin:
    return Plugin(name=name, path=".", manifest=PluginManifest(name=name, contributes={}))


def test_scan_complete_line_when_flag_on_and_zero_contributions(monkeypatch, capsys):
    """'Ran and found nothing' must be distinguishable from 'never invoked'
    (in run 527665 they were not): flag on + zero digestProviders contributions
    across the installed plugins -> one scan-complete evidence line on stdout."""
    monkeypatch.setenv(FLAG_ENV, "1")
    plugins = [
        _plugin_with_no_contributions("plugin-a"),
        _plugin_with_no_contributions("plugin-b"),
    ]
    got = load_digest_provider_plugins(
        _loaded(*plugins), DigestProviderContext(), native_plugin_names=frozenset()
    )
    assert got == []
    assert (
        "digest-provider: scan complete — no digestProviders contributions "
        "among 2 installed plugin(s)"
    ) in capsys.readouterr().out


def test_scan_complete_line_absent_when_flag_off(monkeypatch, capsys):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    got = load_digest_provider_plugins(
        _loaded(_plugin_with_no_contributions("plugin-a")),
        DigestProviderContext(),
        native_plugin_names=frozenset(),
    )
    assert got == []
    assert "scan complete" not in capsys.readouterr().out


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
