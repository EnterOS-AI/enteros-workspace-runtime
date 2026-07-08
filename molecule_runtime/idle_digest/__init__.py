"""Idle-digest assembler (task #219) — the runtime consumer of the SDK
``contracts/idle-prompt`` layer.

The canonical assembler *implementation* the contract names
(``ownership.assembler_owner = molecule_runtime``). This package is the pure
engine + provider machinery; the idle *controller* that wires it into the boot
loop behind ``MOLECULE_MAILBOX_KERNEL`` is a follow-up. Nothing here touches
the loop, the network, or the filesystem.

Public surface:
  * :mod:`~molecule_runtime.idle_digest.contract` — envelope + enums + Policy
  * :mod:`~molecule_runtime.idle_digest.assembler` — pure engine (hash / sort /
    empty / limits / render / assemble + delta signature + fire decision)
  * :mod:`~molecule_runtime.idle_digest.provider` — DigestProvider protocol +
    failure-safe ProviderRunner
"""
from __future__ import annotations

from .assembler import (
    AssembledDigest,
    ContributionError,
    assemble,
    body_is_empty,
    check_reserved_id,
    content_hash,
    render,
    should_fire,
    signature,
    sort_contributions,
    validate,
)
from .controller import (
    FIRED,
    SKIPPED,
    SLEEP,
    UNCHANGED,
    IdleDigestController,
    build_default_providers,
)
from .contract import (
    CONTRACT_SCHEMA_VERSION,
    PINNED_PROVIDER_ID,
    RESERVED_PROVIDER_IDS,
    AgeBand,
    Band,
    Contribution,
    Policy,
    PullInstruction,
    PreviewItem,
    Urgency,
)
from .provider import DigestProvider, GatherResult, ProviderRunner

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "PINNED_PROVIDER_ID",
    "RESERVED_PROVIDER_IDS",
    "AgeBand",
    "AssembledDigest",
    "Band",
    "Contribution",
    "ContributionError",
    "DigestProvider",
    "FIRED",
    "IdleDigestController",
    "SKIPPED",
    "SLEEP",
    "UNCHANGED",
    "build_default_providers",
    "GatherResult",
    "Policy",
    "PreviewItem",
    "ProviderRunner",
    "PullInstruction",
    "Urgency",
    "assemble",
    "body_is_empty",
    "check_reserved_id",
    "content_hash",
    "render",
    "should_fire",
    "signature",
    "sort_contributions",
    "validate",
]
