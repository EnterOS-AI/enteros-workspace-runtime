"""Official default-installed idle-digest providers (task #219).

Each provider is a removable lego satisfying the
:class:`~molecule_runtime.idle_digest.provider.DigestProvider` protocol. Phase 1
ships identity-capabilities (this package), task-queue, and goal-state; phase 2
adds sent-folder, inbound-a2a, delegation-results, and scheduler.
"""
from __future__ import annotations

from .goal import GoalStateProvider
from .identity import IdentityCapabilitiesProvider

__all__ = ["GoalStateProvider", "IdentityCapabilitiesProvider"]
