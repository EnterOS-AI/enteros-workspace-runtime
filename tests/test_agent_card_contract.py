"""Contract test: the workspace ``AgentCard`` (the agent-card / registry
envelope) constructs against the REAL a2a-sdk ``AgentCard`` message — with
the exact kwargs ``main.py`` uses — and a removed/renamed field is REJECTED.

Why this test exists — the agent-card blind spot (same class as #2251)
======================================================================
``main.py`` builds the card the workspace serves at
``/.well-known/agent-card.json`` and sends to the platform registry:

    agent_card = AgentCard(
        name=..., description=..., version=...,
        supported_interfaces=[AgentInterface(protocol_binding=..., url=...)],
        capabilities=AgentCapabilities(streaming=..., push_notifications=...),
        skills=[AgentSkill(...)],
        default_input_modes=[...], default_output_modes=[...],
    )

The whole test suite stubs ``a2a`` in conftest.py, so the stub ``AgentCard``
is a validation-free no-op — every kwarg is accepted. The real a2a-sdk 1.x
``AgentCard`` is a PROTOBUF message that strictly rejects unknown field names
with ``ValueError: Protocol message AgentCard has no "<field>" field``.

That divergence already bit production: a prior code rev passed
``supported_protocols=`` (a removed 0.x field); the stub accepted it, every
unit test passed, and the real constructor crashed the workspace at boot —
the comment at main.py:360-362 documents exactly this ("didn't surface in
the publish-runtime smoke because the smoke only IMPORTS ... never CALLS the
AgentCard constructor"). This test closes that gap: it imports the genuine
a2a.types, constructs the card the way main.py does, and asserts a
removed-field regression FAILS.

If a2a-sdk is not installed, the module SKIPS loudly (CI installs the wheel;
the stubbed unit env does not).
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _import_real_a2a_types():
    """Import the genuine ``a2a.types`` module, evicting the conftest stub
    first and restoring it after (mirrors test_a2a_message_send_contract.py)."""
    saved = {
        k: v for k, v in list(sys.modules.items())
        if k == "a2a" or k.startswith("a2a.")
    }
    for k in saved:
        del sys.modules[k]
    try:
        return importlib.import_module("a2a.types")
    except ModuleNotFoundError:
        return None
    finally:
        for k in [k for k in list(sys.modules) if k == "a2a" or k.startswith("a2a.")]:
            del sys.modules[k]
        sys.modules.update(saved)


_TYPES = _import_real_a2a_types()

pytestmark = pytest.mark.skipif(
    _TYPES is None,
    reason="a2a-sdk not installed — install a2a-sdk[http-server] to run the "
    "AgentCard envelope contract test (CI installs it; the stubbed unit env "
    "does not).",
)


def _build_card_like_main():
    """Construct the card EXACTLY as main.py:364 does, against the real
    a2a.types. Any kwarg the real message doesn't accept raises here."""
    AgentCard = _TYPES.AgentCard
    AgentInterface = _TYPES.AgentInterface
    AgentCapabilities = _TYPES.AgentCapabilities
    AgentSkill = _TYPES.AgentSkill
    return AgentCard(
        name="pm",
        description="team lead",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(protocol_binding="https://a2a.g/v1", url="https://ws.example/a2a")
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[
            AgentSkill(id=n, name=n, description=n, tags=[], examples=[])
            for n in ("coding", "review")
        ],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
    )


def test_main_agent_card_construction_validates():
    """The exact main.py kwargs construct a valid real AgentCard. A future
    rename (e.g. supported_interfaces -> interfaces, or push_notifications ->
    pushNotifications) breaks here BEFORE it ships and crashes a boot."""
    card = _build_card_like_main()
    assert card.name == "pm"
    assert card.version == "1.0.0"
    assert len(card.skills) == 2
    assert card.supported_interfaces[0].url == "https://ws.example/a2a"


def test_removed_supported_protocols_field_is_rejected_regression_guard():
    """Red→green proof: re-introducing the removed 0.x ``supported_protocols``
    kwarg (the field that crashed a boot) MUST raise against the real message.
    This is the assertion that would have caught the original incident in CI."""
    AgentCard = _TYPES.AgentCard
    with pytest.raises((ValueError, TypeError)) as exc:
        AgentCard(
            name="x",
            description="d",
            version="1.0.0",
            supported_protocols=["https://a2a.g/v1"],  # removed in a2a-sdk 1.x
        )
    assert "supported_protocols" in str(exc.value)


def test_enrich_card_skills_produces_valid_card_skills():
    """card_helpers.enrich_card_skills swaps in adapter skill metadata in
    place. Drive it against a REAL AgentCard so the AgentSkill it builds is
    validated by the genuine message, not the stub.

    enrich_card_skills imports AgentCard/AgentSkill at module load — under the
    conftest stub. We re-exec the module against the real a2a.types so its
    AgentSkill construction is the real one.
    """
    import types as _pytypes

    # Temporarily swap the stub a2a.types for the real one, then import a
    # fresh copy of card_helpers bound to it.
    saved = {
        k: v for k, v in list(sys.modules.items())
        if k == "a2a" or k.startswith("a2a.") or k == "molecule_runtime.card_helpers"
    }
    # Evict stub a2a + any cached card_helpers.
    for k in list(saved):
        if k in sys.modules:
            del sys.modules[k]
    try:
        import a2a  # noqa: F401  (real one now)
        card_helpers = importlib.import_module("molecule_runtime.card_helpers")

        card = _build_card_like_main()

        # Minimal adapter-skill shape enrich_card_skills expects:
        # .metadata.{id,name,description,tags,examples}
        def _skill(i, n, d, tags):
            meta = _pytypes.SimpleNamespace(
                id=i, name=n, description=d, tags=tags, examples=[]
            )
            return _pytypes.SimpleNamespace(metadata=meta)

        swapped = card_helpers.enrich_card_skills(
            card, [_skill("seo", "SEO", "search optimization", ["seo"])]
        )
        assert swapped is True
        assert len(card.skills) == 1
        assert card.skills[0].id == "seo"
        assert card.skills[0].description == "search optimization"
    finally:
        for k in [
            k for k in list(sys.modules)
            if k == "a2a" or k.startswith("a2a.") or k == "molecule_runtime.card_helpers"
        ]:
            del sys.modules[k]
        sys.modules.update(saved)
