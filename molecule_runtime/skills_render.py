"""Per-runtime materializers for PLUGIN-DELIVERED SKILLS (the skills-surfacing PORT).

This module is the third sibling of the per-runtime render family:

  * :mod:`molecule_runtime.mcp_render`     — gives a runtime its *tools*
    (MCP descriptor → native MCP-config file).
  * :mod:`molecule_runtime.persona_render` — gives a runtime its *identity*
    (canonical persona → native identity file).
  * **this module**                        — gives a runtime its *skills*
    (the canonical skills dir → the runtime's NATIVE skill-discovery surface).

Canonical source
----------------
``<configs>/skills`` (:data:`CANONICAL_SKILLS_SUBDIR`, the same
``plugins_registry.protocol.SKILLS_SUBDIR`` the AgentskillsAdaptor writes to).
Every skill converges there: workspace-declared skills are delivered there by
provisioning, and plugin-shipped skills are copied there on install
(``AgentskillsAdaptor.install`` — both the ``skills/<name>/`` and the
SKILL.md-at-root shapes). Materialization is therefore DIRECTORY-LEVEL by
contract: we surface the *directory* (symlink or config pointer), never
per-skill copies, so a post-boot plugin install that drops a new skill into
``/configs/skills`` is visible to the runtime's next native scan without
another materialization pass.

The bug this closes (template-claude-code#224, generalized)
-----------------------------------------------------------
Plugin skills were installed into ``/configs/skills`` yet INVISIBLE to the
agent, because no runtime's native binary reads ``/configs/skills``: each
runtime discovers skills from its OWN location. template-claude-code#224
(fa4c2f3) fixed exactly one runtime with an entrypoint symlink
(``~/.claude/skills -> /configs/skills``) — a per-template patch for what is a
cross-runtime contract. This module is the ONE canonical implementation: every
runtime declares its own ``(target_resolver, materializer)`` in
``_RUNTIME_SKILLS`` and the boot path dispatches on ``adapter.name()`` —
exactly like ``mcp_render._RUNTIME_SPECS`` / ``persona_render._RUNTIME_PERSONA``.

Per-runtime native conventions (each one PINNED, not assumed)
-------------------------------------------------------------
* Claude Code → dir symlink ``~/.claude/skills -> <configs>/skills``.
  Claude Code discovers personal skills ONLY under ``~/.claude/skills``
  (verified live on the agents-team platform agent 2026-07-05 — the #224
  evidence). The template's entrypoint creates the same link as root at boot;
  this materializer is the SDK-owned, runtime-agnostic assertion of it
  (belt-and-suspenders: both are idempotent, neither fights the other, and a
  template whose entrypoint predates #224 is healed here).
* Codex → dir symlink ``$CODEX_HOME/skills/molecule -> <configs>/skills``.
  codex discovers skills automatically from ``$CODEX_HOME/skills`` (fallback
  ``~/.codex/skills``) — pinned against the shipped ``@openai/codex@0.130.0``
  binary via its app-server ``skills/list`` (a planted skill listed with
  ``scope: "user"``, and BOTH a symlinked root and a symlinked nested group
  dir were followed; verified 2026-07-05). We link a NESTED group dir (not the
  root) because codex materializes its own ``.system`` skills into the skills
  root at first run — linking the root would make codex write its system
  skills into ``/configs/skills``.
* OpenClaw → dir symlink ``<openclaw workspace>/skills -> <configs>/skills``.
  The gateway's skill loader merges bundled/managed/extra roots with the agent
  workspace's ``skills/`` dir having the HIGHEST precedence, and
  ``loadSkillsFromDirSafe`` realpath-resolves the root before the per-file
  root-boundary check — so a symlinked root loads fine (pinned by reading
  openclaw@2026.6.11 ``dist/workspace-*.js``; the ``skills.load
  .allowSymlinkTargets`` trust gate applies to Skill-Workshop WRITES and
  per-file symlinks, not to reading through a symlinked root). The workspace
  dir follows openclaw's own ``resolveDefaultAgentWorkspaceDir`` rule
  (``~/.openclaw/workspace``, profile-suffixed only for a non-default
  ``OPENCLAW_PROFILE``) — the same rule the openclaw template adapter pins.
* Hermes → merge ``skills.external_dirs: [<configs>/skills]`` into
  ``$HERMES_HOME/config.yaml``. hermes-agent's ``skills_list`` scans
  ``~/.hermes/skills`` PLUS ``skills.external_dirs`` from config.yaml,
  re-reading the config on every scan (pinned by reading the hermes-agent
  fork: ``agent/skill_utils.get_external_skills_dirs`` +
  ``tools/skills_tool._find_all_skills``). A config pointer — not a symlink
  into ``~/.hermes/skills`` — because hermes' curator treats that dir as its
  own managed library, and external_dirs is hermes' first-class mechanism for
  exactly this. Only directories that EXIST are honored, so the materializer
  creates the canonical dir first.
* google-adk → DOCUMENTED SKIP (no on-disk skill discovery exists to
  materialize into). The ADK ``LlmAgent`` consumes the assembled instruction
  string; plugin skills already reach it through ``_common_setup`` →
  ``build_system_prompt(loaded_skills=…)``, which scans the plugin skill dirs
  directly. Post-boot installs surface on the next executor rebuild (plugin
  installs restart the runtime). This is a satisfied-elsewhere skip, logged
  with its reason — not a silent no-op and not a failure.
* gemini / anything unmapped → deliberate fail-LOUD
  ``NotImplementedError``. There is NO universal "base" skill location (the
  claude fallback that is safe for MCP/persona would silently write to a file
  an unmapped runtime never reads — the #3159 class of bug), so an unverified
  runtime must be heard, not guessed at.

Failure stance
--------------
Skills are an ordinary capability (not privileged like the management MCP), so
materialization must never brick a boot — but it must NEVER no-op silently
either. Concrete failures (a REAL directory squatting the link target, an OS
error, an unmapped runtime) raise :class:`SkillsMaterializeError` /
``NotImplementedError``; the boot-path caller
(``BaseAdapter.materialize_skills``) downgrades them to ``logger.error`` with
remediation, keeping the boot alive and the failure loud.

All materializers are PURE filesystem/config renderers: idempotent
(re-running converges to the same state), testable without any runtime binary,
and safe to re-assert on every boot.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

# SSOT for the underscore dispatch-key canonicalization — shared with
# mcp_render/persona_render so ``claude-code`` -> ``claude_code`` normalizes
# identically across all three ports.
from molecule_runtime.mcp_render import normalize_runtime

# SSOT for the canonical skills subdir — the SAME constant the
# AgentskillsAdaptor installs plugin skills under.
from molecule_runtime.plugins_registry.protocol import SKILLS_SUBDIR as CANONICAL_SKILLS_SUBDIR

logger = logging.getLogger(__name__)


class SkillsMaterializeError(RuntimeError):
    """A runtime's native skill surface could not be satisfied.

    Raised when the materializer KNOWS the runtime's convention but cannot
    apply it (e.g. a real directory squats the symlink target, or the
    filesystem write failed). Distinct from ``NotImplementedError`` (runtime
    convention unverified/unmapped). Callers downgrade to a loud, non-fatal
    boot error — skills must never brick a boot, but must never fail silently.
    """


# ---------------------------------------------------------------------------
# Canonical source — the runtime-agnostic INPUT to every materializer.
# ---------------------------------------------------------------------------

def canonical_skills_dir(config_path: str | os.PathLike) -> Path:
    """The one canonical skills directory (``<configs>/skills``) where
    workspace-declared AND plugin-installed skills converge."""
    return Path(config_path) / CANONICAL_SKILLS_SUBDIR


# ---------------------------------------------------------------------------
# Native-target resolvers (uniform (config_path) -> Path signature).
# ---------------------------------------------------------------------------

def _claude_skills_target(config_path: str | os.PathLike) -> Path:
    """Claude Code's personal-skills dir — the ONLY dir its CLI scans."""
    return Path(os.path.expanduser("~")) / ".claude" / "skills"


def _codex_home() -> Path:
    """codex's home — ``$CODEX_HOME``, falling back to ``~/.codex`` (the same
    rule the codex CLI itself documents and the codex template adapter uses)."""
    env = (os.environ.get("CODEX_HOME") or "").strip()
    return Path(env) if env else Path(os.path.expanduser("~")) / ".codex"


# The nested group dir we own inside $CODEX_HOME/skills. Codex discovers
# nested skill groups (verified against 0.130.0), and a nested link keeps
# codex's self-materialized ``.system`` skills OUT of /configs/skills.
CODEX_SKILLS_GROUP = "molecule"


def _codex_skills_target(config_path: str | os.PathLike) -> Path:
    return _codex_home() / "skills" / CODEX_SKILLS_GROUP


def openclaw_workspace_dir() -> Path:
    """Resolve the workspace dir OpenClaw's gateway actually reads.

    Mirrors openclaw's own ``resolveDefaultAgentWorkspaceDir`` (pinned against
    openclaw 2026.6.11, dist/config-utils — the same rule the openclaw template
    adapter's ``_openclaw_workspace_dir`` pins): ``~/.openclaw/workspace``,
    suffixed to ``~/.openclaw/workspace-<profile>`` ONLY when
    ``OPENCLAW_PROFILE`` is a non-``default`` value. Exposed publicly so the
    openclaw template can converge on this SSOT instead of keeping its own
    copy (it may not import this until its pinned runtime version ships it —
    no version floor is imposed)."""
    profile = (os.environ.get("OPENCLAW_PROFILE") or "").strip().lower()
    if profile and profile != "default":
        return Path(os.path.expanduser(f"~/.openclaw/workspace-{profile}"))
    return Path(os.path.expanduser("~/.openclaw/workspace"))


def _openclaw_skills_target(config_path: str | os.PathLike) -> Path:
    """The agent workspace's ``skills/`` root — openclaw's HIGHEST-precedence
    skill root, scanned natively by the gateway."""
    return openclaw_workspace_dir() / "skills"


def _hermes_home() -> Path:
    """hermes-agent's home — ``$HERMES_HOME``, falling back to ``~/.hermes``
    (mirrors ``hermes_constants.get_hermes_home``)."""
    env = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(env) if env else Path(os.path.expanduser("~")) / ".hermes"


def _hermes_skills_target(config_path: str | os.PathLike) -> Path:
    """hermes' native pointer lives in its config file, not a directory."""
    return _hermes_home() / "config.yaml"


# ---------------------------------------------------------------------------
# Materializer primitives.
# ---------------------------------------------------------------------------

def _ensure_dir_symlink(target: Path, source: Path) -> Path:
    """Idempotently ensure ``target`` is a directory symlink to ``source``.

    * ``source`` (the canonical skills dir) is created if absent — a link to a
      missing dir would make native scanners see ENOENT until the first plugin
      install.
    * A ``target`` that is already the correct symlink is a no-op.
    * A ``target`` that is a WRONG/stale symlink (e.g. restored by a backup
      rsync from a previous layout) is re-pointed.
    * A ``target`` that is a REAL directory (or any non-symlink) is NEVER
      clobbered — it could hold agent-authored skills (the same guard
      template-claude-code#224's entrypoint applies). That raises
      :class:`SkillsMaterializeError` so the condition is heard.
    """
    source = Path(source)
    target = Path(target)
    source.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        try:
            current = os.readlink(target)
        except OSError:
            current = None
        if current is not None and Path(current) == source:
            return target
        # Stale/wrong symlink — re-point (safe: only the LINK is replaced).
        target.unlink()
    elif target.exists():
        raise SkillsMaterializeError(
            f"skills materialization: {target} exists and is not a symlink — "
            f"refusing to clobber a real directory (it may hold agent-authored "
            f"skills). Plugin skills in {source} will NOT be natively visible "
            f"until it is moved aside."
        )

    try:
        os.symlink(str(source), str(target), target_is_directory=True)
    except OSError as exc:
        raise SkillsMaterializeError(
            f"skills materialization: could not link {target} -> {source}: {exc}"
        ) from exc
    return target


# ---------------------------------------------------------------------------
# Per-runtime materializers — uniform (config_path) -> Path | None signature.
# Return: the native surface written/asserted, or None for a documented skip.
# ---------------------------------------------------------------------------

def materialize_claude_skills(config_path: str | os.PathLike) -> Path:
    """Claude Code — ``~/.claude/skills -> <configs>/skills`` dir symlink.

    Matches (and at boot re-asserts) template-claude-code#224's entrypoint
    symlink. Both are idempotent: whichever runs first creates the link, the
    other converges. Keeping the entrypoint copy is deliberate
    belt-and-suspenders — it runs as root BEFORE the runtime and also fixes
    /configs/skills ownership."""
    return _ensure_dir_symlink(_claude_skills_target(config_path), canonical_skills_dir(config_path))


def materialize_codex_skills(config_path: str | os.PathLike) -> Path:
    """Codex — ``$CODEX_HOME/skills/molecule -> <configs>/skills`` dir symlink.

    A NESTED group link (codex discovers nested skill groups; pinned against
    the 0.130.0 binary via app-server ``skills/list``) so codex's
    self-materialized ``.system`` skills never land inside /configs/skills."""
    return _ensure_dir_symlink(_codex_skills_target(config_path), canonical_skills_dir(config_path))


def materialize_openclaw_skills(config_path: str | os.PathLike) -> Path:
    """OpenClaw — ``<openclaw workspace>/skills -> <configs>/skills`` symlink.

    The workspace skills root is openclaw's highest-precedence native root and
    its loader realpath-resolves the root, so the link is honored (pinned
    against openclaw@2026.6.11). Known trade-off: openclaw's Skill Workshop
    WRITES through a symlinked workspace path are refused by its own trust
    gate unless the operator configures ``skills.load.allowSymlinkTargets`` —
    a loud native error naming the fix, accepted because plugin-managed skills
    are not the Workshop's to mutate."""
    return _ensure_dir_symlink(_openclaw_skills_target(config_path), canonical_skills_dir(config_path))


def materialize_hermes_skills(config_path: str | os.PathLike) -> Path:
    """Hermes — merge ``skills.external_dirs: [<configs>/skills]`` into
    ``$HERMES_HOME/config.yaml`` (hermes' first-class external-skill-dirs
    mechanism; the config is re-read on every native skill scan, so this is
    dir-level and post-boot-install-visible like the symlink runtimes).

    Idempotent YAML merge: every other key is preserved; the entry is added
    once. hermes only honors external dirs that EXIST, so the canonical dir is
    created first. NOTE: a YAML round-trip does not preserve comments — hermes'
    own config writer (``hermes_cli/config.py``) already round-trips the same
    file, so this introduces no new loss."""
    source = canonical_skills_dir(config_path)
    source.mkdir(parents=True, exist_ok=True)

    config_file = _hermes_skills_target(config_path)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_file.is_file():
        try:
            loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
            elif loaded is not None:
                raise SkillsMaterializeError(
                    f"skills materialization: {config_file} is not a YAML mapping "
                    f"— refusing to overwrite an unrecognized hermes config."
                )
        except yaml.YAMLError as exc:
            raise SkillsMaterializeError(
                f"skills materialization: {config_file} is unparseable YAML "
                f"({exc}) — refusing to overwrite a config hermes may still "
                f"partially honor."
            ) from exc

    skills_cfg = data.get("skills")
    if not isinstance(skills_cfg, dict):
        skills_cfg = {}
    raw_dirs = skills_cfg.get("external_dirs")
    if isinstance(raw_dirs, str):
        dirs = [raw_dirs]
    elif isinstance(raw_dirs, list):
        dirs = [str(d) for d in raw_dirs]
    else:
        dirs = []

    entry = str(source)
    if entry not in dirs:
        dirs.append(entry)
        skills_cfg["external_dirs"] = dirs
        data["skills"] = skills_cfg
        try:
            config_file.write_text(
                yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            raise SkillsMaterializeError(
                f"skills materialization: could not write {config_file}: {exc}"
            ) from exc
        logger.info("skills materialization: added %s to skills.external_dirs in %s", entry, config_file)
    return config_file


def materialize_google_adk_skills(config_path: str | os.PathLike) -> None:
    """google-adk — DOCUMENTED SKIP (satisfied through the prompt path).

    ADK's ``LlmAgent`` has no on-disk skill discovery: its native skill
    surface IS the assembled instruction, and plugin skills already reach it —
    ``BaseAdapter._common_setup`` loads every plugin's ``skills/`` dir and
    ``build_system_prompt`` embeds each skill's instructions. Post-boot
    installs surface on the next executor rebuild (plugin installs restart the
    runtime). Returns ``None``; the caller logs the documented reason so this
    is never mistaken for a silent no-op."""
    logger.info(
        "skills materialization: google-adk has no on-disk skill discovery — "
        "plugin skills are surfaced through the assembled instruction "
        "(_common_setup -> build_system_prompt), refreshed on restart."
    )
    return None


def materialize_gemini_skills(config_path: str | os.PathLike) -> Path:
    """TODO: gemini-cli's native skill-discovery convention is unverified.

    gemini-cli is not one of the 5 pinned live runtimes and its skill
    discovery (if any) has not been verified against a shipped binary. Rather
    than guess a location its runtime never reads (the #3159 class of bug),
    this is a marked fail-loud stub — implement concretely once pinned:
    one dict entry + one materializer, no other change."""
    raise NotImplementedError(
        "gemini skills materialization not implemented — native skill "
        "discovery convention unverified"
    )


# ===========================================================================
# Per-runtime dispatch — the production wiring.
# ===========================================================================
# runtime -> (target_resolver, materializer). Mirrors mcp_render._RUNTIME_SPECS
# and persona_render._RUNTIME_PERSONA. UNLIKE those ports there is NO
# claude_code default for unmapped runtimes: skills have no universal base
# location, and defaulting would silently write a link no other runtime reads
# (the #3159 flaw). Unmapped runtimes fail loud (NotImplementedError).
_RUNTIME_SKILLS: dict[str, tuple] = {
    "claude_code": (_claude_skills_target, materialize_claude_skills),
    "codex": (_codex_skills_target, materialize_codex_skills),
    "openclaw": (_openclaw_skills_target, materialize_openclaw_skills),
    "hermes": (_hermes_skills_target, materialize_hermes_skills),
    # google-adk: no on-disk discovery — documented, satisfied via the prompt
    # path (see materialize_google_adk_skills). Target resolver yields None.
    "google_adk": (lambda _p: None, materialize_google_adk_skills),
    # gemini: convention unverified — deliberate fail-loud stub.
    "gemini": (lambda _p: None, materialize_gemini_skills),
}

# Runtimes whose materializer is a deliberate fail-loud stub (unverified), and
# runtimes with a documented not-on-disk skip. Kept as introspectable sets so
# the render-matrix completeness test pins every live runtime to ONE stance.
_UNVERIFIED_RUNTIMES = frozenset({"gemini"})
_PROMPT_EMBEDDED_RUNTIMES = frozenset({"google_adk"})


def is_skills_supported(runtime: str) -> bool:
    """True when this runtime has a CONCRETE on-disk skills materializer.

    ``google_adk`` reports False here (its surface is the prompt, not a disk
    location) — use :func:`is_skills_satisfied` for the contract-level
    question "do plugin skills reach this runtime's agent?"."""
    key = normalize_runtime(runtime)
    return (
        key in _RUNTIME_SKILLS
        and key not in _UNVERIFIED_RUNTIMES
        and key not in _PROMPT_EMBEDDED_RUNTIMES
    )


def is_skills_satisfied(runtime: str) -> bool:
    """True when plugin skills verifiably reach this runtime's agent — via an
    on-disk materializer OR the documented prompt-embedded path."""
    key = normalize_runtime(runtime)
    return key in _RUNTIME_SKILLS and key not in _UNVERIFIED_RUNTIMES


def skills_target_for(runtime: str, config_path: str | os.PathLike) -> Path | None:
    """The native surface (dir/config file) the runtime discovers skills from,
    or ``None`` for a prompt-embedded runtime. Raises ``NotImplementedError``
    for an unmapped runtime (no silent claude fallback — see module doc)."""
    key = normalize_runtime(runtime)
    spec = _RUNTIME_SKILLS.get(key)
    if spec is None:
        raise NotImplementedError(
            f"skills materialization not implemented for runtime {runtime!r} — "
            "no verified native skill-discovery convention (add a "
            "_RUNTIME_SKILLS entry once pinned)"
        )
    return spec[0](config_path)


def materialize_skills_for(runtime: str, config_path: str | os.PathLike) -> Path | None:
    """Materialize the canonical skills dir into ``runtime``'s native surface.

    Returns the path written/asserted, or ``None`` for a documented
    prompt-embedded runtime (google-adk). Raises:

    * ``NotImplementedError`` — unmapped/unverified runtime (fail-loud).
    * :class:`SkillsMaterializeError` — known convention, unsatisfiable state
      (real dir squatting the link target, unwritable config, …).

    The boot caller (``BaseAdapter.materialize_skills``) downgrades both to a
    LOUD non-fatal error — skills never brick a boot, never no-op silently.
    """
    key = normalize_runtime(runtime)
    spec = _RUNTIME_SKILLS.get(key)
    if spec is None:
        raise NotImplementedError(
            f"skills materialization not implemented for runtime {runtime!r} — "
            "no verified native skill-discovery convention (add a "
            "_RUNTIME_SKILLS entry once pinned)"
        )
    return spec[1](config_path)
