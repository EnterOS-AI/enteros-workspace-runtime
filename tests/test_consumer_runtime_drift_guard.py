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

    findings = guard.find_runtime_drift("molecule-core", repo)

    assert [(finding.path, finding.reason) for finding in findings] == [
        (
            "workspace/",
            "top-level workspace/ runtime tree is forbidden; use the runtime package",
        )
    ]


def test_detects_nested_vendored_molecule_runtime_package(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-workspace-template-hermes"
    (repo / "vendor" / "molecule_runtime").mkdir(parents=True)

    findings = guard.find_runtime_drift("molecule-ai-workspace-template-hermes", repo)

    assert [(finding.path, finding.reason) for finding in findings] == [
        (
            "vendor/molecule_runtime/",
            "vendored molecule_runtime/ package is forbidden; import the SSOT package",
        )
    ]


def test_allows_runtime_pins_and_workspace_path_mentions(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-workspace-template-codex"
    repo.mkdir()
    (repo / ".runtime-version").write_text("0.2.0\n")
    (repo / "requirements.txt").write_text("molecule-ai-workspace-runtime==0.2.0\n")
    (repo / "README.md").write_text("Mount files at /workspace and import molecule_runtime.\n")

    assert guard.find_runtime_drift("molecule-ai-workspace-template-codex", repo) == []
