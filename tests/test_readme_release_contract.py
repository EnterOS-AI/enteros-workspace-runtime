"""Regression tests for the runtime's public install and release contract."""

from pathlib import Path


README = (Path(__file__).parents[1] / "README.md").read_text()
AUTO_RELEASE = (
    Path(__file__).parents[1] / ".gitea/workflows/auto-release.yml"
).read_text()


def test_install_guidance_uses_canonical_private_distribution() -> None:
    assert "pip install molecule-ai-workspace-runtime" not in README
    assert "pipx install molecule-ai-workspace-runtime" not in README
    assert "molecule-ai-workspace-runtime==X.Y.Z" not in README
    assert "molecule-ai-workspace-runtime==0.2.0" not in README

    assert "molecules-workspace-runtime==0.4.0" in README
    assert (
        "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
        in README
    )
    assert "https://pypi.org/simple/" in README


def test_release_guidance_matches_tag_only_staging_flow() -> None:
    normalized = " ".join(README.split())

    assert "four maintained workspace templates" in README
    assert "staging `runtime_image_pins`" in README
    assert "prod + staging" not in README
    assert "**Loop guard:** the bump commit" not in README
    assert "The version bump in this repo is the gating event" not in README
    assert "no bump commit or bot-actor guard" in normalized
    assert "The published runtime tag is the gating event" in normalized


def test_auto_release_narrative_does_not_promise_production_promotion() -> None:
    assert "prod + staging" not in AUTO_RELEASE
    assert 'fully "in prod"' not in AUTO_RELEASE
    assert "staging runtime_image_pins" in AUTO_RELEASE
    assert "Production pin promotion remains separate and explicit" in AUTO_RELEASE


def test_auto_release_requires_sdk_schema_sync_before_tagging() -> None:
    assert "schema-sync:" in AUTO_RELEASE
    assert "Gate — SDK schema sync" in AUTO_RELEASE
    assert "bash scripts/check-schemas-in-sync.sh" in AUTO_RELEASE
    assert (
        "needs: [classify, unit-tests, responsiveness-e2e, schema-sync]"
        in AUTO_RELEASE
    )
