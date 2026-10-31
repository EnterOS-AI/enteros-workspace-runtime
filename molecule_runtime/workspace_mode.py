"""Whether this workspace wakes itself (24/7) or runs only when something calls it.

``MOLECULE_CONTINUOUS`` unset or truthy keeps today's behavior: the idle digest
re-wakes the agent ``idle_fire_after_seconds`` after every turn while its digest
has changed. Set explicitly falsy, the runtime arms no idle self-wake at all —
neither the idle digest nor the legacy static idle loop. The workspace still
runs every message it receives (user chat, a peer's delegation) and every
schedule its trigger daemon fires; it only stops waking itself.
"""
from __future__ import annotations

import os

from molecule_runtime.mailbox_dir import _FALSY

CONTINUOUS_ENV = "MOLECULE_CONTINUOUS"


def continuous() -> bool:
    """True unless ``MOLECULE_CONTINUOUS`` is set explicitly falsy. Read live (not cached)."""
    return os.environ.get(CONTINUOUS_ENV, "").strip().lower() not in _FALSY
