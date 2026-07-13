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


def test_release_tags_the_commit_that_passed_gates_when_main_advances(
    monkeypatch, tmp_path: Path
) -> None:
    """A later merge must not replace the SHA validated by this workflow run."""
    tested_sha = "a" * 40
    later_main_sha = "b" * 40
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.4.0"\n')
    created: dict[str, str] = {}

    monkeypatch.setenv("RELEASE_BOT_TOKEN", "test-token")
    monkeypatch.setattr(
        release,
        "latest_release_tag",
        lambda base, token: ("runtime-v0.4.0", (0, 4, 0)),
    )
    monkeypatch.setattr(release, "tag_exists", lambda base, token, tag: False)
    monkeypatch.setattr(
        release,
        "branch_head_sha",
        lambda *args: later_main_sha,
        raising=False,
    )

    def record_tag(base, token, *, tag, commit_sha, target):
        created.update(tag=tag, commit_sha=commit_sha, target=target)

    monkeypatch.setattr(release, "create_tag", record_tag)

    result = release.main(
        ["--commit-sha", tested_sha, "--pyproject", str(pyproject)]
    )

    assert result == 0
    assert created == {
        "tag": "runtime-v0.4.1",
        "commit_sha": tested_sha,
        "target": "0.4.1",
    }


def test_release_rejects_a_non_commit_sha(monkeypatch, tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.4.0"\n')
    monkeypatch.setenv("RELEASE_BOT_TOKEN", "test-token")

    result = release.main(
        ["--commit-sha", "main", "--pyproject", str(pyproject)]
    )

    assert result == 2
