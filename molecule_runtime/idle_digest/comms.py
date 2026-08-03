"""Stable comms contract for the idle-digest mail providers.

Extracted verbatim from ``providers/mail.py`` as the **D3 mail-plugin seam
precursor** (RFC molecule-core#4413; staged-retirement molecule-core#4450).
These three types — the unavailable signal, the summary DTO, and the source
protocol — are shared between the runtime-owned ``PlatformMailSummarySource``
binding (which STAYS in ``providers/mail.py``) and the mail digest providers'
render half (which moves to ``molecule-ai-plugin-digest-mail`` at v0.2.0).

They live HERE, on a stable module that is NOT scheduled for deletion, so the
mail plugin can ``from molecule_runtime.idle_digest.comms import
CommsSummaryUnavailable`` from a path that survives the eventual delete of
``providers/mail.py``'s render half. Importing from a stable runtime module (not
a vendored copy, not an SDK pip package) also keeps the type IDENTITY the mail
providers' defensive ``s is None`` guard raises.

Purely additive: ``providers/mail.py`` imports and re-exports every name defined
here, so ``PlatformMailSummarySource``, the baked ``SentMailProvider`` /
``InboundMailProvider``, ``controller.build_default_providers``, and the existing
``tests/test_idle_provider_mail.py`` / ``test_idle_provider_plugin_loader.py``
imports are byte-behaviour-identical.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

#: Default no-reply age (seconds) beyond which a sent delegation is flagged
#: "target agent may have an issue" (matches the server default and the
#: delegations hard-deadline default).
DEFAULT_OVERDUE_AFTER_SECONDS = 21600


class CommsSummaryUnavailable(RuntimeError):
    """The comms source could not produce a summary THIS tick.

    Providers let this propagate: the ProviderRunner catches it, marks the
    section FAILED, and the assembler excludes failed sections from the delta
    hash SYMMETRICALLY (contract failed_sections_excluded_from_hash) — a
    transient outage neither fires the digest nor suppresses it, and the
    degraded marker renders. Returning an empty summary instead would conflate
    "comms down" with "no mail" and fire spuriously on the blip + recovery.
    """


@dataclass
class MailSummary:
    """The tenant mail-summary API response, typed."""

    received_unread: int = 0
    replies_unread: int = 0
    #: core#5028 — per-message IDENTITY for the same unread set the two counts
    #: above aggregate: a capped, NEWEST-FIRST tuple of dicts carrying ``id``
    #: (always present, the server row id), ``message_id`` (the a2a messageId
    #: when the sender supplied one), ``sender_id`` and ``method``. Ids only —
    #: never bodies; the D5 "counts + a pull instruction" rule is unchanged.
    #:
    #: ``None`` vs ``()`` IS LOAD-BEARING, do not collapse it:
    #:   None  the server did not project the field — an OLD server. The
    #:         inbound provider degrades to a count-only identity and LOGS a
    #:         warning, because a silent permanent fallback is exactly how
    #:         ``overdue_count`` looked fixed for a month while it wasn't.
    #:   ()    an upgraded server with nothing unread. A fact, not a skew.
    received: Optional[tuple] = None  # of dict: id, message_id, sender_id, method
    sent_awaiting_reply: int = 0
    #: a SAMPLE of the overdue sends, capped server-side for naming offenders.
    overdue: tuple = ()  # of dict: delegation_id, target_workspace_id, age_seconds
    #: the TRUE, UNCAPPED overdue total. Render THIS, never len(overdue) — the
    #: list is capped at 10, so 25 overdue rendered as "10" (under-reporting the
    #: blast radius) and the delta signal froze once the cap saturated.
    overdue_count: int = 0
    overdue_after_seconds: int = DEFAULT_OVERDUE_AFTER_SECONDS


class CommsSummarySource(Protocol):
    """The comms abstraction the mail providers depend on.

    THE plugin seam: today the one implementation reads the platform
    mail-summary API; a future communication-layer plugin provides its own
    implementation and is bound in ``build_default_providers`` — nothing else
    changes. ``fetch()`` returns a summary, or raises
    :class:`CommsSummaryUnavailable` when the source cannot answer THIS tick —
    never an empty summary for an outage (failed-exclusion symmetry).
    """

    async def fetch(self) -> MailSummary:  # pragma: no cover - protocol
        ...
