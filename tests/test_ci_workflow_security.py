"""Static release/CI credential-boundary regressions."""

from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".gitea" / "workflows" / "ci.yml"


def test_ci_checkouts_never_persist_repository_credentials() -> None:
    workflow = WORKFLOW.read_text()
    checkout_count = workflow.count("uses: actions/checkout@")

    assert checkout_count > 0
    assert workflow.count("persist-credentials: false") == checkout_count


def test_live_observability_secrets_are_dispatch_only_on_main() -> None:
    workflow = WORKFLOW.read_text()
    obs_job = workflow[workflow.index("\n  obs-smoke:\n") :]

    assert "github.event_name == 'workflow_dispatch'" in obs_job
    assert "github.ref == 'refs/heads/main'" in obs_job
    assert "INFISICAL_TOKEN: ${{ secrets.INFISICAL_TOKEN }}" in obs_job
