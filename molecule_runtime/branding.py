"""Product branding, read from the vendored branding SSOT contract.

The product this runtime belongs to has been renamed once already, and that
rename happened *because* the display name was hardcoded in dozens of places.
Every one of those literals is a future stale string. So no module in this
package may spell the product name — including this one. The name is read at
import time from the byte-for-byte SSOT mirror

    molecule_runtime/contracts/branding.contract.json

(vendored from ``molecule-ai-sdk`` ``contracts/branding/branding.contract.json``
— see ``molecule_runtime/contracts/PROVENANCE.md``; drift from sdk ``main`` fails
``scripts/check-schemas-in-sync.sh``). The file ships inside the wheel via
``[tool.setuptools.package-data]`` and is read with ``importlib.resources``, so
this is an OFFLINE read: no clone, no token, no network, works in a locked-down
workspace container. Same pattern as ``manifest_ssot`` and the idle-digest
native-plugin registry.

Only ``tier1`` is exposed. ``tier1`` is the flip-safe internal brand-token set —
the SSOT's own ``$comment`` marks it "ALREADY FLIPPED", i.e. safe to render into
customer-visible text. ``tier2`` (customer DNS, persisted org rows, pinned image
pull paths) is mid-staged-migration and must NOT be surfaced as branding.

Failure posture — deliberately NOT fail-closed and NOT fail-hard:

* This is read at import time by ``prompt.py`` to build the system-prompt frame.
  Raising here would crash workspace boot, and a workspace that cannot boot is
  strictly worse than one with a slightly generic frame.
* But the fallback must not re-introduce the defect this module exists to kill.
  So the fallback is ``_UNBRANDED_FALLBACK`` — a generic English noun phrase,
  deliberately NOT a product name of any kind (not this product's, and above all
  not some third party's). A degraded read yields a *brand-free* prompt, never a
  *wrongly-branded* one. It is logged at ERROR because an unreadable vendored
  contract means the wheel is malformed.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Path of the vendored SSOT mirror, relative to the ``molecule_runtime`` package
# root — the same "contracts/<file>.json" resource-name shape the idle-digest
# native-registry loader uses.
BRANDING_RESOURCE = "contracts/branding.contract.json"

# NOT a product name. A generic noun phrase used only when the vendored contract
# cannot be read, so a degraded runtime presents no brand rather than the wrong
# brand. Never widen this to an actual product name — that is precisely the
# hardcoded literal this module exists to eliminate, and
# tests/test_prompt_identity_branding.py's source ratchet will fail the build.
_UNBRANDED_FALLBACK = "the platform"


def _load_tier1() -> dict:
    """Read ``tier1`` out of the vendored branding contract. ``{}`` on any error."""
    try:
        from importlib import resources

        raw = (
            resources.files("molecule_runtime")
            .joinpath(BRANDING_RESOURCE)
            .read_text(encoding="utf-8")
        )
        tier1 = json.loads(raw).get("tier1")
        return tier1 if isinstance(tier1, dict) else {}
    except Exception as exc:  # noqa: BLE001 — never raise into workspace boot
        logger.error(
            "branding: could not read the vendored branding SSOT (%s: %s) from "
            "molecule_runtime/%s. The wheel is missing or has a malformed copy of "
            "the contract. CONSEQUENCE: every system prompt this process builds "
            "will name the product generically as %r instead of its real display "
            "name. Re-vendor per molecule_runtime/contracts/PROVENANCE.md.",
            type(exc).__name__,
            exc,
            BRANDING_RESOURCE,
            _UNBRANDED_FALLBACK,
        )
        return {}


# Read ONCE per process. The contract is packaged data — it cannot change under a
# running workspace — and prompt.py needs it at import time.
_TIER1: dict = _load_tier1()


def _tier1_string(key: str) -> str | None:
    value = _TIER1.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def product_display_name() -> str:
    """The customer-facing product display name (``tier1.product_display_name``).

    This is what a workspace calls the platform it runs on, in text an end user
    reads. Returns :data:`_UNBRANDED_FALLBACK` (a brand-free noun phrase, never a
    third-party product name) if the vendored contract could not be read.
    """
    name = _tier1_string("product_display_name")
    if name is not None:
        return name
    logger.error(
        "branding: vendored branding SSOT has no usable tier1.product_display_name; "
        "falling back to the brand-free placeholder %r. System prompts will not "
        "name the product.",
        _UNBRANDED_FALLBACK,
    )
    return _UNBRANDED_FALLBACK


def product_server_display_name() -> str:
    """``tier1.product_server_display_name`` — the self-hosted server product name.

    Falls back to :func:`product_display_name` (which is itself brand-free-safe)
    rather than to a literal.
    """
    return _tier1_string("product_server_display_name") or product_display_name()
