"""The idle-digest controller — drives one fire cycle and owns the delta state.

This is the piece that makes the digest *live*: it registers the providers,
evaluates a tick (gather → assemble → delta-gate → fire), invokes the
fire-outcome callbacks, recomputes the baseline POST-callback (the double-fire
guard), and persists the delta state to ``digest_state.json``. It is kernel-
gated at the boot seam (:mod:`molecule_runtime.main`); this module is pure logic
with injected seams (providers, a digest ``poster``, an optional pre-fire
recheck, an optional skip predicate, a clock) so the fire cycle is exhaustively
unit-testable without a runtime, a lease, or a live tenant.

State machine (design §3):
  * **SLEEPING** — the digest body is empty (the pinned header never counts):
    ``tick`` returns ``"sleep"`` and the boot loop stops the timer, re-armed by
    an event. Nothing is injected.
  * **ARMED** — the body is non-empty: ``tick`` re-assembles each call and fires
    ONLY when the content hash changed since the last fire (delta gate). An
    unchanged digest returns ``"unchanged"`` and never re-fires on a timer.

The idle *detection* (lease age vs the 300s threshold, no turn in flight, no
recent inbound) lives in the boot loop that CALLS ``tick`` — the controller only
decides fire-vs-not and performs the fire.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

from .assembler import assemble, should_fire, signature
from .contract import Policy
from .provider import DigestProvider, ProviderRunner

logger = logging.getLogger(__name__)

# tick outcomes
FIRED = "fired"
UNCHANGED = "unchanged"
SLEEP = "sleep"
SKIPPED = "skipped"

Poster = Callable[[str], Awaitable[None]]


@dataclass
class IdleDigestController:
    """One controller per workspace. ``tick`` is the unit under test."""

    providers: Sequence[DigestProvider]
    policy: Policy
    poster: Poster  # async: injects the rendered digest as a KIND_IDLE turn

    # injected seams
    state_dir: Optional[Path] = None
    now_fn: Callable[[], float] = time.time
    # optional pre-fire recheck (atomic fire): return False to abort the fire
    # after assembly (a turn started / new inbound arrived mid-assembly).
    pre_fire_check: Optional[Callable[[], bool]] = None
    # optional skip predicate (e.g. #381: unconsumed delegation results pending)
    skip_check: Optional[Callable[[], bool]] = None

    _runner: ProviderRunner = field(init=False)
    _baseline: tuple[tuple[str, str], ...] = field(init=False, default=())
    _last_fire: Optional[float] = field(init=False, default=None)
    _loaded: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._runner = ProviderRunner(self.policy)

    # ---- state persistence ----------------------------------------------

    def _dir(self) -> Path:
        if self.state_dir is not None:
            base = Path(self.state_dir)
        else:
            from molecule_runtime import mailbox_dir

            base = mailbox_dir.resolve() / "idle-prompt"
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass
        return base

    def _state_file(self) -> Path:
        return self._dir() / "digest_state.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self._state_file().read_text(encoding="utf-8"))
            self._baseline = tuple(
                (str(p[0]), str(p[1])) for p in data.get("baseline", [])
            )
            lf = data.get("last_fire")
            self._last_fire = float(lf) if lf is not None else None
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            self._baseline = ()
            self._last_fire = None

    def _save(self, baseline: tuple[tuple[str, str], ...], fired_at: float) -> None:
        data = {"baseline": [[p[0], p[1]] for p in baseline], "last_fire": fired_at}
        f = self._state_file()
        tmp = f.with_name(f.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            logger.warning("idle-digest: could not persist digest_state.json")

    # ---- the tick -------------------------------------------------------

    async def tick(self) -> str:
        """Evaluate one idle-fire opportunity. Returns FIRED / UNCHANGED /
        SLEEP / SKIPPED. Never raises — a provider or IO fault degrades to a
        no-op, never crashing the boot loop."""
        self._load()

        if self.skip_check is not None:
            try:
                if self.skip_check():
                    return SKIPPED  # e.g. #381: unconsumed delegation results
            except Exception:  # noqa: BLE001
                pass  # a broken skip check must not block the digest

        gathered = await self._runner.gather(self.providers)
        assembled = assemble(gathered.contributions, self.policy)

        # empty body (pinned header excluded) -> sleep, do not fire
        if assembled.is_empty:
            return SLEEP

        # delta gate: fire only when content changed since the last fire
        if not should_fire(assembled.contributions, self._baseline, gathered.failed):
            return UNCHANGED

        # atomic-fire recheck: a turn may have started / inbound arrived while we
        # assembled — if so, abort and try again next tick.
        if self.pre_fire_check is not None:
            try:
                if not self.pre_fire_check():
                    return SKIPPED
            except Exception:  # noqa: BLE001
                pass

        # FIRE: inject the rendered digest
        try:
            await self.poster(assembled.text + self._degraded_suffix(gathered))
        except Exception:  # noqa: BLE001 — a post failure must not crash the loop
            logger.warning("idle-digest: digest post failed; not advancing baseline")
            return SKIPPED

        fired_at = self.now_fn()

        # fire-outcome callbacks on every included provider
        included = {c.provider_id for c in assembled.contributions}
        for p in self.providers:
            if p.provider_id in included:
                try:
                    p.on_included(fired_at)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "idle-digest: on_included raised for %s", p.provider_id
                    )

        # recompute the baseline POST-callback (re-gather so a cadence band that
        # reset on inclusion — goal-state — is baselined at its post-reset value;
        # this is what prevents the one-tick-later double fire).
        # peek=True: a pure READ. It probes no quarantined provider and mutates no
        # failure state. The peek exists only to re-read providers whose cadence
        # band RESET on inclusion (goal-state), so they are baselined at their
        # post-reset value — that is what prevents the one-tick-later double fire.
        post = await self._runner.gather(self.providers, peek=True)
        post_assembled = assemble(post.contributions, self.policy)

        # THE RULE: a provider's baseline entry may only change if that provider was
        # RENDERED in the digest we just posted.
        #
        # (Stated precisely, because "the baseline only holds what the agent saw" is
        # an OVERCLAIM and review caught it: the peek deliberately re-reads a
        # rendered provider to capture a cadence band that RESET on inclusion —
        # goal-state — so its entry is the post-reset value, which is NOT byte-wise
        # what was rendered. That is the whole point of the peek, and it is confined
        # to providers that WERE rendered. The rule below is what actually holds.)
        #
        # Everything the delta gate does rests on this. A baseline entry means "the
        # agent has already read this" — so writing an entry for a section that was
        # never rendered makes the digest permanently silent about it (it will look
        # "unchanged" forever), and dropping an entry for a section that WAS rendered
        # makes it re-fire a byte-identical duplicate.
        #
        # Two previous attempts got this wrong by enumerating failure shapes and
        # patching each one. That approach cannot be finished: the set of ways a
        # provider can fail to be rendered keeps growing (raised; timed out;
        # quarantined; skipped; returned nothing), and every one it misses is a
        # silent bug. So the rule is now structural — derived from `just_fired`, the
        # set of providers that were genuinely IN the digest we posted:
        #
        #   rendered this tick        -> baseline it (post-reset value if the peek
        #                                saw one, else exactly what we rendered)
        #   NOT rendered this tick    -> its baseline cannot change. Keep what the
        #                                agent last saw, if anything.
        #
        # ...with one deliberate exception: a provider that ran fine and simply had
        # NOTHING to say (zero mail) must have its entry DROPPED, not carried — else
        # when that content comes back it would match a stale baseline and never
        # fire. `ran_ok_now` distinguishes "silent because healthy" from "absent
        # because broken".
        prev = dict(self._baseline)
        just_fired = dict(assembled.signature)
        post_sig = dict(post_assembled.signature)

        ran_ok_now = {
            p.provider_id
            for p in self.providers
            if p.provider_id not in post.failed
            and p.provider_id not in gathered.failed
            and not self._runner.is_disabled(p.provider_id)
        }

        merged: dict[str, str] = {}
        for pid, sig in prev.items():
            # A healthy provider with nothing to say drops out; a broken/quarantined
            # one keeps the last thing the agent read from it.
            if pid not in ran_ok_now:
                merged[pid] = sig
        for pid, sig in just_fired.items():
            merged[pid] = post_sig.get(pid, sig)

        self._baseline = tuple(sorted(merged.items()))
        self._last_fire = fired_at
        self._save(self._baseline, fired_at)
        return FIRED

    @staticmethod
    def _degraded_suffix(gathered) -> str:
        if not gathered.degraded_markers:
            return ""
        return "\n\n" + "\n".join(gathered.degraded_markers)

    # convenience for the boot loop / tests
    @property
    def baseline(self) -> tuple[tuple[str, str], ...]:
        self._load()
        return self._baseline

    def reset_provider(self, provider_id: str) -> None:
        """Re-enable a provider disabled by repeated failures (e.g. on
        re-registration after reprovision)."""
        self._runner.reset(provider_id)


def build_default_providers(
    config_path: str = "",
    prompt_files: Sequence[str] = (),
    workspace_name: str = "this workspace",
    runtime_kind: str = "claude-code",
    platform_url: str = "",
    workspace_id: str = "",
    comms_source=None,
    loaded_plugins=None,
) -> list[DigestProvider]:
    """The official provider roster, in tier order. The boot seam calls this,
    runs the goal-state first-boot migration + env bootstrap, then hands the
    list to a controller. Kept here so the roster + wiring live together.

    PLUGIN SEAM (D5, CTO 2026-07-14): the mail providers (sent-folder +
    inbound-a2a) depend only on the CommsSummarySource protocol; the binding
    happens HERE and nowhere else. Today the default binding is the platform
    mail-summary API (requires platform_url + workspace_id — both absent in
    unit contexts, in which case the mail providers are simply not assembled);
    when the communication layer moves behind the plugin boundary, the plugin
    hands its source in via ``comms_source`` and this function stays the one
    line that changes.

    PLUGIN DISCOVERY (D1, RFC molecule-core#4413): when
    ``MOLECULE_DIGEST_PROVIDER_PLUGINS`` is enabled, providers contributed by
    installed plugins (plugin-manifest ``contributes.digestProviders``) are
    discovered and appended. Flag-off (default) is byte-identical to the
    hardcoded roster below. The assembler sorts by contribution tier, so
    append order does not affect render order. ``loaded_plugins`` is the
    already-scanned :class:`~molecule_runtime.plugins.LoadedPlugins`; when the
    caller does not have it, discovery lazily re-scans.
    """
    from .providers import (
        GoalStateProvider,
        IdentityCapabilitiesProvider,
        TaskQueueProvider,
    )
    from .providers.mail import (
        InboundMailProvider,
        PlatformMailSummarySource,
        SentMailProvider,
    )

    providers: list[DigestProvider] = [
        IdentityCapabilitiesProvider(
            config_path=config_path,
            prompt_files=prompt_files,
            workspace_name=workspace_name,
            runtime_kind=runtime_kind,
        ),
        TaskQueueProvider(),
    ]
    source = comms_source
    if source is None and platform_url and workspace_id:
        source = PlatformMailSummarySource(
            platform_url=platform_url, workspace_id=workspace_id
        )
    if source is not None:
        # Tier order: sent-folder (2) before inbound-a2a (3).
        providers.append(SentMailProvider(source=source))
        providers.append(InboundMailProvider(source=source))
    providers.append(GoalStateProvider())

    # D1: append plugin-contributed providers (flag-gated; default off = byte-identical).
    from .plugin_loader import digest_provider_plugins_enabled

    if digest_provider_plugins_enabled():
        try:
            from .plugin_loader import (
                DigestProviderContext,
                load_digest_provider_plugins,
                native_plugin_names,
            )

            plugins = loaded_plugins
            if plugins is None:
                from ..plugins import load_plugins

                plugins = load_plugins()
            ctx = DigestProviderContext(
                config_path=config_path,
                prompt_files=tuple(prompt_files),
                workspace_name=workspace_name,
                runtime_kind=runtime_kind,
                comms_source=source,
                platform_url=platform_url,
                workspace_id=workspace_id,
            )
            providers.extend(
                load_digest_provider_plugins(
                    plugins, ctx, native_plugin_names=native_plugin_names()
                )
            )
        except Exception as exc:  # discovery must never break the baked roster
            logger.warning(
                "digest-provider: plugin discovery failed, using baked roster only: %s", exc
            )
    return providers


def _sig_of(contributions) -> tuple[tuple[str, str], ...]:
    return signature(contributions)
