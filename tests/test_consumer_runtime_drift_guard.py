from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_consumer_runtime_drift.py"
SPEC = importlib.util.spec_from_file_location("check_consumer_runtime_drift", SCRIPT_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


def test_detects_top_level_workspace_runtime_tree(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-core"
    (repo / "workspace").mkdir(parents=True)

    import check_consumer_runtime_drift as guard

    assert [(finding.path, finding.reason) for finding in guard.find_runtime_drift(
        "molecule-core", repo,
        runtime_root=Path(__file__).resolve().parents[1]
    )] == [
        (
            "workspace/",
            "top-level workspace/ runtime tree is forbidden; use the runtime package",
        )
    ]


def test_detects_nested_vendored_molecule_runtime_package(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-workspace-template-hermes"
    (repo / "vendor" / "molecule_runtime").mkdir(parents=True)

    import check_consumer_runtime_drift as guard

    assert [(finding.path, finding.reason) for finding in guard.find_runtime_drift(
        "molecule-ai-workspace-template-hermes", repo,
        runtime_root=Path(__file__).resolve().parents[1]
    )] == [
        (
            "vendor/molecule_runtime/",
            "vendored molecule_runtime/ package is forbidden; import the SSOT package",
        )
    ]


def test_detects_runtime_pin_drift(tmp_path: Path) -> None:
    """DriftFinding fires when .runtime-version differs from SSOT (runtime#53)."""
    repo = tmp_path / "molecule-ai-workspace-template-claude-code"
    repo.mkdir()
    (repo / ".runtime-version").write_text("0.2.1\n")  # stale drift from SSOT 0.3.6

    import check_consumer_runtime_drift as guard

    findings = guard.find_runtime_drift(
        "molecule-ai-workspace-template-claude-code", repo,
        runtime_root=Path(__file__).resolve().parents[1],
    )
    assert len(findings) == 1
    assert findings[0].path == ".runtime-version"
    assert "runtime pin drift" in findings[0].reason
    assert "0.2.1" in findings[0].reason


def test_allows_runtime_pin_matching_ssot(tmp_path: Path) -> None:
    """No drift finding when .runtime-version matches SSOT (even if older)."""
    repo = tmp_path / "molecule-ai-workspace-template-codex"
    repo.mkdir()

    import check_consumer_runtime_drift as guard

    # SSOT 0.3.6 is fresh; use that version so no drift.
    (repo / ".runtime-version").write_text(guard.current_runtime_version(
        Path(__file__).resolve().parents[1]
    ) + "\n")
    (repo / "requirements.txt").write_text("molecule-ai-workspace-runtime==0.3.6\n")
    (repo / "README.md").write_text("Mount files at /workspace and import molecule_runtime.\n")

    assert guard.find_runtime_drift(
        "molecule-ai-workspace-template-codex", repo,
        runtime_root=Path(__file__).resolve().parents[1],
    ) == []


def test_clone_consumers_retries_on_transient_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """clone_consumers retries clone on transient failure (RCA #52 Finding 2)."""
    import subprocess

    call_count = 0

    def flaky_clone(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return type("Result", (), {"returncode": 128, "stderr": "transient error", "stdout": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", flaky_clone)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    import check_consumer_runtime_drift as guard
    guard.clone_consumers(workdir, ("molecule-core",), gitea_url="https://git.moleculesai.app", token="fake-token")
    assert call_count == 3, f"expected 3 attempts, got {call_count}"


def test_clone_consumers_never_puts_token_in_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GIT_ASKPASS path: token must not appear in git clone argv or remote URL (runtime#86)."""
    import subprocess

    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture_run(*args: object, **kwargs: object) -> object:
        captured.append((args, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", capture_run)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    import check_consumer_runtime_drift as guard
    guard.clone_consumers(workdir, ("molecule-core",), gitea_url="https://git.moleculesai.app", token="s3cr3t-t0k3n")

    assert len(captured) == 1
    cmd = captured[0][0][0]
    env = captured[0][1].get("env") or {}
    cmd_str = " ".join(str(c) for c in cmd)
    assert "s3cr3t-t0k3n" not in cmd_str, "token leaked into subprocess argv"
    assert "x-access-token" not in cmd_str, "username leaked into subprocess argv"
    assert env.get("GIT_ASKPASS") is not None, "GIT_ASKPASS not set in clone env"
