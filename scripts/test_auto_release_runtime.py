"""Release-version floor regressions for the runtime auto-tagger."""

from pathlib import Path

import auto_release_runtime as release


def test_release_target_honors_a_declared_minor_floor() -> None:
    assert release.release_target((0, 3, 125), "0.4.0") == "0.4.0"


def test_release_target_keeps_incrementing_after_floor_is_published() -> None:
    assert release.release_target((0, 4, 0), "0.4.0") == "0.4.1"


def test_release_target_ignores_an_older_local_fallback() -> None:
    assert release.release_target((0, 3, 125), "0.3.70") == "0.3.126"


def test_project_version_reads_pyproject_floor(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "runtime"\nversion = "1.2.3"\n')
    assert release.project_version(pyproject) == "1.2.3"
