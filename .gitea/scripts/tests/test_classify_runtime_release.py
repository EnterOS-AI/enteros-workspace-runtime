import importlib.util
import pathlib
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).parents[1] / "classify_runtime_release.py"
SPEC = importlib.util.spec_from_file_location("classify_runtime_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
classify_runtime_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classify_runtime_release)


class ReleaseRequiredTest(unittest.TestCase):
    def test_changed_paths_disables_rename_detection(self):
        completed = mock.Mock(stdout="molecule_runtime/main.py\ntests/main.py\n")
        with mock.patch.object(
            classify_runtime_release.subprocess, "run", return_value=completed
        ) as run:
            paths = classify_runtime_release.changed_paths("a" * 40, "b" * 40)

        self.assertEqual(paths, ["molecule_runtime/main.py", "tests/main.py"])
        self.assertIn("--no-renames", run.call_args.args[0])

    def test_test_only_change_skips_release(self):
        should_release, reason = classify_runtime_release.release_required(
            ["tests/test_executor_helpers.py"]
        )

        self.assertFalse(should_release)
        self.assertEqual(reason, "no package-affecting changes")

    def test_docs_and_ci_metadata_skip_release(self):
        should_release, _ = classify_runtime_release.release_required(
            ["docs/execution.md", ".gitea/workflows/ci.yml", ".github/CODEOWNERS"]
        )

        self.assertFalse(should_release)

    def test_script_test_skips_release(self):
        should_release, _ = classify_runtime_release.release_required(
            ["scripts/test_merge_runtime_version_bumps.py"]
        )

        self.assertFalse(should_release)

    def test_package_source_change_releases(self):
        should_release, reason = classify_runtime_release.release_required(
            ["molecule_runtime/main.py"]
        )

        self.assertTrue(should_release)
        self.assertEqual(reason, "package-affecting changes detected")

    def test_package_metadata_change_releases(self):
        should_release, _ = classify_runtime_release.release_required(["pyproject.toml"])

        self.assertTrue(should_release)

    def test_dependency_change_releases(self):
        should_release, _ = classify_runtime_release.release_required(
            ["requirements-dev.txt"]
        )

        self.assertTrue(should_release)

    def test_build_script_change_releases(self):
        should_release, _ = classify_runtime_release.release_required(
            ["scripts/auto_release_runtime.py"]
        )

        self.assertTrue(should_release)

    def test_mixed_change_releases(self):
        should_release, _ = classify_runtime_release.release_required(
            ["tests/test_imports.py", "molecule_runtime/adapter_base.py"]
        )

        self.assertTrue(should_release)

    def test_empty_diff_releases_fail_safe(self):
        should_release, reason = classify_runtime_release.release_required([])

        self.assertTrue(should_release)
        self.assertEqual(reason, "empty diff; release fails safe")

    def test_manual_dispatch_forces_release(self):
        should_release, reason = classify_runtime_release.release_required(
            ["tests/test_imports.py"], event_name="workflow_dispatch"
        )

        self.assertTrue(should_release)
        self.assertEqual(reason, "manual workflow dispatch")


if __name__ == "__main__":
    unittest.main()
