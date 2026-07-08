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


@dataclass
class ProviderRunner:
    """Invokes providers with per-provider timeout + skip-and-degrade, and
    disables a provider after ``max_consecutive_failures`` consecutive
    failures (surfacing that once)."""

    policy: Policy
    _failures: dict[str, int] = field(default_factory=dict)
    _disabled: set[str] = field(default_factory=set)

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

    async def gather(self, providers: Sequence[DigestProvider]) -> GatherResult:
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
            if p.provider_id in self._disabled:
                continue
            official = bool(getattr(p, "official", False))
            try:
                check_reserved_id(p.provider_id, official=official)
            except ContributionError:
                self._disabled.add(p.provider_id)
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
            if ok:
                self._failures[pid] = 0
                contributions.extend(contribs)
                continue
            failed.add(pid)
            self._failures[pid] = self._failures.get(pid, 0) + 1
            if self._failures[pid] >= self.policy.max_consecutive_failures:
                self._disabled.add(pid)
                newly_disabled.add(pid)
                markers.append(
                    f"• [{pid}] disabled after "
                    f"{self._failures[pid]} consecutive failures"
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
        return provider_id in self._disabled

    def reset(self, provider_id: str) -> None:
        """Re-enable a provider (e.g. on re-registration after reprovision)."""
        self._disabled.discard(provider_id)
        self._failures.pop(provider_id, None)
