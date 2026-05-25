"""Regression tests for plugin env scrubbing (issue #19, CWE-C-312)."""

import asyncio
import subprocess
from pathlib import Path

import pytest


class TestScrubbedEnv:
    """_scrubbed_env() must strip sensitive keys before passing env to subprocess."""

    def test_strips_anthropic_api_key(self):
        from molecule_runtime.plugins_registry.builtins import _scrubbed_env

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
            env = _scrubbed_env({})
            assert "ANTHROPIC_API_KEY" not in env

    def test_strips_github_token(self):
        from molecule_runtime.plugins_registry.builtins import _scrubbed_env

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GITHUB_TOKEN", "ghs_secret")
            env = _scrubbed_env({})
            assert "GITHUB_TOKEN" not in env
            assert "GH_TOKEN" not in env

    def test_strips_workspace_auth_token(self):
        from molecule_runtime.plugins_registry.builtins import _scrubbed_env

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_AUTH_TOKEN", "tok_abc123")
            env = _scrubbed_env({})
            assert "WORKSPACE_AUTH_TOKEN" not in env

    def test_preserves_path_and_user_env(self):
        from molecule_runtime.plugins_registry.builtins import _scrubbed_env

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("HOME", "/root")
            mp.setenv("PATH", "/usr/bin")
            env = _scrubbed_env({})
            assert env.get("HOME") == "/root"
            assert env.get("PATH") == "/usr/bin"

    def test_merges_extra_vars(self):
        from molecule_runtime.plugins_registry.builtins import _scrubbed_env

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("FOO", "bar")
            env = _scrubbed_env({"CONFIGS_DIR": "/configs", "MY_KEY": "my_value"})
            assert env.get("CONFIGS_DIR") == "/configs"
            assert env.get("MY_KEY") == "my_value"
            assert env.get("FOO") == "bar"


class TestAgentskillsAdaptorEnvScrubbing:
    """AgentskillsAdaptor.install() must pass a scrubbed env to subprocess."""

    def test_install_runs_setup_sh_with_scrubbed_env(self, tmp_path: Path):
        """Verify setup.sh receives no API keys in its environment."""
        from molecule_runtime.plugins_registry.builtins import AgentskillsAdaptor
        from molecule_runtime.plugins_registry.protocol import InstallContext
        import logging

        # Create a fake plugin with a setup.sh
        setup_sh = tmp_path / "setup.sh"
        setup_sh.write_text("#!/bin/sh\necho CLEAN && exit 0\n")
        setup_sh.chmod(0o755)

        env_with_secrets = {
            "ANTHROPIC_API_KEY": "sk-ant-test123",
            "OPENAI_API_KEY": "sk-openai-test456",
            "GITHUB_TOKEN": "ghs_test789",
            "WORKSPACE_AUTH_TOKEN": "tok_test000",
            "HOME": "/root",
            "PATH": "/usr/bin",
        }

        ctx = InstallContext(
            plugin_root=tmp_path,
            configs_dir=tmp_path / "configs",
            workspace_id="test-workspace",
            runtime="claude-code",
            memory_filename="CLAUDE.md",
            logger=logging.getLogger("test"),
        )
        adaptor = AgentskillsAdaptor("test-plugin", "claude-code")

        captured_env: dict = {}

        original_run = subprocess.run

        def capture_run(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return original_run(*args, **{
                k: v for k, v in kwargs.items() if k != "env"
            } | {"env": env_with_secrets})

        # Patch subprocess.run, run install, restore
        subprocess.run = capture_run  # type: ignore[misc-reassign-operator]
        try:
            asyncio.run(adaptor.install(ctx))
        finally:
            subprocess.run = original_run  # type: ignore[misc-reassign-operator]

        # Verify no API keys reached the subprocess
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "WORKSPACE_AUTH_TOKEN"):
            assert key not in captured_env, f"{key} leaked to setup.sh env!"

        # CONFIGS_DIR must be passed
        assert captured_env.get("CONFIGS_DIR") == str(tmp_path / "configs")

        # Non-secret vars from the scrubbed env must be present (PATH always
        # exists in a Linux container; HOME may differ between test envs).
        assert "PATH" in captured_env, "PATH missing from subprocess env"
        assert "HOME" in captured_env, "HOME missing from subprocess env"
