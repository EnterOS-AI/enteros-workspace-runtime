"""Keep packaged operator guidance aligned with the current Canvas/tool surfaces."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
RUNTIME_ROOT = REPO_ROOT / "molecule_runtime"


def test_workspace_token_guidance_names_current_canvas_surface() -> None:
    """The retired generic Tokens tab must not return in packaged guidance."""
    findings: list[str] = []
    for path in sorted(RUNTIME_ROOT.rglob("*.py")):
        text = path.read_text()
        if "canvas → Tokens" in text or re.search(
            r"(?<!Workspace )Tokens tab", text
        ):
            findings.append(str(path.relative_to(REPO_ROOT)))

    assert not findings, (
        "retired Canvas token guidance found in: " + ", ".join(findings)
    )

    for relative_path in (
        "molecule_runtime/a2a_client.py",
        "molecule_runtime/mcp_doctor.py",
        "molecule_runtime/mcp_heartbeat.py",
        "molecule_runtime/mcp_workspace_resolver.py",
    ):
        text = (REPO_ROOT / relative_path).read_text()
        assert "Workspace Tokens" in text and "Settings" in text, (
            f"{relative_path} does not name the current token surface"
        )


def test_external_mcp_tool_description_tracks_registry_without_fixed_count() -> None:
    """Tool additions must not make the console-script description stale again."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()

    assert not re.search(r"\bSame \d+ platform tools\b", pyproject)
    assert "registry-backed" in pyproject
    assert "platform tool contract" in pyproject


def test_platform_tool_guide_references_current_sources_and_tests() -> None:
    guide = (RUNTIME_ROOT / "platform_tools" / "README.md").read_text()
    registry = (RUNTIME_ROOT / "platform_tools" / "registry.py").read_text()

    for text in (guide, registry):
        assert "workspace/tests/test_platform_tools.py" not in text
        assert "test_platform_tools.py" not in text
        assert "Search workspace/" not in text

    assert "molecule_runtime/mcp_tools.py" in guide
    assert "tests/test_mcp_ssot.py" in guide
    assert "tests/test_executor_helpers.py" in guide


def test_packaged_comments_do_not_point_at_retired_monorepo_paths() -> None:
    findings: list[str] = []
    for path in sorted(RUNTIME_ROOT.rglob("*.py")):
        text = path.read_text()
        if re.search(r"(?<![-/])\bworkspace/", text):
            findings.append(str(path.relative_to(REPO_ROOT)))

    assert not findings, (
        "retired monorepo workspace/ paths found in: " + ", ".join(findings)
    )


def test_standalone_repo_guidance_does_not_claim_mirror_status() -> None:
    paths = (
        RUNTIME_ROOT / "a2a_tools.py",
        RUNTIME_ROOT / "a2a_tools_identity.py",
        RUNTIME_ROOT / "runtime_wedge.py",
        RUNTIME_ROOT / "main.py",
    )
    combined = "\n".join(path.read_text() for path in paths)

    assert "mirror-only" not in combined
    assert "wheel mirror" not in combined
    assert "molecule-ai-workspace-runtime`" not in combined
    assert "runtime-pin-compat.yml" not in combined


def test_dated_publish_verification_is_explicitly_non_operational() -> None:
    audit = (
        RUNTIME_ROOT / "audit" / "PUBLISH_RUNTIME_VERIFY_2026-05-11.md"
    ).read_text()

    assert "Historical record" in audit
    assert "does not trigger" in audit


def test_secret_scan_comment_points_at_current_gitea_source() -> None:
    workflow = (REPO_ROOT / ".gitea" / "workflows" / "secret-scan.yml").read_text()

    assert ".github/workflows/secret-scan.yml" not in workflow
    assert "@staging" not in workflow
    assert "molecule-ai/molecule-core/.gitea/workflows/secret-scan.yml@main" in workflow


def test_event_log_comments_do_not_claim_unwired_platform_consumers() -> None:
    config = (RUNTIME_ROOT / "config.py").read_text()
    event_log = (RUNTIME_ROOT / "event_log.py").read_text()
    adapter = (RUNTIME_ROOT / "adapter_base.py").read_text()

    normalized = " ".join("\n".join((config, event_log, adapter)).split())

    assert "no production append/query callers are wired" in normalized
    assert "Core's `/activity` endpoint reads its own `activity_logs`" in normalized
    assert "does not feed Core's `/activity` endpoint or Canvas Activity" in normalized
    assert "is not exposed through Core's `/activity` endpoint" in normalized
    assert "does not silence Canvas Activity" in normalized

    for stale in (
        "canvas Activity and platform `/activity` paths consume",
        "external readers — the canvas Activity tab",
        "Operators who pick this backend opt out of the canvas Activity tab",
        "Readers query the buffer via the platform's",
        "disabled`` returns a no-op log so the canvas Activity tab is silent",
        "so the canvas can group by prefix",
    ):
        assert stale not in normalized


def test_public_distribution_history_is_not_presented_as_live_state() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()

    assert "is SQUATTED on public PyPI" not in pyproject
    assert "as of 2026-07-14" in pyproject
    assert "currently returns 404" in pyproject


def test_actions_comments_match_current_gitea_capabilities() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    auto_release = (
        REPO_ROOT / ".gitea" / "workflows" / "auto-release.yml"
    ).read_text()
    secret_scan = (
        REPO_ROOT / ".gitea" / "workflows" / "secret-scan.yml"
    ).read_text()
    combined = "\n".join((readme, auto_release, secret_scan))

    for stale in (
        "Gitea has no `workflow_run`",
        "NO `workflow_run`",
        "1.22.x..1.26.2",
        "until Gitea is upgraded",
    ):
        assert stale not in combined

    assert "supports `workflow_run`" in " ".join(readme.split())
    assert "supports `workflow_run`" in " ".join(auto_release.split())
    assert "deliberate repo-local copy" in secret_scan
    assert "collaborative-owner access" in secret_scan


def test_bundled_internal_path_hook_uses_current_repo_guidance() -> None:
    hook = (RUNTIME_ROOT / "scripts" / "pre-commit-checks.sh").read_text()

    assert "git.moleculesai.app/molecule-ai/molecule-core" in hook
    assert "git.moleculesai.app/molecule-ai/internal" in hook
    assert "Molecule-AI/molecule-monorepo" not in hook
    assert "Molecule-AI/molecule-core" not in hook
    assert "gh repo clone Molecule-AI/internal" not in hook
    assert "gh pr create" not in hook
    assert ".github/workflows/block-internal-paths.yml" not in hook


def test_runtime_upload_filename_sanitizers_stay_aligned() -> None:
    from molecule_runtime.inbox_uploads import sanitize_filename as external
    from molecule_runtime.internal_chat_uploads import sanitize_filename as internal

    samples = (
        "report final.pdf",
        "../unsafe/name?.txt",
        ".",
        "a" * 120 + ".png",
    )
    assert [external(name) for name in samples] == [internal(name) for name in samples]
