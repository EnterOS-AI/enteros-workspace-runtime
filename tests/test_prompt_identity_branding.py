"""Guardrail: a workspace NEVER introduces itself as a third-party product.

Live incident this file encodes (both containers observed read-only on the prod
box, 2026-07-30, SAME image, SAME hermes runtime):

  enteros-ws-test1-22efcb5a227b   /configs/prompts/concierge.md  PRESENT
                                  /configs/.hermes/SOUL.md  29790 B
                                  line 54: "# You are test1 Agent — the Org Concierge"

  enteros-ws-test2-e2c8ff1f0ae0   /configs/prompts/            ABSENT ENTIRELY
                                  /configs/.hermes/SOUL.md  21051 B
                                  line 54: "# Hermes Agent"
                                           "**Role:** Nous Research hermes-agent — the
                                            self-improving AI agent with built-in
                                            terminal, file ops, web search, ..."

test2's persona file never arrived. ``build_system_prompt`` printed a bare
warning and carried on; the prompt still got an identity, because ``AGENTS.md``
is in ``DEFAULT_MEMORY_SNAPSHOT_FILES`` and is auto-loaded as a memory snapshot.
``agents_md.generate_agents_md`` had built that AGENTS.md from config.yaml's
``name``/``role``/``description``, which for an unrendered hermes template are
the UPSTREAM VENDOR's. Net effect: a paying customer's workspace introduced
itself as "I'm Hermes, a self-improving AI agent from Nous Research."

The tests below are that live pair, reproduced over ``build_system_prompt``:
persona present → the role identity is there; persona absent → NO vendor
identity survives and the branded platform default is present instead.

Every test carries an explicit NON-VACUITY precondition block asserting the
input state it believes it set up (and, in the absent case, asserting the
AGENTS.md snapshot really was consumed) — a guard that passes because it
covered nothing is not a guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from molecule_runtime import branding
from molecule_runtime.prompt import build_system_prompt

# Verbatim shape of the live AGENTS.md that hijacked test2's identity — the
# exact output of agents_md.generate_agents_md() over an unrendered hermes
# template's config.yaml (see /configs/.hermes/SOUL.md line 54 on
# enteros-ws-test2-e2c8ff1f0ae0).
HERMES_TEMPLATE_CARD = """\
# Hermes Agent

**Role:** Nous Research hermes-agent — the self-improving AI agent with built-in \
terminal, file ops, web search, memory, skills, and cross-session recall. This \
workspace runs the real `hermes-agent` (github.com/NousResearch/hermes-agent) behind \
an A2A bridge.

## Description
Nous Research hermes-agent — the self-improving AI agent with built-in terminal, \
file ops, web search, memory, skills, and cross-session recall.

## A2A Endpoint
http://localhost:8000/a2a

## MCP Tools
- task_add
- task_complete
"""

# Strings that MUST NOT reach a customer as this workspace's identity. These are
# the upstream runtime vendor's product identity, not ours.
VENDOR_IDENTITY_MARKERS = (
    "Nous Research",
    "hermes-agent",
    "# Hermes Agent",
    "self-improving AI agent",
)

# The live pair's role identity (test1's concierge.md first heading).
ROLE_IDENTITY = "# You are test1 Agent - the Org Concierge"


def _write(base: Path, rel: str, text: str) -> Path:
    f = base / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


@pytest.fixture(autouse=True)
def _kernel_off(monkeypatch):
    """Read memory snapshots from ``config_path`` (tmp_path), not the durable
    mailbox dir, so these tests are hermetic and the AGENTS.md we write is the
    AGENTS.md that gets loaded."""
    monkeypatch.setenv("MOLECULE_MAILBOX_KERNEL", "0")
    import molecule_runtime.mailbox_dir as mailbox_dir

    assert mailbox_dir.kernel_enabled() is False, (
        "NON-VACUITY: the kernel-off fixture did not take effect, so the "
        "memory-snapshot source is not tmp_path and these tests prove nothing"
    )


def test_persona_present_prompt_carries_the_role_identity(tmp_path):
    """The test1 leg: persona delivered → the workspace's real role is the identity."""
    _write(tmp_path, "prompts/concierge.md", ROLE_IDENTITY + "\n\nYou are the org concierge.")
    _write(tmp_path, "AGENTS.md", HERMES_TEMPLATE_CARD)

    # ── NON-VACUITY: the inputs really are what this test claims ──────────────
    assert (tmp_path / "prompts" / "concierge.md").exists()
    assert (tmp_path / "AGENTS.md").exists()
    assert "Nous Research" in HERMES_TEMPLATE_CARD, (
        "NON-VACUITY: the vendor card fixture lost its vendor text, so the "
        "absent-persona test below could pass trivially"
    )

    out = build_system_prompt(
        str(tmp_path), "ws-test1", [], [],
        prompt_files=["prompts/concierge.md"], a2a_mcp=False,
    )

    assert len(out) > 200, "NON-VACUITY: no prompt was assembled at all"
    assert ROLE_IDENTITY in out


def test_persona_absent_yields_branded_default_never_a_vendor_identity(tmp_path):
    """The test2 leg: persona NEVER arrived.

    The assembled prompt must not present the workspace as the upstream runtime
    vendor's product, and must carry a correctly-branded platform default in the
    role slot instead.
    """
    _write(tmp_path, "AGENTS.md", HERMES_TEMPLATE_CARD)

    # ── NON-VACUITY: the inputs really are what this test claims ──────────────
    assert not (tmp_path / "prompts").exists(), (
        "NON-VACUITY: this test only means anything if the persona is ABSENT"
    )
    assert (tmp_path / "AGENTS.md").exists(), (
        "NON-VACUITY: without the vendor AGENTS.md on disk there is nothing to "
        "hijack the identity and the assertions below are trivially satisfied"
    )

    out = build_system_prompt(
        str(tmp_path), "ws-test2", [], [],
        prompt_files=["prompts/concierge.md"], a2a_mcp=False,
    )

    # NON-VACUITY: a prompt really was assembled, AND the AGENTS.md snapshot
    # really was consumed by this build (its non-identity sections survive). If
    # AGENTS.md were simply never read, the vendor assertions below would pass
    # for the wrong reason.
    assert len(out) > 200, "NON-VACUITY: no prompt was assembled at all"
    assert "http://localhost:8000/a2a" in out, (
        "NON-VACUITY: the AGENTS.md snapshot was not consumed by this build, so "
        "the no-vendor-identity assertions below prove nothing"
    )

    for marker in VENDOR_IDENTITY_MARKERS:
        assert marker not in out, (
            f"third-party vendor identity {marker!r} reached the assembled system "
            "prompt — this is the live enteros-ws-test2 defect"
        )

    product = branding.product_display_name()
    assert product not in ("", None)
    assert product in out, (
        "no branded platform default in the role slot: when the persona does not "
        "arrive the workspace must still know it is a workspace on this product"
    )


def test_absent_persona_default_is_not_injected_when_the_persona_is_present(tmp_path):
    """The branded default is a FALLBACK, not a second identity.

    A workspace whose role prompt did arrive must not also be told "no role
    prompt file was delivered" — that would contradict its real persona.
    """
    _write(tmp_path, "prompts/concierge.md", ROLE_IDENTITY)

    assert (tmp_path / "prompts" / "concierge.md").exists()  # NON-VACUITY

    with_persona = build_system_prompt(
        str(tmp_path), "ws-a", [], [],
        prompt_files=["prompts/concierge.md"], a2a_mcp=False,
    )
    without_persona = build_system_prompt(
        str(tmp_path), "ws-b", [], [],
        prompt_files=["prompts/missing-on-purpose.md"], a2a_mcp=False,
    )

    # NON-VACUITY: the two builds really did differ in persona availability.
    assert ROLE_IDENTITY in with_persona
    assert ROLE_IDENTITY not in without_persona

    from molecule_runtime.prompt import DEFAULT_ROLE_PROMPT

    assert DEFAULT_ROLE_PROMPT.strip(), "NON-VACUITY: the default role block is empty"
    assert DEFAULT_ROLE_PROMPT in without_persona
    assert DEFAULT_ROLE_PROMPT not in with_persona


def test_missing_role_prompt_file_is_logged_as_a_loud_error(tmp_path, caplog):
    """A missing persona must be an ERROR that names the consequence — not a
    bare ``print`` that no operator and no log level will ever surface."""
    caplog.set_level("DEBUG")

    build_system_prompt(
        str(tmp_path), "ws-c", [], [],
        prompt_files=["prompts/concierge.md"], a2a_mcp=False,
    )

    errors = [
        r for r in caplog.records
        if r.levelname == "ERROR" and r.name == "molecule_runtime.prompt"
    ]
    assert errors, "NON-VACUITY: molecule_runtime.prompt logged nothing at ERROR"
    joined = "\n".join(r.getMessage() for r in errors)
    assert "prompts/concierge.md" in joined.replace("\\", "/"), (
        "the error does not name WHICH file failed to arrive"
    )
    assert "identity" in joined.lower(), (
        "the error does not name the CONSEQUENCE (the workspace boots without "
        "its role identity)"
    )


# ── Source ratchet ───────────────────────────────────────────────────────────
# The product was renamed once already, precisely because hardcoded product
# strings were scattered everywhere. This makes the next rename impossible to
# silently rot: the prompt-building sources may not spell ANY product display
# name — old or new. The name comes from the vendored branding SSOT.

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Files whose entire job is to assemble customer-visible prompt text (plus the
# branding loader itself, which must not shortcut its own SSOT).
_RATCHETED_SOURCES = (
    "molecule_runtime/prompt.py",
    "molecule_runtime/branding.py",
)

# Every product DISPLAY name this platform has ever had, in any casing/spacing.
# Scope note: this bans display names, not the lowercase `resource_prefix` brand
# token — that token legitimately appears in these files when a comment cites a
# live container name as incident evidence (e.g. enteros-ws-test2-…), and it is
# never what gets rendered into customer-facing prompt text.
_BANNED_PRODUCT_LITERALS = (
    re.compile(r"molecule\s+ai\b", re.IGNORECASE),
    re.compile(r"molecules\s*ai\b", re.IGNORECASE),
    re.compile(r"enter[\s-]+os\b", re.IGNORECASE),
    re.compile(r"\bEnterOS\b"),
)


@pytest.mark.parametrize("rel", _RATCHETED_SOURCES)
def test_prompt_sources_never_hardcode_a_product_name(rel):
    path = _REPO_ROOT / rel
    assert path.is_file(), f"NON-VACUITY: ratcheted source {rel} does not exist"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"NON-VACUITY: ratcheted source {rel} is empty"

    for pattern in _BANNED_PRODUCT_LITERALS:
        hits = [
            f"{rel}:{i}: {line.strip()}"
            for i, line in enumerate(text.splitlines(), 1)
            if pattern.search(line)
        ]
        assert not hits, (
            "hardcoded product literal reintroduced — read it from the vendored "
            "branding SSOT (molecule_runtime/branding.py) instead:\n"
            + "\n".join(hits)
        )


def test_ratchet_regexes_actually_match_something():
    """NON-VACUITY for the ratchet itself: a typo'd regex that matches nothing
    would let every literal through while the test above stayed green."""
    samples = ("Molecule AI platform", "MoleculesAI", "Enter OS Server", "EnterOS")
    for pattern, sample in zip(_BANNED_PRODUCT_LITERALS, samples):
        assert pattern.search(sample), f"{pattern.pattern!r} failed to match {sample!r}"


def test_branding_contract_is_vendored_and_drift_gated():
    """The vendored SSOT mirror must exist, ship in the wheel, and be listed in
    the drift gate. A mirror nobody diffs is a fork."""
    vendored = _REPO_ROOT / "molecule_runtime" / "contracts" / "branding.contract.json"
    assert vendored.is_file(), "branding SSOT mirror is not vendored"

    gate = (_REPO_ROOT / "scripts" / "check-schemas-in-sync.sh").read_text(encoding="utf-8")
    assert "molecule_runtime/contracts/branding.contract.json" in gate, (
        "the vendored branding contract is not wired into "
        "scripts/check-schemas-in-sync.sh — it could drift from sdk main forever"
    )
    assert "contracts/branding/branding.contract.json" in gate, (
        "the drift gate does not name the sdk-side source path"
    )

    provenance = (
        _REPO_ROOT / "molecule_runtime" / "contracts" / "PROVENANCE.md"
    ).read_text(encoding="utf-8")
    assert "branding.contract.json" in provenance, "no PROVENANCE entry for the mirror"

    # And it must be readable the way production reads it: offline, out of the
    # installed package, via importlib.resources.
    assert branding.product_display_name().strip()
