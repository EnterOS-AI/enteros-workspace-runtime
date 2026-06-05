"""Helpers for building / mutating the workspace ``AgentCard``.

Kept as their own module so the behavior is unit-testable without booting
the whole runtime (``main.py`` is ``# pragma: no cover``).
"""
from __future__ import annotations

from typing import Iterable

from a2a.types import AgentCard, AgentSkill


def enrich_card_skills(card: AgentCard, loaded_skills: Iterable | None) -> bool:
    """Replace ``card.skills`` with rich metadata from the adapter's loaded
    skills, in place. Pairs with PR #2756: the card was built up front from
    static ``config.skills`` names so /.well-known/agent-card.json could
    serve before ``adapter.setup()`` finishes; this swaps in the richer
    descriptions/tags/examples that ``setup()``'s skill loader produces.

    Returns ``True`` on swap, ``False`` when the swap was skipped or
    failed. Failure cases:
    * ``loaded_skills`` is None / empty — caller didn't load any.
    * Any element doesn't expose ``.metadata.{id,name,description,tags,examples}``
      (a future adapter that doesn't follow the canonical shape).

    Failures DO NOT raise — a malformed ``loaded_skills`` shape would
    otherwise propagate to ``main.py``'s outer ``except Exception``,
    silently degrading an OK boot to the not-configured state. Static
    stubs from ``config.skills`` stay in place; setup() already
    succeeded, the agent works, only the card's skill enrichment is
    degraded. Operator sees a clear log line; tests assert this
    distinction.
    """
    if not loaded_skills:
        return False

    try:
        rich = [
            AgentSkill(
                id=skill.metadata.id,
                name=skill.metadata.name,
                description=skill.metadata.description,
                tags=skill.metadata.tags,
                examples=skill.metadata.examples,
            )
            for skill in loaded_skills
        ]
        # a2a-sdk 1.x AgentCard is a protobuf message: ``card.skills`` is a
        # repeated field that does NOT allow direct assignment
        # (``AttributeError: Assignment not allowed to ... repeated field``).
        # Clear-and-extend is the protobuf-correct in-place swap. The old
        # ``card.skills = rich`` only worked against the conftest stub;
        # against the real SDK it raised AttributeError, which was swallowed
        # below and silently degraded enrichment to the static config stubs —
        # PR #2756's rich metadata never reached the served card. Kept inside
        # the try so the no-raise contract above still holds. Pinned by
        # tests/test_agent_card_contract.py against the genuine a2a.types.
        del card.skills[:]
        card.skills.extend(rich)
    except Exception as enrich_err:  # noqa: BLE001
        print(
            f"Warning: skill metadata enrichment failed (keeping static "
            f"stubs from config.skills): {type(enrich_err).__name__}: {enrich_err}",
            flush=True,
        )
        return False

    return True
