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

Planned as the ecosystem matures (none are implemented yet — rule of
three: promote a class here only after 3+ plugins ship the same custom
shape via their own ``adapters/<runtime>.py``):

* :class:`MCPServerAdaptor` — install a plugin as an MCP server ✅ (issue #847)
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

import json
import os
import shutil
import subprocess
from pathlib import Path

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

    Uninstall reverses the file copies and strips the rule/fragment block by
    marker (best-effort — if the user edited CLAUDE.md manually, only the
    marker line itself is removed).

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
        result = InstallResult(
            plugin_name=self.plugin_name,
            runtime=self.runtime,
            source="plugin",  # overridden by registry caller if source==registry
        )

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

        if memory_blocks:
            joined = "\n\n".join(memory_blocks)
            ctx.append_to_memory(ctx.memory_filename, joined)
            ctx.logger.info(
                "%s: injected %d rule+fragment block(s) into %s",
                self.plugin_name, len(memory_blocks), ctx.memory_filename,
            )

        # 3. Skills — copy each skill dir to /configs/skills/.
        src_skills_dir = ctx.plugin_root / "skills"
        if src_skills_dir.is_dir():
            dst_skills_root = ctx.configs_dir / SKILLS_SUBDIR
            dst_skills_root.mkdir(parents=True, exist_ok=True)
            copied = 0
            for entry in sorted(src_skills_dir.iterdir()):
                if not entry.is_dir():
                    continue
                dst = dst_skills_root / entry.name
                if dst.exists():
                    ctx.logger.debug("%s: skill %s already present, skipping", self.plugin_name, entry.name)
                    continue
                # symlinks=True: copy links AS links, never dereference. A skill
                # tree carrying e.g. `x -> /etc/molecule.env` or `-> /proc/self/
                # environ` must NOT have its target's contents copied into the
                # agent-readable /configs/skills/ (arbitrary-file-read). RFC#2843
                # #32 hardening — load-bearing once #31 admits third-party skills.
                shutil.copytree(entry, dst, symlinks=True)
                copied += 1
                for p in dst.rglob("*"):
                    if p.is_file() and not p.is_symlink():
                        result.files_written.append(str(p.relative_to(ctx.configs_dir)))
            if copied:
                ctx.logger.info("%s: copied %d skill dir(s) to %s", self.plugin_name, copied, dst_skills_root)
        elif (ctx.plugin_root / "SKILL.md").is_file():
            # SKILL.md-at-root shape (RFC#2843 #32): the plugin root IS a single
            # agentskills unit (no skills/<name>/ subdir) — e.g. the seo-all skill
            # fetched to /configs/plugins/seo-all/. Copy the whole plugin dir to
            # /configs/skills/<plugin_name>/ so Claude Code's native skill
            # discovery picks it up. Excludes packaging-only dirs that aren't part
            # of the skill payload.
            dst_skills_root = ctx.configs_dir / SKILLS_SUBDIR
            dst_skills_root.mkdir(parents=True, exist_ok=True)
            dst = dst_skills_root / self.plugin_name
            if dst.exists():
                ctx.logger.debug("%s: skill %s already present, skipping", self.plugin_name, self.plugin_name)
            else:
                # symlinks=True: never dereference a symlink in the skill tree
                # into the agent-readable /configs/skills/ (arbitrary-file-read).
                shutil.copytree(
                    ctx.plugin_root, dst,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "adapters"),
                    symlinks=True,
                )
                for p in dst.rglob("*"):
                    if p.is_file() and not p.is_symlink():
                        result.files_written.append(str(p.relative_to(ctx.configs_dir)))
                ctx.logger.info("%s: copied root SKILL.md skill to %s", self.plugin_name, dst)

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
                        raise PrivilegedPluginInstallError(err)
            except subprocess.TimeoutExpired:
                err = "setup.sh timed out (120s)"
                result.warnings.append(err)
                result.errors.append(err)
                ctx.logger.error("%s: setup.sh timed out", self.plugin_name)
                if self.plugin_name == _PRIVILEGED_MCP_PLUGIN:
                    raise PrivilegedPluginInstallError(err)

        # 5. Hooks — copy hooks/* into <configs>/.claude/hooks/ (Claude Code-
        #    style harness hooks). No-op when the plugin doesn't ship any.
        # 6. Commands — copy commands/*.md into <configs>/.claude/commands/.
        # 7. settings-fragment.json — merge into <configs>/.claude/settings.json,
        #    rewriting ${CLAUDE_DIR} to the absolute install path. Existing
        #    user hooks are preserved (deep-merge by event).
        _install_claude_layer(ctx, result, self.plugin_name)

        return result

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    async def uninstall(self, ctx: InstallContext) -> None:
        # Remove copied skill dirs.
        src_skills_dir = ctx.plugin_root / "skills"
        if src_skills_dir.is_dir():
            for entry in src_skills_dir.iterdir():
                dst = ctx.configs_dir / SKILLS_SUBDIR / entry.name
                if dst.exists() and dst.is_dir():
                    shutil.rmtree(dst)
                    ctx.logger.info("%s: removed %s", self.plugin_name, dst)
        elif (ctx.plugin_root / "SKILL.md").is_file():
            # Mirror the SKILL.md-at-root install (RFC#2843 #32).
            dst = ctx.configs_dir / SKILLS_SUBDIR / self.plugin_name
            if dst.exists() and dst.is_dir():
                shutil.rmtree(dst)
                ctx.logger.info("%s: removed %s", self.plugin_name, dst)

        # Best-effort strip of our markers from CLAUDE.md. Users can always
        # edit manually; we only guarantee the injected block's first line
        # is removed so re-install re-adds cleanly.
        memory_path = ctx.configs_dir / ctx.memory_filename
        if not memory_path.exists():
            return
        text = memory_path.read_text()
        prefix = f"# Plugin: {self.plugin_name} / "
        lines = text.splitlines(keepends=True)
        kept = [line for line in lines if not line.startswith(prefix)]
        if len(kept) != len(lines):
            memory_path.write_text("".join(kept))
            ctx.logger.info("%s: stripped markers from %s", self.plugin_name, ctx.memory_filename)




# ----------------------------------------------------------------------
# Claude Code layer — hooks, slash commands, settings.json fragments.
# Promoted from the molecule-guardrails plugin so any plugin can ship
# these by dropping the right files; no custom adapter needed.
# ----------------------------------------------------------------------

def _install_claude_layer(ctx: InstallContext, result: InstallResult, plugin_name: str) -> None:
    claude_dir = ctx.configs_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    _copy_dir_files(
        ctx.plugin_root / "hooks",
        claude_dir / "hooks",
        result,
        executable_suffix=".sh",
    )
    _copy_dir_files(
        ctx.plugin_root / "commands",
        claude_dir / "commands",
        result,
        only_suffix=".md",
    )
    _merge_settings_fragment(ctx, claude_dir, result, plugin_name)


def _copy_dir_files(
    src: Path,
    dst: Path,
    result: InstallResult,
    executable_suffix: str | None = None,
    only_suffix: str | None = None,
) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if not f.is_file():
            continue
        if only_suffix and f.suffix != only_suffix:
            # When copying hooks, allow .py companion files alongside .sh
            if not (executable_suffix and f.suffix == ".py"):
                continue
        target = dst / f.name
        shutil.copy2(f, target)
        if executable_suffix and f.suffix == executable_suffix:
            target.chmod(0o755)
        result.files_written.append(str(target.relative_to(target.parents[2])))


def _merge_settings_fragment(
    ctx: InstallContext,
    claude_dir: Path,
    result: InstallResult,
    plugin_name: str,
) -> None:
    fragment_path = ctx.plugin_root / "settings-fragment.json"
    if not fragment_path.is_file():
        return
    try:
        fragment = json.loads(fragment_path.read_text())
    except Exception as e:
        result.warnings.append(f"settings-fragment.json invalid: {e}")
        return

    settings_path = claude_dir / "settings.json"
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception:
            existing = {}
    else:
        existing = {}

    # mcpServers are NOT merged here. They are wired through the MCP-wiring PORT
    # (ctx.register_mcp_server → BaseAdapter.register_mcp_server_hook) so that a
    # non-Claude runtime gets the MCP rendered into the file IT reads (codex
    # config.toml, …) instead of being silently written to .claude/settings.json
    # that its runtime never loads (#3159). MCPServerAdaptor parses the
    # mcpServers block and calls the port; this claude-layer path only handles
    # hooks (and any other non-mcpServers settings keys). Dropping mcpServers
    # here also avoids double-writing the same entry on Claude.
    fragment_no_mcp = {k: v for k, v in fragment.items() if k != "mcpServers"}
    if not fragment_no_mcp:
        # The fragment only declared mcpServers — nothing for the claude hook
        # layer to merge. The port owns it; leave settings.json untouched here.
        return

    rewritten = _rewrite_hook_paths(fragment_no_mcp, claude_dir)
    merged = _deep_merge_hooks(existing, rewritten)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n")
    result.files_written.append(str(settings_path.relative_to(ctx.configs_dir)))
    ctx.logger.info("%s: merged hook config into %s", plugin_name, settings_path)


def _rewrite_hook_paths(fragment: dict, claude_dir: Path) -> dict:
    out = json.loads(json.dumps(fragment))  # deep copy via roundtrip
    for handlers in out.get("hooks", {}).values():
        for handler in handlers:
            for h in handler.get("hooks", []):
                cmd = h.get("command", "")
                h["command"] = cmd.replace("${CLAUDE_DIR}", str(claude_dir))
    return out


def _deep_merge_hooks(existing: dict, fragment: dict) -> dict:
    out = dict(existing)
    out.setdefault("hooks", {})
    for event, handlers in fragment.get("hooks", {}).items():
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
    for top_key, val in fragment.items():
        if top_key == "hooks":
            continue
        # mcpServers must be deep-merged: plugin A ships "firecrawl" and
        # plugin B ships "github" → both entries land in settings.json.
        # Using setdefault would skip the fragment's value when the key
        # already exists, so we explicitly handle the dict case.
        if top_key in out and isinstance(out[top_key], dict) and isinstance(val, dict):
            out[top_key] = {**out[top_key], **val}
        else:
            out.setdefault(top_key, val)
    return out


# ----------------------------------------------------------------------
# MCPServerAdaptor — issue #847.
# Promoted from custom adapters after 4 plugin proposals (molecule-firecrawl
# #512, molecule-github-mcp #520, molecule-browser-use #553, mcp-connector
# #573) all shipped the same pattern independently.
# ----------------------------------------------------------------------


def _read_mcp_descriptor(plugin_root: Path) -> dict[str, dict]:
    """Parse the runtime-agnostic ``mcpServers`` descriptor a plugin ships.

    The plugin is the SSOT for the descriptor (RFC §2b). Today it is carried in
    ``settings-fragment.json``'s ``mcpServers`` block — historically the Claude
    adapter's *rendering*, now read as the canonical descriptor and re-rendered
    per runtime via the MCP-wiring PORT. A dedicated ``mcp-servers.json`` (a
    pure descriptor, no Claude framing) takes precedence if present, so a plugin
    can drop the Claude-specific filename entirely once consumers migrate.

    Returns ``{name: spec}`` (possibly empty). Malformed JSON yields ``{}``.
    """
    for candidate in ("mcp-servers.json", "settings-fragment.json"):
        path = plugin_root / candidate
        if not path.is_file():
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


class MCPServerAdaptor:
    """Sub-type adaptor for plugins that wrap an MCP server.

    The plugin ships:

    * ``settings-fragment.json`` with an ``mcpServers`` block — standard
      Claude Code ``claude_desktop_config`` format, e.g.:

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

      1. The ``mcpServers`` descriptor (from ``mcp-servers.json`` or
         ``settings-fragment.json``) is parsed and each entry is wired via the
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
         ``settings.json`` on uninstall. MCP server configurations are
         often shared with other tools or manually curated, so removing
         them could break a user's setup. The user must remove them
         manually if desired.

    Usage — in the plugin's per-runtime adapter file:

    .. code-block:: python

        # plugins/<name>/adapters/claude_code.py
        from molecule_runtime.plugins_registry.builtins import MCPServerAdaptor as Adaptor
    """

    def __init__(self, plugin_name: str, runtime: str) -> None:
        self.plugin_name = plugin_name
        self.runtime = runtime

    async def install(self, ctx: InstallContext) -> InstallResult:
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
                ctx.register_mcp_server(name, spec)
                ctx.logger.info("%s: wired MCP server %r via register_mcp_server (runtime=%s)",
                                self.plugin_name, name, self.runtime)
            except NotImplementedError as exc:
                # A runtime whose native MCP-config renderer is not yet
                # implemented (gemini/hermes stubs). For the privileged
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
        sub = await AgentskillsAdaptor(self.plugin_name, self.runtime).install(ctx)
        result.files_written.extend(sub.files_written)
        result.warnings.extend(sub.warnings)
        result.errors.extend(sub.errors)
        return result

    async def uninstall(self, ctx: InstallContext) -> None:
        # Delegate to AgentskillsAdaptor for skills + rules cleanup.
        # NOTE: mcpServers entries are intentionally NOT removed (see class docstring).
        await AgentskillsAdaptor(self.plugin_name, self.runtime).uninstall(ctx)
