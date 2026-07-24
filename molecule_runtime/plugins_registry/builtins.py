"""Built-in plugin adaptors — one per agent shape.

The adapter layer is our extensibility surface. Each agent "shape" (form
of installable capability) gets its own named sub-type adapter. A plugin
picks which sub-type to use by importing it as ``Adaptor`` in its
per-runtime file:

.. code-block:: python

    # plugins/<name>/adapters/claude_code.py
    from molecule_runtime.plugins_registry.builtins import AgentskillsAdaptor as Adaptor

Shape taxonomy (one class per shape; add more as the ecosystem evolves):

* :class:`AgentskillsAdaptor` — skills in the `agentskills.io
  <https://agentskills.io>`_ format (``SKILL.md`` + ``scripts/`` +
  ``references/`` + ``assets/``), plus Molecule AI's optional ``rules/`` and
  root-level prompt fragments at the plugin level. Works on every runtime
  we support (the spec's filesystem layout makes activation trivial on
  Claude Code, and our adapter code does the equivalent on the other
  runtimes we support). **This is the default and covers the common case.**
* :class:`MCPServerAdaptor` — consumes canonical
  ``plugin.yaml`` ``contributes.mcpServers`` entries (plus legacy JSON
  descriptors) and registers them through the active runtime's MCP port.

Planned as the ecosystem matures (rule of
three: promote a class here only after 3+ plugins ship the same custom
shape via their own ``adapters/<runtime>.py``):

* ``SubagentAdaptor`` — register a runtime-native sub-agent *(backlog)*
* ``RAGPipelineAdaptor`` — wire a retriever + index *(backlog)*
* ``SwarmAdaptor`` — bind a multi-agent swarm *(backlog)*
* ``WebhookAdaptor`` — register an event handler *(backlog)*

Plugins whose shape doesn't match any built-in ship their own adapter
class in ``plugins/<name>/adapters/<runtime>.py`` — full Python, no
constraint. When 3+ plugins ship the same custom pattern, we promote
the class into this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from .protocol import SKILLS_SUBDIR, InstallContext, InstallResult

# Import the dedicated exception type so we can distinguish a
# privileged-plugin install failure from an ordinary-plugin hiccup.
# adapter_base defines PrivilegedPluginInstallError as a RuntimeError
# subclass, so existing `pytest.raises(RuntimeError, ...)` / `except
# RuntimeError` call sites still match (we use the dedicated type
# only at the throw site and at the discriminator site in main.py).
from molecule_runtime.adapter_base import PrivilegedPluginInstallError

# Keys scrubbed from plugin setup.sh env — matches skill_loader/loader.py's
# _SCRUB_KEYS so a malicious plugin's setup.sh cannot exfiltrate credentials
# that are available to the parent process. Fixes issue #19 (CWE-C-312).
# Enforced by test_plugins_builtins_env_scrub.py.
_SCRUB_KEYS = frozenset((
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "WORKSPACE_AUTH_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    # RFC#2843 #32 hardening: the read-only Gitea template PAT and the CP admin
    # token are present in the workspace container env (InjectTemplateRepoCreds /
    # boot-event wiring). A malicious plugin's setup.sh must not inherit + exfil
    # them. The agent can still read its own env directly — this only closes the
    # third-party-plugin setup.sh vector (the agent-itself vector is inherent to
    # the interim single-token model, retired by the marketplace broker #31).
    "MOLECULE_TEMPLATE_REPO_TOKEN",
    "MOLECULE_ADMIN_TOKEN",
))


def _scrubbed_env(extra: dict[str, str]) -> dict[str, str]:
    """Return a copy of os.environ with sensitive keys stripped plus *extra* merged."""
    return {k: v for k, v in os.environ.items() if k not in _SCRUB_KEYS} | extra


def _setup_shell() -> str:
    """Resolve bash before env scrubbing changes PATH for setup.sh."""
    return shutil.which("bash") or "/bin/bash"


# Files at the plugin root that are never treated as prompt fragments,
# even if they're markdown. Module-level so tests and other adapters can
# import the set rather than re-declaring it.
# skill.md is the skill spec file (handled by the skills-copy step), never a
# memory fragment — exclude it from the root-*.md memory append (RFC#2843 #32,
# SKILL.md-at-root shape).
SKIP_ROOT_MD = frozenset({"readme.md", "changelog.md", "license.md", "contributing.md", "skill.md"})

# Privileged org-management MCP plugin. A failed setup.sh here must fail the
# install loudly rather than leaving a configured-but-missing MCP binary.
# See molecule-ai-workspace-runtime#151.
_PRIVILEGED_MCP_PLUGIN = "molecule-platform-mcp"
_OWNERSHIP_VERSION = 1


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _state_dir(ctx: InstallContext) -> Path:
    root = ctx.configs_dir.resolve()
    state_dir = (root / ".molecule").resolve()
    try:
        state_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("plugin lifecycle state directory escapes configs_dir") from exc
    return state_dir


def _ownership_path(ctx: InstallContext, plugin_name: str) -> Path:
    key = hashlib.sha256(plugin_name.encode("utf-8")).hexdigest()[:24]
    root = ctx.configs_dir.resolve()
    parent = (_state_dir(ctx) / "plugin-ownership").resolve()
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ValueError("plugin ownership directory escapes configs_dir") from exc
    return parent / f"{key}.json"


@contextmanager
def _plugin_lifecycle_lock(ctx: InstallContext):
    state_dir = _state_dir(ctx)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "plugin-lifecycle.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_settings_fragment(fragment: Any) -> bool:
    if not isinstance(fragment, dict):
        return False
    hooks = fragment.get("hooks")
    if hooks is None:
        return True
    if not isinstance(hooks, dict):
        return False
    for event, handlers in hooks.items():
        if not isinstance(event, str) or not isinstance(handlers, list):
            return False
        for handler in handlers:
            if not isinstance(handler, dict):
                return False
            commands = handler.get("hooks", [])
            if not isinstance(commands, list):
                return False
            for command in commands:
                if not isinstance(command, dict):
                    return False
                value = command.get("command")
                if value is not None and not isinstance(value, str):
                    return False
    return True


def _valid_ownership_state(state: Any, plugin_name: str) -> bool:
    if (
        not isinstance(state, dict)
        or state.get("version") != _OWNERSHIP_VERSION
        or state.get("plugin_name") != plugin_name
    ):
        return False
    owned_files = state.get("owned_files")
    if not isinstance(owned_files, dict) or any(
        not isinstance(relative, str) or not _is_sha256(digest)
        for relative, digest in owned_files.items()
    ):
        return False
    memory = state.get("memory")
    if memory is not None and (
        not isinstance(memory, dict)
        or not isinstance(memory.get("filename"), str)
        or not isinstance(memory.get("path"), str)
        or not isinstance(memory.get("marker"), str)
        or not _is_sha256(memory.get("sha256"))
    ):
        return False
    settings = state.get("settings")
    if settings is not None and (
        not isinstance(settings, dict)
        or not isinstance(settings.get("path"), str)
        or not _valid_settings_fragment(settings.get("fragment"))
        or not isinstance(settings.get("existed_before"), bool)
    ):
        return False
    return True


def _load_ownership(ctx: InstallContext, plugin_name: str) -> dict[str, Any] | None:
    try:
        path = _ownership_path(ctx, plugin_name)
    except ValueError as exc:
        ctx.logger.warning(
            "%s: unsafe ownership path, preserving installed files: %s",
            plugin_name,
            exc,
        )
        return None
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        ctx.logger.warning(
            "%s: invalid ownership record, preserving installed files: %s",
            plugin_name,
            exc,
        )
        return None
    if not _valid_ownership_state(state, plugin_name):
        ctx.logger.warning(
            "%s: invalid ownership record, preserving installed files",
            plugin_name,
        )
        return None
    return state


def _write_ownership(
    ctx: InstallContext,
    plugin_name: str,
    state: dict[str, Any],
) -> None:
    path = _ownership_path(ctx, plugin_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _persist_ownership(
    ctx: InstallContext,
    plugin_name: str,
    owned_files: dict[str, str],
    memory: dict[str, str] | None,
    settings: dict[str, Any] | None,
) -> None:
    state: dict[str, Any] = {
        "version": _OWNERSHIP_VERSION,
        "plugin_name": plugin_name,
        "owned_files": owned_files,
    }
    if memory:
        state["memory"] = memory
    if settings:
        state["settings"] = settings
    _write_ownership(ctx, plugin_name, state)


def _owned_path(ctx: InstallContext, relative: str) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    root = ctx.configs_dir.resolve()
    raw = ctx.configs_dir / relative
    if raw.name in ("", ".", ".."):
        return None
    # Resolve the PARENT (following directory symlinks) and containment-check it,
    # but never follow the final component. If the recorded owned path has since
    # been replaced by a symlink, we must treat it as the symlink it is rather
    # than resolving to — and then mutating/deleting — its target, which the
    # plugin never owned. Callers get None and preserve the replacement.
    try:
        parent = raw.parent.resolve()
    except OSError:
        return None
    try:
        parent.relative_to(root)
    except ValueError:
        return None
    leaf = parent / raw.name
    if leaf.is_symlink():
        return None
    return leaf


def _has_symlink_component(path: Path, root: Path) -> bool:
    """True if ``path`` — or any path component below ``root`` — is a symlink.

    Walks from ``root`` down to ``path`` with per-component ``lstat`` (no-follow),
    so a symlink swapped in at any level, including the leaf, is detected without
    ever following it. Keeps an install/update from writing *through* a symlinked
    destination onto a target the plugin never owned. A ``path`` outside ``root``
    is treated as unsafe.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _memory_marker(plugin_name: str) -> str:
    return hashlib.sha256(plugin_name.encode("utf-8")).hexdigest()[:24]


def _runtime_memory_path(ctx: InstallContext, filename: str) -> Path | None:
    candidate = Path(filename)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        import molecule_runtime.mailbox_dir as mailbox_dir

        if mailbox_dir.kernel_enabled():
            root = mailbox_dir.memory_dir().resolve()
            path = mailbox_dir.memory_file(filename).resolve()
            path.relative_to(root)
            return path
    except Exception:
        return None
    root = ctx.configs_dir.resolve()
    path = (root / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _memory_path_from_state(
    ctx: InstallContext,
    memory: dict[str, Any],
) -> Path | None:
    filename = memory.get("filename")
    stored = memory.get("path")
    if not isinstance(filename, str) or not isinstance(stored, str):
        return None
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    configs_root = ctx.configs_dir.resolve()
    configs_path = (configs_root / relative).resolve()
    candidates: set[Path] = set()
    try:
        configs_path.relative_to(configs_root)
        candidates.add(configs_path)
    except ValueError:
        pass
    current = _runtime_memory_path(ctx, filename)
    if current is not None:
        candidates.add(current)
    path = Path(stored).resolve()
    return path if path in candidates else None


def _remove_memory_block(
    path: Path,
    marker: str,
    expected_sha256: str,
) -> bool:
    if not path.is_file():
        return False
    start_marker = f"<!-- molecule-plugin:{marker}:start -->"
    end_marker = f"<!-- molecule-plugin:{marker}:end -->"
    text = path.read_text()
    changed = False
    cursor = 0
    while True:
        start = text.find(start_marker, cursor)
        if start < 0:
            break
        end = text.find(end_marker, start + len(start_marker))
        if end < 0:
            break
        block_end = end + len(end_marker)
        block = text[start:block_end]
        if _text_sha256(block) != expected_sha256:
            cursor = block_end
            continue
        after = block_end + (1 if text[block_end:block_end + 1] == "\n" else 0)
        text = text[:start] + text[after:]
        changed = True
        cursor = start
    if changed:
        _atomic_write_text(path, text)
    return changed


def _subtract_settings_fragment(
    existing: dict[str, Any],
    fragment: dict[str, Any],
) -> dict[str, Any]:
    out = json.loads(json.dumps(existing))
    hooks = out.get("hooks")
    contributed_hooks = fragment.get("hooks")
    if isinstance(hooks, dict) and isinstance(contributed_hooks, dict):
        for event, contributed in contributed_hooks.items():
            current = hooks.get(event)
            if not isinstance(current, list) or not isinstance(contributed, list):
                continue
            for handler in contributed:
                try:
                    current.remove(handler)
                except ValueError:
                    pass
            if not current:
                hooks.pop(event, None)
        if not hooks:
            out.pop("hooks", None)
    for key, value in fragment.items():
        if key == "hooks" or key not in out:
            continue
        current = out[key]
        if isinstance(current, dict) and isinstance(value, dict):
            cleaned = _subtract_mapping(current, value)
            if cleaned:
                out[key] = cleaned
            else:
                out.pop(key, None)
        elif current == value:
            out.pop(key, None)
    return out


def _subtract_mapping(
    existing: dict[str, Any],
    contribution: dict[str, Any],
) -> dict[str, Any]:
    out = json.loads(json.dumps(existing))
    for key, value in contribution.items():
        if key not in out:
            continue
        current = out[key]
        if isinstance(current, dict) and isinstance(value, dict):
            cleaned = _subtract_mapping(current, value)
            if cleaned:
                out[key] = cleaned
            else:
                out.pop(key, None)
        elif current == value:
            out.pop(key, None)
    return out


def _remove_empty_parents(path: Path, root: Path) -> None:
    while path != root:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _read_md_files(directory: Path) -> list[tuple[str, str]]:
    """Return [(filename, content)] for all *.md files in directory, sorted."""
    if not directory.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.suffix == ".md":
            out.append((p.name, p.read_text().strip()))
    return out


class AgentskillsAdaptor:
    """Sub-type adaptor for `agentskills.io <https://agentskills.io>`_-format skills.

    This is the default adapter for the "skills + rules" shape — the most
    common pattern. A plugin using this adapter ships:

    * ``skills/<name>/SKILL.md`` (+ optional ``scripts/``, ``references/``,
      ``assets/``) — each skill is a spec-compliant agentskills unit,
      portable to Claude Code, Cursor, Codex, and ~35 other skill-compatible
      tools without modification.
    * ``rules/*.md`` (optional, Molecule AI extension) — always-on prose that
      gets appended to the runtime's memory file (CLAUDE.md).
    * Root-level ``*.md`` (optional) — prompt fragments, also appended to
      memory.

    On ``install()``:
      1. Rules → append to ``/configs/<memory_filename>``, wrapped in a
         ``# Plugin: <name>`` marker for idempotent re-install.
      2. Prompt fragments (``*.md`` at plugin root, excl. README/CHANGELOG/etc.)
         → same treatment.
      3. Skills (``skills/<skill_name>/``) → copied to
         ``/configs/skills/<skill_name>/``. Runtimes with native agentskills
         activation (Claude Code) pick them up automatically; other runtimes'
         loaders scan the same path.

    Install records exact, content-hashed ownership. Reinstall reconciles only
    unchanged owned files, and uninstall removes only contributions that still
    match that record. Pre-existing and user-modified content is preserved.

    For shapes other than agentskills (MCP server, sub-agent, RAG pipeline,
    swarm, webhook handler, etc.), see
    the module docstring for the planned sibling adapters, or ship a custom
    adapter class in the plugin's ``adapters/<runtime>.py``.
    """

    def __init__(self, plugin_name: str, runtime: str) -> None:
        self.plugin_name = plugin_name
        self.runtime = runtime

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    async def install(self, ctx: InstallContext) -> InstallResult:
        with _plugin_lifecycle_lock(ctx):
            return self._install(ctx)

    def _install(self, ctx: InstallContext) -> InstallResult:
        result = InstallResult(
            plugin_name=self.plugin_name,
            runtime=self.runtime,
            source="plugin",  # overridden by registry caller if source==registry
        )
        _ownership_path(ctx, self.plugin_name)
        previous = _load_ownership(ctx, self.plugin_name) or {}
        previous_owned_files = previous.get("owned_files") or {}
        owned_files: dict[str, str] = {}

        # 1. Rules — append to memory file.
        rules = _read_md_files(ctx.plugin_root / "rules")
        # 2. Prompt fragments — any *.md at plugin root except skip list.
        root_fragments: list[tuple[str, str]] = []
        if ctx.plugin_root.is_dir():
            for p in sorted(ctx.plugin_root.iterdir()):
                if p.is_file() and p.suffix == ".md" and p.name.lower() not in SKIP_ROOT_MD:
                    content = p.read_text().strip()
                    if content:
                        root_fragments.append((p.name, content))

        memory_blocks: list[str] = []
        for filename, content in rules:
            memory_blocks.append(f"# Plugin: {self.plugin_name} / rule: {filename}\n\n{content}")
        for filename, content in root_fragments:
            memory_blocks.append(f"# Plugin: {self.plugin_name} / fragment: {filename}\n\n{content}")

        marker = _memory_marker(self.plugin_name)
        previous_memory = previous.get("memory")
        if isinstance(previous_memory, dict):
            previous_path = _memory_path_from_state(ctx, previous_memory)
            if previous_path is not None:
                removed = _remove_memory_block(
                    previous_path,
                    previous_memory["marker"],
                    previous_memory["sha256"],
                )
                if not removed and previous_path.exists():
                    result.warnings.append(
                        f"preserved modified memory block in {previous_memory['filename']}"
                    )

        memory_state: dict[str, str] | None = None
        if memory_blocks:
            start = f"<!-- molecule-plugin:{marker}:start -->"
            end = f"<!-- molecule-plugin:{marker}:end -->"
            memory_body = "\n\n".join(memory_blocks)
            memory_block = f"{start}\n\n{memory_body}\n\n{end}"
            memory_path = _runtime_memory_path(ctx, ctx.memory_filename)
            if memory_path is None:
                raise ValueError("runtime memory path escapes its allowed root")
            existed_before = (
                memory_path.is_file() and memory_block in memory_path.read_text()
            )
            ctx.append_to_memory(ctx.memory_filename, memory_block)
            if (
                not existed_before
                and memory_path is not None
                and memory_path.is_file()
                and memory_block in memory_path.read_text()
            ):
                memory_state = {
                    "filename": ctx.memory_filename,
                    "path": str(memory_path),
                    "marker": marker,
                    "sha256": _text_sha256(memory_block),
                }
            ctx.logger.info(
                "%s: injected %d rule+fragment block(s) into %s",
                self.plugin_name,
                len(memory_blocks),
                ctx.memory_filename,
            )

        # 3. Skills — copy each skill dir to /configs/skills/.
        src_skills_dir = ctx.plugin_root / "skills"
        current_skill_names: set[str] = set()
        if src_skills_dir.is_dir():
            dst_skills_root = ctx.configs_dir / SKILLS_SUBDIR
            for entry in sorted(src_skills_dir.iterdir()):
                if not entry.is_dir() or entry.is_symlink():
                    continue
                current_skill_names.add(entry.name)
                dst = dst_skills_root / entry.name
                _sync_owned_files(
                    ctx,
                    entry,
                    dst,
                    result,
                    previous_owned_files,
                    owned_files,
                    recursive=True,
                    require_existing_ownership=True,
                )
        elif (ctx.plugin_root / "SKILL.md").is_file():
            # SKILL.md-at-root shape (RFC#2843 #32): the plugin root IS a single
            # agentskills unit (no skills/<name>/ subdir) — e.g. the seo-all skill
            # fetched to /configs/plugins/seo-all/. Copy the whole plugin dir to
            # /configs/skills/<plugin_name>/ so Claude Code's native skill
            # discovery picks it up. Excludes packaging-only dirs that aren't part
            # of the skill payload.
            current_skill_names.add(self.plugin_name)
            _sync_owned_files(
                ctx,
                ctx.plugin_root,
                ctx.configs_dir / SKILLS_SUBDIR / self.plugin_name,
                result,
                previous_owned_files,
                owned_files,
                recursive=True,
                require_existing_ownership=True,
                ignored_roots={".git", "__pycache__", "adapters"},
            )

        previous_skill_names = {
            relative.split("/", 2)[1]
            for relative in previous_owned_files
            if relative.startswith(f"{SKILLS_SUBDIR}/") and relative.count("/") >= 2
        }
        for removed_skill in sorted(previous_skill_names - current_skill_names):
            _sync_owned_files(
                ctx,
                src_skills_dir / removed_skill,
                ctx.configs_dir / SKILLS_SUBDIR / removed_skill,
                result,
                previous_owned_files,
                owned_files,
                recursive=True,
            )

        # 4. Setup script — run setup.sh if present (for npm/pip dependencies).
        # Mirrors sdk/python/molecule_plugin/builtins.py — must stay in sync
        # (drift guard: tests/test_plugins_builtins_drift.py).
        setup_script = ctx.plugin_root / "setup.sh"
        if setup_script.is_file():
            ctx.logger.info("%s: running setup.sh", self.plugin_name)
            try:
                proc = subprocess.run(
                    [_setup_shell(), str(setup_script)],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(ctx.plugin_root),
                    env=_scrubbed_env({"CONFIGS_DIR": str(ctx.configs_dir)}),
                )
                if proc.returncode == 0:
                    ctx.logger.info("%s: setup.sh completed successfully", self.plugin_name)
                else:
                    err = f"setup.sh exited {proc.returncode}: {proc.stderr[:200]}"
                    result.warnings.append(err)
                    result.errors.append(err)
                    ctx.logger.error("%s: setup.sh failed: %s", self.plugin_name, proc.stderr[:200])
                    if self.plugin_name == _PRIVILEGED_MCP_PLUGIN:
                        _persist_ownership(
                            ctx,
                            self.plugin_name,
                            owned_files,
                            memory_state,
                            None,
                        )
                        raise PrivilegedPluginInstallError(err)
            except subprocess.TimeoutExpired:
                err = "setup.sh timed out (120s)"
                result.warnings.append(err)
                result.errors.append(err)
                ctx.logger.error("%s: setup.sh timed out", self.plugin_name)
                if self.plugin_name == _PRIVILEGED_MCP_PLUGIN:
                    _persist_ownership(
                        ctx,
                        self.plugin_name,
                        owned_files,
                        memory_state,
                        None,
                    )
                    raise PrivilegedPluginInstallError(err)

        # 5. Hooks — copy hooks/* into <configs>/.claude/hooks/ (Claude Code-
        #    style harness hooks). No-op when the plugin doesn't ship any.
        # 6. Commands — copy commands/*.md into <configs>/.claude/commands/.
        # 7. settings-fragment.json — merge into <configs>/.claude/settings.json,
        #    rewriting ${CLAUDE_DIR} to the absolute install path. Existing
        #    user hooks are preserved (deep-merge by event).
        settings_state = _install_claude_layer(
            ctx,
            result,
            self.plugin_name,
            previous_owned_files,
            owned_files,
            previous.get("settings")
            if isinstance(previous.get("settings"), dict)
            else None,
        )

        _persist_ownership(
            ctx,
            self.plugin_name,
            owned_files,
            memory_state,
            settings_state,
        )

        return result

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    async def uninstall(self, ctx: InstallContext) -> None:
        with _plugin_lifecycle_lock(ctx):
            self._uninstall(ctx)

    def _uninstall(self, ctx: InstallContext) -> None:
        state = _load_ownership(ctx, self.plugin_name)
        if state is None:
            return

        memory = state.get("memory")
        if isinstance(memory, dict):
            memory_path = _memory_path_from_state(ctx, memory)
            if memory_path is not None:
                _remove_memory_block(memory_path, memory["marker"], memory["sha256"])

        settings = state.get("settings")
        if isinstance(settings, dict):
            _uninstall_settings_fragment(ctx, settings)

        owned_files = state.get("owned_files")
        if isinstance(owned_files, dict):
            for relative, digest in sorted(owned_files.items(), reverse=True):
                path = _owned_path(ctx, relative)
                if (
                    path is None
                    or not path.is_file()
                    or not isinstance(digest, str)
                    or _file_sha256(path) != digest
                ):
                    continue
                path.unlink()
                _remove_empty_parents(path.parent, ctx.configs_dir.resolve())

        try:
            ownership_path = _ownership_path(ctx, self.plugin_name)
        except ValueError:
            return
        ownership_path.unlink(missing_ok=True)
        _remove_empty_parents(ownership_path.parent, ctx.configs_dir.resolve())


# ----------------------------------------------------------------------
# Claude Code layer — hooks, slash commands, settings.json fragments.
# Promoted from the molecule-guardrails plugin so any plugin can ship
# these by dropping the right files; no custom adapter needed.
# ----------------------------------------------------------------------

def _install_claude_layer(
    ctx: InstallContext,
    result: InstallResult,
    plugin_name: str,
    previous_owned_files: dict[str, str],
    owned_files: dict[str, str],
    previous_settings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    claude_dir = ctx.configs_dir / ".claude"
    _sync_owned_files(
        ctx,
        ctx.plugin_root / "hooks",
        claude_dir / "hooks",
        result,
        previous_owned_files,
        owned_files,
        executable_suffix=".sh",
    )
    _sync_owned_files(
        ctx,
        ctx.plugin_root / "commands",
        claude_dir / "commands",
        result,
        previous_owned_files,
        owned_files,
        only_suffix=".md",
    )
    return _merge_settings_fragment(
        ctx,
        claude_dir,
        result,
        plugin_name,
        previous_settings,
    )


def _sync_owned_files(
    ctx: InstallContext,
    src: Path,
    dst: Path,
    result: InstallResult,
    previous_owned_files: dict[str, str],
    owned_files: dict[str, str],
    executable_suffix: str | None = None,
    only_suffix: str | None = None,
    recursive: bool = False,
    require_existing_ownership: bool = False,
    ignored_roots: set[str] | None = None,
) -> None:
    dst_relative = dst.relative_to(ctx.configs_dir).as_posix()
    prefix = f"{dst_relative}/"
    previous_scope = {
        relative: digest
        for relative, digest in previous_owned_files.items()
        if relative.startswith(prefix)
    }
    if require_existing_ownership and dst.exists() and not previous_scope:
        result.warnings.append(f"preserved unowned existing directory: {dst_relative}")
        return

    candidates: list[tuple[Path, Path]] = []
    source_files = (
        src.rglob("*")
        if recursive and src.is_dir()
        else src.iterdir()
        if src.is_dir()
        else []
    )
    for source in source_files:
        if not source.is_file() or source.is_symlink():
            continue
        relative_source = source.relative_to(src) if recursive else Path(source.name)
        if ignored_roots and relative_source.parts[0] in ignored_roots:
            continue
        if only_suffix and source.suffix != only_suffix:
            # When copying hooks, allow .py companion files alongside .sh
            if not (executable_suffix and source.suffix == ".py"):
                continue
        candidates.append((source, dst / relative_source))

    candidate_relatives: set[str] = set()
    for source, target in candidates:
        relative = target.relative_to(ctx.configs_dir).as_posix()
        candidate_relatives.add(relative)
        if _has_symlink_component(target, ctx.configs_dir):
            # The destination (or a parent component) is a symlink. exists()/
            # is_file()/copy2() would all follow it and overwrite a target the
            # plugin never owned; preserve the replacement instead.
            result.warnings.append(f"preserved symlinked path: {relative}")
            continue
        if target.exists():
            previous_digest = previous_scope.get(relative)
            if (
                not target.is_file()
                or previous_digest is None
                or _file_sha256(target) != previous_digest
            ):
                result.warnings.append(f"preserved unowned existing file: {relative}")
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if executable_suffix and source.suffix == executable_suffix:
            target.chmod(0o755)
        result.files_written.append(relative)
        owned_files[relative] = _file_sha256(target)

    for relative, previous_digest in previous_scope.items():
        if relative in candidate_relatives or relative in owned_files:
            continue
        path = _owned_path(ctx, relative)
        if (
            path is None
            or not path.is_file()
            or _file_sha256(path) != previous_digest
        ):
            continue
        path.unlink()
        _remove_empty_parents(path.parent, ctx.configs_dir.resolve())


def _merge_settings_fragment(
    ctx: InstallContext,
    claude_dir: Path,
    result: InstallResult,
    plugin_name: str,
    previous_settings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    fragment_path = ctx.plugin_root / "settings-fragment.json"
    if not fragment_path.is_file() and previous_settings is None:
        return
    fragment: dict[str, Any] | None = None
    if fragment_path.is_file():
        try:
            loaded = json.loads(fragment_path.read_text())
            if not _valid_settings_fragment(loaded):
                raise ValueError("root/hooks/handler shape is invalid")
            fragment = loaded
        except Exception as exc:
            result.warnings.append(f"settings-fragment.json invalid: {exc}")
            return previous_settings

    settings_path = claude_dir / "settings.json"
    existed_before = (
        bool(previous_settings.get("existed_before"))
        if previous_settings is not None
        else settings_path.is_file()
    )
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception as exc:
            result.warnings.append(
                f"existing settings.json is invalid; preserved: {exc}"
            )
            return previous_settings
    else:
        existing = {}
    if not _valid_settings_fragment(existing):
        result.warnings.append(
            "existing settings.json is invalid; preserved: "
            "root/hooks/handler shape is invalid"
        )
        return previous_settings
    if previous_settings is not None and isinstance(
        previous_settings.get("fragment"), dict
    ):
        existing = _subtract_settings_fragment(
            existing,
            previous_settings["fragment"],
        )

    fragment_no_mcp = (
        {key: value for key, value in fragment.items() if key != "mcpServers"}
        if fragment is not None
        else None
    )
    if not fragment_no_mcp:
        if previous_settings is None:
            return None
        if existing or existed_before:
            _atomic_write_text(settings_path, json.dumps(existing, indent=2) + "\n")
        else:
            settings_path.unlink(missing_ok=True)
        return None

    rewritten = _rewrite_hook_paths(fragment_no_mcp, claude_dir)
    merged, applied = _deep_merge_hooks(existing, rewritten)
    if not applied:
        if previous_settings is None:
            return None
        if existing or existed_before:
            _atomic_write_text(settings_path, json.dumps(existing, indent=2) + "\n")
        else:
            settings_path.unlink(missing_ok=True)
        return None

    claude_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(settings_path, json.dumps(merged, indent=2) + "\n")
    result.files_written.append(str(settings_path.relative_to(ctx.configs_dir)))
    ctx.logger.info("%s: merged hook config into %s", plugin_name, settings_path)
    return {
        "path": str(settings_path.relative_to(ctx.configs_dir)),
        "fragment": applied,
        "existed_before": existed_before,
    }


def _uninstall_settings_fragment(
    ctx: InstallContext,
    settings: dict[str, Any],
) -> None:
    relative = settings.get("path")
    fragment = settings.get("fragment")
    if not isinstance(relative, str) or not isinstance(fragment, dict):
        return
    settings_path = _owned_path(ctx, relative)
    if settings_path is None or not settings_path.is_file():
        return
    try:
        existing = json.loads(settings_path.read_text())
    except (OSError, ValueError) as exc:
        ctx.logger.warning("settings file changed or invalid; preserving it: %s", exc)
        return
    if not isinstance(existing, dict):
        return
    cleaned = _subtract_settings_fragment(existing, fragment)
    if cleaned or settings.get("existed_before"):
        _atomic_write_text(settings_path, json.dumps(cleaned, indent=2) + "\n")
    else:
        settings_path.unlink()


def _rewrite_hook_paths(fragment: dict, claude_dir: Path) -> dict:
    out = json.loads(json.dumps(fragment))  # deep copy via roundtrip
    for handlers in out.get("hooks", {}).values():
        for handler in handlers:
            for h in handler.get("hooks", []):
                cmd = h.get("command", "")
                h["command"] = cmd.replace("${CLAUDE_DIR}", str(claude_dir))
    return out


def _deep_merge_hooks(existing: dict, fragment: dict) -> tuple[dict, dict]:
    out = dict(existing)
    applied: dict[str, Any] = {}
    fragment_hooks = fragment.get("hooks", {})
    if fragment_hooks:
        out.setdefault("hooks", {})
    for event, handlers in fragment_hooks.items():
        out["hooks"].setdefault(event, [])
        # Build a set of already-present handler fingerprints so that
        # re-installing the same plugin fragment does not append duplicates.
        # Key: (matcher, frozenset-of-commands) — same logic the issue spec
        # describes. Two handlers are considered identical when they watch the
        # same matcher pattern and invoke exactly the same set of commands.
        seen: set[tuple[str, frozenset[str]]] = {
            (h.get("matcher", ""), frozenset(c.get("command", "") for c in h.get("hooks", [])))
            for h in out["hooks"][event]
        }
        for handler in handlers:
            hkey = (
                handler.get("matcher", ""),
                frozenset(c.get("command", "") for c in handler.get("hooks", [])),
            )
            if hkey not in seen:
                seen.add(hkey)
                out["hooks"][event].append(handler)
                applied.setdefault("hooks", {}).setdefault(event, []).append(handler)
    for top_key, val in fragment.items():
        if top_key == "hooks":
            continue
        if top_key in out and isinstance(out[top_key], dict) and isinstance(val, dict):
            added = {
                key: value
                for key, value in val.items()
                if key not in out[top_key]
            }
            if added:
                out[top_key] = {**out[top_key], **added}
                applied[top_key] = added
        elif top_key not in out:
            out[top_key] = val
            applied[top_key] = val
    return out, applied


# ----------------------------------------------------------------------
# MCPServerAdaptor — issue #847.
# Promoted from custom adapters after 4 plugin proposals (molecule-firecrawl
# #512, molecule-github-mcp #520, molecule-browser-use #553, mcp-connector
# #573) all shipped the same pattern independently.
# ----------------------------------------------------------------------


def _read_mcp_descriptor(plugin_root: Path) -> dict[str, dict]:
    """Parse the runtime-agnostic ``mcpServers`` descriptor a plugin ships.

    The canonical SDK contract is ``plugin.yaml``'s
    ``contributes.mcpServers`` list. Legacy plugins may instead carry a dedicated
    ``mcp-servers.json`` descriptor or a Claude-shaped
    ``settings-fragment.json``. The canonical manifest wins when more than one
    form is present, so generated plugins have one source of truth.

    Returns ``{name: spec}`` (possibly empty). Malformed descriptors are ignored.
    """
    manifest_path = plugin_root / "plugin.yaml"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = yaml.safe_load(manifest_path.read_text())
        except (OSError, yaml.YAMLError):
            manifest = None
        if isinstance(manifest, dict):
            contributes = manifest.get("contributes")
            entries = (
                contributes.get("mcpServers")
                if isinstance(contributes, dict)
                else None
            )
            if isinstance(entries, list):
                servers: dict[str, dict] = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    spec = {
                        key: value for key, value in entry.items() if key != "name"
                    }
                    if spec:
                        servers[name] = spec
                if servers:
                    return servers

    for candidate in ("mcp-servers.json", "settings-fragment.json"):
        path = plugin_root / candidate
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # mcp-servers.json may be either {name: spec} directly or wrapped in an
        # mcpServers key; settings-fragment.json always nests under mcpServers.
        servers = data.get("mcpServers", data if candidate == "mcp-servers.json" else {})
        if isinstance(servers, dict) and servers:
            return {n: s for n, s in servers.items() if isinstance(s, dict)}
    return {}


def _resolve_plugin_local_mcp_paths(plugin_root: Path, spec: dict) -> dict:
    """Make direct plugin-local command/argument paths launch-location safe.

    Native MCP renderers persist ``command`` and ``args`` and the runtime later
    launches them from its own working directory.  SDK scaffolds intentionally
    use the portable ``python server.py`` shape, so any relative value that
    names a real regular file inside the plugin is rewritten to its absolute
    path. Flags, package names, URLs, absolute paths, missing files, symlinks,
    and paths escaping the plugin root remain untouched.
    """
    resolved = dict(spec)
    try:
        root = plugin_root.resolve(strict=True)
    except OSError:
        return resolved

    def local_file(value: object) -> object:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            return value
        candidate = plugin_root / value
        try:
            if candidate.is_symlink():
                return value
            target = candidate.resolve(strict=True)
            target.relative_to(root)
            if not target.is_file():
                return value
        except (OSError, ValueError):
            return value
        return str(target)

    if "command" in resolved:
        resolved["command"] = local_file(resolved["command"])
    args = resolved.get("args")
    if isinstance(args, list):
        resolved["args"] = [local_file(arg) for arg in args]
    return resolved


# Explicit secret-binding sigil for an MCP server's ``env`` values:
# ``${secret:NAME}``. A plugin BUNDLE carries only the secret NAME — never the
# value — and the runtime resolves it HERE, before the spec reaches the
# runtime-agnostic ``register_mcp_server`` PORT. Resolving at this layer (instead
# of relying on the downstream launcher to expand it) makes the binding behave
# IDENTICALLY on every runtime: hermes happens to expand a bare ``${VAR}`` from
# its inherited ``os.environ`` at spawn, but the claude-code JSON adapter (and
# hermes multiplex profiles) write env values verbatim and would otherwise ship
# the literal placeholder to the subprocess. Only this explicit sigil is
# resolved; a bare ``${VAR}`` is deliberately left untouched (backward compatible
# with the existing hermes passthrough). Values resolve against the workspace's
# own injected environment (the provisioner delivers workspace secrets as
# container env) — the same source hermes already reads — so this adds
# portability, not a new trust surface. A missing secret DROPS the key so an
# audience:self MCP can fall back to pulling it via its workspace token.
_MCP_SECRET_REF = re.compile(r"\A\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}\Z")


def _resolve_mcp_secret_refs(spec: dict, lookup=None) -> dict:
    """Resolve ``${secret:NAME}`` bindings in an MCP spec's ``env`` map.

    ``lookup(name) -> value | None`` supplies each secret; defaults to
    ``os.environ.get``. A value that is EXACTLY ``${secret:NAME}`` is replaced by
    the resolved secret. When the named secret is absent/empty the env key is
    DROPPED (not passed as a useless literal) so the MCP server falls back to its
    own resolution (e.g. a self-fetch via the audience:self workspace token).
    Every other value — a bare ``${VAR}``, a plain literal, a non-string — passes
    through unchanged. Returns a NEW spec; the input is not mutated.
    """
    env = spec.get("env")
    if not isinstance(env, dict):
        return spec
    if lookup is None:
        lookup = os.environ.get
    resolved: dict = {}
    for key, value in env.items():
        if isinstance(value, str):
            match = _MCP_SECRET_REF.match(value.strip())
            if match:
                secret = lookup(match.group(1))
                if secret:
                    resolved[key] = str(secret)
                # else: drop the key — let the server self-resolve
                continue
        resolved[key] = value
    out = dict(spec)
    out["env"] = resolved
    return out


class MCPServerAdaptor:
    """Sub-type adaptor for plugins that wrap an MCP server.

    The plugin ships:

    * canonical ``plugin.yaml`` with ``contributes.mcpServers``. Legacy
      ``mcp-servers.json`` and Claude-shaped ``settings-fragment.json``
      descriptors remain accepted during migration, e.g.:

      .. code-block:: json

          {
            "mcpServers": {
              "my-server": {
                "command": "npx",
                "args": ["-y", "@org/my-mcp-server"]
              }
            }
          }

    * ``skills/<name>/SKILL.md`` (optional) — agentskills.io skill docs;
      ``AgentskillsAdaptor`` logic handles these.
    * ``rules/*.md`` (optional) — always-on prose appended to CLAUDE.md;
      ``AgentskillsAdaptor`` logic handles these.
    * ``setup.sh`` (optional) — install npm packages, build binaries, etc.;
      ``AgentskillsAdaptor`` logic handles these.

    On ``install()``:

      1. The ``mcpServers`` descriptor (canonical manifest or legacy JSON) is
         parsed and each entry is wired via the
         MCP-wiring PORT: ``ctx.register_mcp_server(name, spec)``. The active
         runtime's adapter renders the descriptor into the file IT reads —
         ``.claude/settings.json`` for Claude Code (the default hook),
         ``~/.codex/config.toml`` for codex, etc. This replaces the old path
         that always wrote ``.claude/settings.json`` regardless of runtime,
         which silently mis-wired non-Claude concierges (#3159).
      2. Hooks/commands + skills + rules + setup.sh → delegated to
         ``AgentskillsAdaptor`` (which still merges any NON-mcpServers
         settings-fragment keys, e.g. hooks, via the claude layer).

    On ``uninstall()``:

      1. Skills + rules → delegated to ``AgentskillsAdaptor.uninstall()``.
      2. ``mcpServers`` entries are intentionally **not** removed from
         ``settings.json`` on uninstall — EXCEPT audience=``self`` entries.
         A generic MCP config is often shared with other tools or manually
         curated, so removing it could break a user's setup; the user removes
         those manually. But an ``audience:"self"`` entry is a workspace-token
         surface the runtime CREDENTIALED at install (token-file path + self
         mode), so on opt-out it MUST be torn down or it lingers on disk as a
         live self-mode surface (RFC plugin-mcp-audience-contract, review
         MAJOR-4). The strip is scoped NARROWLY to audience=``self`` entries —
         every other entry keeps the intentionally-not-removed behavior.

    ⚠ NOT-YET-WIRED (honest scope): NOTHING in the runtime currently calls this
    adaptor's ``uninstall()`` with a real ``InstallContext``. The plugin pipeline
    dispatches only ``install()`` (``adapter_base.install_plugins_via_registry``);
    there is no symmetric removal pipeline, and plugin removal takes a full restart
    (``daemon_runtime`` module docstring: "Hot-REMOVE (uninstall) is intentionally
    out of scope"). Because the base hook renders ``mcpServers`` by ADDITIVE MERGE
    into a ``settings.json`` on the persistent ``/configs`` volume
    (``mcp_render.render_json_mcp_servers``), a removed self entry genuinely lingers
    across restarts — so the teardown below is a CORRECT-when-invoked strip that is
    not reached in production yet. The audience=self opt-out teardown is therefore
    only exercised by tests until a removal pipeline is wired (follow-up).

    Usage — in the plugin's per-runtime adapter file:

    .. code-block:: python

        # plugins/<name>/adapters/claude_code.py
        from molecule_runtime.plugins_registry.builtins import MCPServerAdaptor as Adaptor
    """

    def __init__(self, plugin_name: str, runtime: str) -> None:
        self.plugin_name = plugin_name
        self.runtime = runtime

    async def install(self, ctx: InstallContext) -> InstallResult:
        with _plugin_lifecycle_lock(ctx):
            return self._install(ctx)

    def _install(self, ctx: InstallContext) -> InstallResult:
        result = InstallResult(
            plugin_name=self.plugin_name,
            runtime=self.runtime,
            source="plugin",
        )
        # 1. Wire each MCP server through the runtime-agnostic PORT. The active
        #    adapter (resolved at install time) renders the descriptor into the
        #    native config its runtime reads — Claude settings.json, codex
        #    config.toml, etc. — instead of this adaptor hard-coding the Claude
        #    path (the #3159 bug: a codex concierge got the MCP written to a file
        #    its runtime never reads).
        descriptor = _read_mcp_descriptor(ctx.plugin_root)
        for name, spec in descriptor.items():
            try:
                resolved_spec = _resolve_mcp_secret_refs(
                    _resolve_plugin_local_mcp_paths(ctx.plugin_root, spec)
                )
                ctx.register_mcp_server(name, resolved_spec)
                ctx.logger.info("%s: wired MCP server %r via register_mcp_server (runtime=%s)",
                                self.plugin_name, name, self.runtime)
            except NotImplementedError as exc:
                # A runtime whose native MCP-config renderer is not yet
                # implemented (a documented fail-loud stub). For the privileged
                # management MCP this is a loud failure — a concierge on that
                # runtime would boot WITHOUT create_workspace, the exact #3159
                # class of bug — so surface it like a privileged-install failure.
                err = f"register_mcp_server({name!r}) unsupported on runtime {self.runtime!r}: {exc}"
                result.warnings.append(err)
                result.errors.append(err)
                ctx.logger.error("%s: %s", self.plugin_name, err)
                if self.plugin_name == _PRIVILEGED_MCP_PLUGIN:
                    raise PrivilegedPluginInstallError(err) from exc
        # 2. Hooks/commands + skills + rules + setup.sh — reuse AgentskillsAdaptor
        #    logic (its claude layer now skips mcpServers; the PORT owns those).
        sub = AgentskillsAdaptor(self.plugin_name, self.runtime)._install(ctx)
        result.files_written.extend(sub.files_written)
        result.warnings.extend(sub.warnings)
        result.errors.extend(sub.errors)
        return result

    async def uninstall(self, ctx: InstallContext) -> None:
        # NOTE: mcpServers entries are intentionally NOT removed (see class docstring)
        # — EXCEPT audience=self entries, which the runtime credentialed at install
        # (workspace-token-file path + MOLECULE_MCP_MODE=self) and which therefore
        # MUST be torn down on opt-out so no live self-mode surface lingers on disk
        # (RFC plugin-mcp-audience-contract, review MAJOR-4). Scoped NARROWLY to
        # audience=self; any other entry keeps the intentionally-not-removed behavior.
        #
        # ⚠ NOT-YET-WIRED: this method has NO production caller — the plugin pipeline
        # only dispatches install(); removal takes a restart (see class docstring).
        # The teardown below is correct-when-invoked but currently reached only by
        # tests. Do not treat the audience=self opt-out strip as an active runtime
        # guarantee until a removal pipeline calls this with a real InstallContext.
        self._teardown_self_audience_entries(ctx)
        # Delegate to AgentskillsAdaptor for skills + rules cleanup.
        await AgentskillsAdaptor(self.plugin_name, self.runtime).uninstall(ctx)

    def _teardown_self_audience_entries(self, ctx: InstallContext) -> None:
        """Strip this plugin's audience=self mcpServers entries from the rendered
        native settings.json on opt-out.

        Reads the SAME descriptor install wired, and for each entry that declared
        ``audience: self`` removes that named entry — and with it the nested injected
        credential env (the token-file path + self mode) — from the generic JSON
        ``mcpServers`` config the base hook renders to
        (``<configs_dir>/.claude/settings.json``). Every non-self entry is left
        untouched, preserving the intentionally-not-removed behavior for shared /
        curated MCP configs. Best-effort + fail-safe: a missing/malformed settings
        file is a no-op (nothing to leak).

        SCOPE (honest, v1): TWO limitations, neither yet closed —
          1. NOT-INVOKED: no production removal path calls ``uninstall()`` (see the
             class + ``uninstall`` docstrings), so this strip runs only under test.
          2. HARDCODED TARGET: it strips the generic JSON default the base hook
             writes (``default_json_settings_path``). A runtime that renders a
             DIFFERENT native file via its OWN adapter override (codex TOML,
             hermes/openclaw YAML/JSON) would need the symmetric native remove —
             install/uninstall disagree for non-claude runtimes. Per-runtime
             teardown parity (strip the adapter's ACTUAL render target) is a tracked
             follow-up (RFC §9 item 6). v1's self-schedule sibling ships on the
             claude-code base runtime, whose native config IS this generic JSON, so
             the hardcoded path is correct for that one runtime only."""
        from molecule_runtime.mcp_render import (
            default_json_settings_path,
            remove_json_mcp_servers,
        )

        descriptor = _read_mcp_descriptor(ctx.plugin_root)
        settings_path = default_json_settings_path(ctx.configs_dir)
        for name, spec in descriptor.items():
            if not isinstance(spec, dict) or spec.get("audience") != "self":
                continue
            if remove_json_mcp_servers(settings_path, name):
                ctx.logger.info(
                    "%s: stripped audience=self MCP %r from %s on uninstall "
                    "(opt-out teardown)", self.plugin_name, name, settings_path,
                )
