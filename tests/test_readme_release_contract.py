"""Regression tests for the runtime's public install and release contract."""

import re
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

    assert not re.search(r"molecules-workspace-runtime==\d+\.\d+\.\d+", README)
    assert '  "molecules-workspace-runtime"\n' not in README
    assert README.count('"molecules-workspace-runtime==${RUNTIME_VERSION}"') >= 3
    assert (
        "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
        in README
    )

    local_development = README.split("### Local development", 1)[1].split(
        "## Release process", 1
    )[0]
    assert 'pip install -e ".[test]"' in local_development
    assert "--index-url" not in local_development
    assert "--extra-index-url" not in local_development
    assert "https://pypi.org/simple/" in README


def test_release_guidance_matches_tag_only_staging_flow() -> None:
    normalized = " ".join(README.split())

    assert "2 non-author approvals" not in README
    assert "required_approvals: 2" not in AUTO_RELEASE
    assert "required non-author approvals" in normalized
    assert "required status contexts" in normalized
    assert "four maintained workspace templates" in README
    assert "Gitea OCI" in README
    assert "registry.moleculesai.app" in README
    assert not re.search(r"\bECR\b", README)
    assert "staging `runtime_image_pins`" in README
    assert "production freeze is in effect" not in normalized.lower()
    assert "reviewed control-plane change" in normalized
    assert "prod + staging" not in README
    assert "**Loop guard:** the bump commit" not in README
    assert "The version bump in this repo is the gating event" not in README
    assert "no bump commit or bot-actor guard" in normalized
    assert "The published runtime tag is the gating event" in normalized


def test_auto_release_narrative_does_not_promise_production_promotion() -> None:
    normalized = " ".join(AUTO_RELEASE.split())

    assert "prod + staging" not in AUTO_RELEASE
    assert 'fully "in prod"' not in AUTO_RELEASE
    assert "staging runtime_image_pins" in AUTO_RELEASE
    assert "Gitea OCI" in AUTO_RELEASE
    assert "registry.moleculesai.app" in AUTO_RELEASE
    assert not re.search(r"\bECR\b", AUTO_RELEASE)
    assert "Production pin promotion remains separate and explicit" in AUTO_RELEASE
    assert "production freeze is in effect" not in normalized.lower()
    assert "reviewed control-plane change" in normalized


def test_architecture_describes_registry_artifacts_without_implying_public_pypi() -> None:
    assert "ship as a PyPI artifact" not in README
    assert "ship as a wheel and sdist" in README


def test_auto_release_requires_sdk_schema_sync_before_tagging() -> None:
    assert "schema-sync:" in AUTO_RELEASE
    assert "Gate — SDK schema sync" in AUTO_RELEASE
    assert "bash scripts/check-schemas-in-sync.sh" in AUTO_RELEASE
    assert (
        "needs: [classify, unit-tests, responsiveness-e2e, schema-sync]"
        in AUTO_RELEASE
    )


def test_auto_release_tags_the_exact_commit_that_passed_the_gates() -> None:
    assert "TESTED_COMMIT_SHA: ${{ github.sha }}" in AUTO_RELEASE
    assert '--commit-sha "$TESTED_COMMIT_SHA"' in AUTO_RELEASE
    assert "tag at main HEAD" not in AUTO_RELEASE
