"""Digest-provider protocol + failure-safe invocation.

A provider is an in-process object that returns zero or more contribution
envelopes. The assembler treats provider output as untrusted and provider
execution as fallible: a crashing, hanging, or malformed provider must never
stall the idle tick or take the digest down (contract ``failure`` policy).

:class:`ProviderRunner` owns the cross-tick failure counters (in-memory —
counts reset on restart, which is acceptable: a restart re-derives durable
provider state anyway). It is the one stateful piece; the assembler engine
proper stays pure.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from .assembler import ContributionError, check_reserved_id, validate
from .contract import Contribution, Policy

logger = logging.getLogger(__name__)


@runtime_checkable
class DigestProvider(Protocol):
    """The contract every digest provider satisfies. ``contribute`` returns the
    provider's current envelopes (possibly empty); ``on_included`` is the
    assembler's fire-outcome callback, invoked after a successful fire that
    included this provider — the goal-state cadence and phase-2 sent-folder
    escalation ride it. Providers that ignore it lose nothing."""

    provider_id: str
    # Trust marker (optional): True only for official platform-owned providers.
    # Read by the runner via getattr with a False default, so a provider that
    # omits it is treated as third-party — the fail-safe direction. An official
    # provider MUST set ``official = True`` to legitimately use a reserved id.
    official: bool

    async def contribute(self) -> Sequence[Contribution]: ...

    def on_included(self, fired_at: float) -> None: ...


@dataclass
class GatherResult:
    """One tick's provider sweep."""

    contributions: list[Contribution] = field(default_factory=list)
    # providers that errored/timed out/produced an invalid envelope this tick
    failed: frozenset[str] = frozenset()
    # providers newly disabled this tick (crossed the consecutive-failure cap)
    newly_disabled: frozenset[str] = frozenset()
    # one-line markers to render for degraded/disabled sections
    degraded_markers: list[str] = field(default_factory=list)


#: Ticks a quarantined provider sits out before it is re-probed, doubling each
#: time it fails the probe, capped. See ProviderRunner._disabled_until.
_REPROBE_BACKOFF_TICKS = (1, 2, 4, 8, 16, 32)


@dataclass
class ProviderRunner:
    """Invokes providers with per-provider timeout + skip-and-degrade, and
    QUARANTINES a provider after ``max_consecutive_failures`` consecutive
    failures (surfacing that once).

    QUARANTINE IS RECOVERABLE, and that is load-bearing. It used to be
    permanent: ``_disabled`` was a set nothing in the tick loop ever cleared —
    the only escape was ``reset()``, whose docstring scopes it to
    "re-registration after reprovision", and which the loop never calls.

    So a provider whose DEPENDENCY blipped was punished as though the provider
    itself were broken. Three failed ticks during a routine workspace-server
    redeploy and the mail section went dark **for the life of the container** —
    the server came back healthy 60s later and the agent never saw its mail
    again until it was reprovisioned. The contract's stated intent is that "a
    transient outage neither fires the digest nor suppresses it"; a permanent
    disable inverts that into permanent suppression.

    The cap still exists — a genuinely broken provider must not be retried every
    tick forever — but it now backs off and RE-PROBES instead of executing the
    provider. A provider that starts working again comes back on its own.
    """

    policy: Policy
    _failures: dict[str, int] = field(default_factory=dict)
    #: quarantined for repeated FAILURES — recoverable, re-probed on a backoff.
    _disabled: set[str] = field(default_factory=set)
    #: BANNED for a reserved-id TRUST VIOLATION — permanent, never re-probed.
    #:
    #: Kept separate from _disabled precisely because quarantine is now
    #: recoverable: a third-party provider caught spoofing an official reserved
    #: id must NEVER be resurrected by a cooldown expiring. Folding the two into
    #: one set would turn the re-probe into a periodic un-ban.
    _banned: set[str] = field(default_factory=set)
    #: quarantined provider -> ticks remaining before the next re-probe
    _cooldown: dict[str, int] = field(default_factory=dict)
    #: quarantined provider -> index into _REPROBE_BACKOFF_TICKS
    _backoff: dict[str, int] = field(default_factory=dict)

    def _quarantine(self, pid: str) -> None:
        """Disable a provider and arm its first re-probe."""
        self._disabled.add(pid)
        step = self._backoff.get(pid, 0)
        self._cooldown[pid] = _REPROBE_BACKOFF_TICKS[
            min(step, len(_REPROBE_BACKOFF_TICKS) - 1)
        ]

    def _due_for_reprobe(self, pid: str) -> bool:
        """Tick the cooldown; True when this provider gets one probe attempt.

        Called ONLY from a non-peek gather, so "ticks" here are DIGEST ticks — the
        clock the operator reasons in — and each tick consumes exactly one.

        An earlier attempt at this had the peek call it with advance=False, using a
        different readiness rule (`cooldown <= 1`). At cooldown==2 the two gathers
        in a single tick then DISAGREED about whether the provider ran, which is how
        a never-rendered section reached the baseline. There is no non-advancing
        mode any more: a peek does not probe quarantined providers at all.
        """
        remaining = self._cooldown.get(pid, 0) - 1
        if remaining > 0:
            self._cooldown[pid] = remaining
            return False
        # let it through for one probe; if it fails again _quarantine() re-arms
        # with the next (longer) backoff step.
        self._backoff[pid] = self._backoff.get(pid, 0) + 1
        self._disabled.discard(pid)
        self._cooldown.pop(pid, None)
        self._failures[pid] = self.policy.max_consecutive_failures - 1
        return True

    async def _invoke_one(
        self, provider: DigestProvider
    ) -> tuple[list[Contribution], bool]:
        """Return (contributions, ok). ok=False on any error/timeout/invalid
        envelope; contributions is empty then."""
        try:
            raw = await asyncio.wait_for(
                provider.contribute(), timeout=self.policy.provider_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning(
                "idle-digest: provider %s timed out after %ss — skipping section",
                provider.provider_id,
                self.policy.provider_timeout_seconds,
            )
            return [], False
        except Exception:  # noqa: BLE001 — a provider must never crash the tick
            logger.warning(
                "idle-digest: provider %s raised — skipping section",
                provider.provider_id,
                exc_info=True,
            )
            return [], False
        # validate every envelope; a single bad one degrades the whole section
        try:
            contribs = list(raw)
            for c in contribs:
                validate(c)
                if c.provider_id != provider.provider_id:
                    raise ContributionError(
                        f"provider {provider.provider_id!r} returned an envelope "
                        f"claiming provider_id {c.provider_id!r}"
                    )
        except ContributionError as exc:
            logger.warning(
                "idle-digest: provider %s returned an invalid envelope (%s) — "
                "skipping section",
                provider.provider_id,
                exc,
            )
            return [], False
        return contribs, True

    async def gather(
        self, providers: Sequence[DigestProvider], *, peek: bool = False
    ) -> GatherResult:
        """Invoke every not-disabled provider concurrently, honoring the
        per-provider timeout, and fold the results + failure bookkeeping.

        Before invocation each provider passes the reserved-id trust check
        (:func:`check_reserved_id`): a third-party provider claiming an official
        reserved id is rejected permanently (a hard trust violation, not a
        transient failure) so it can never emit a spoofed system-owned section.
        """
        contributions: list[Contribution] = []
        failed: set[str] = set()
        newly_disabled: set[str] = set()
        markers: list[str] = []

        active: list[DigestProvider] = []
        for p in providers:
            # A trust violation is permanent and is checked FIRST — it must not
            # be reachable by the recoverable-quarantine path below.
            if p.provider_id in self._banned:
                continue
            if p.provider_id in self._disabled:
                if peek:
                    # A PEEK NEVER PROBES A QUARANTINED PROVIDER.
                    #
                    # Letting it probe here was a permanent-silence bug: the peek
                    # could resurrect a provider the MAIN gather had skipped, so its
                    # content went into the baseline having never been rendered to
                    # the agent — and then, being "unchanged", was never rendered
                    # again. The digest exists to tell the agent things; baselining
                    # a section it never saw is the one outcome worse than a
                    # duplicate.
                    continue
                # Quarantined — but not forever. Tick its cooldown; when it
                # elapses the provider gets ONE probe. A dependency that has
                # recovered brings the section back with no operator action.
                if not self._due_for_reprobe(p.provider_id):
                    continue
                logger.info(
                    "idle-digest: re-probing quarantined provider %s", p.provider_id
                )
            official = bool(getattr(p, "official", False))
            try:
                check_reserved_id(p.provider_id, official=official)
            except ContributionError:
                if peek:
                    continue  # read-only: the main gather owns the ban decision
                # BAN, not quarantine: spoofing a reserved official id is a hard
                # trust violation, not a transient failure. It never re-probes.
                self._banned.add(p.provider_id)
                self._disabled.discard(p.provider_id)
                self._cooldown.pop(p.provider_id, None)
                newly_disabled.add(p.provider_id)
                markers.append(
                    f"• [{p.provider_id}] rejected: reserved official id "
                    f"claimed by a non-official provider"
                )
                continue
            active.append(p)

        results = await asyncio.gather(
            *(self._invoke_one(p) for p in active), return_exceptions=False
        )

        for provider, (contribs, ok) in zip(active, results):
            pid = provider.provider_id
            if peek:
                # PURE READ. No _failures, no _quarantine, no _cooldown, no markers.
                #
                # The post-fire re-gather used to run the full failure machinery, so
                # a FIRING tick double-incremented _failures (quarantine after 2
                # ticks against a cap of 3) — and if the cap was crossed inside the
                # peek, the "[mail] unavailable" announcement went into the PEEK's
                # markers, which the controller discards. The agent was never told
                # its mail section had gone dark. A tick is one observation; the
                # peek is a second look at it, not a second one.
                if ok:
                    contributions.extend(contribs)
                else:
                    failed.add(pid)
                continue
            if ok:
                # A successful probe fully rehabilitates the provider: clear the
                # backoff too, so a later, unrelated outage starts from the
                # short cooldown rather than inheriting an old long one.
                self._failures[pid] = 0
                self._backoff.pop(pid, None)
                self._cooldown.pop(pid, None)
                contributions.extend(contribs)
                continue
            failed.add(pid)
            self._failures[pid] = self._failures.get(pid, 0) + 1
            if self._failures[pid] >= self.policy.max_consecutive_failures:
                # ANNOUNCE ONCE. A backoff step > 0 means this provider was
                # already quarantined and announced, and we are looking at a
                # FAILED RE-PROBE — it must be silent.
                #
                # (_due_for_reprobe has already discarded it from _disabled to
                # let the probe through, so membership in that set cannot tell
                # us this; the backoff step is the signal that survives.)
                #
                # Re-announcing on every re-probe would put a fresh "unavailable"
                # line in the user's digest every 1, 2, 4, 8... ticks for as long
                # as the dependency stays down — turning a recoverable quarantine
                # into recurring noise. The pre-existing contract is one notice,
                # then silence until it recovers; recoverability must not cost us
                # that. The provider still lands in `failed`, so the assembler
                # keeps excluding its section from the delta hash.
                first_quarantine = self._backoff.get(pid, 0) == 0
                self._quarantine(pid)
                if first_quarantine:
                    newly_disabled.add(pid)
                    markers.append(
                        f"• [{pid}] unavailable after "
                        f"{self._failures[pid]} consecutive failures "
                        f"(retrying in {self._cooldown[pid]} tick(s))"
                    )
            else:
                markers.append(f"• [{pid}] section unavailable (degraded)")

        return GatherResult(
            contributions=contributions,
            failed=frozenset(failed),
            newly_disabled=frozenset(newly_disabled),
            degraded_markers=markers,
        )

    def is_disabled(self, provider_id: str) -> bool:
        return provider_id in self._disabled or provider_id in self._banned

    def reset(self, provider_id: str) -> None:
        """Force a provider back on immediately (e.g. re-registration after
        reprovision), skipping the cooldown. Quarantine also lifts on its own —
        see _due_for_reprobe — so this is a shortcut, not the only escape."""
        if provider_id in self._banned:
            # A reserved-id trust violation is NOT operator-resettable.
            logger.warning(
                "idle-digest: refusing to reset banned provider %s "
                "(reserved-id trust violation)",
                provider_id,
            )
            return
        self._disabled.discard(provider_id)
        self._failures.pop(provider_id, None)
        self._cooldown.pop(provider_id, None)
        self._backoff.pop(provider_id, None)
