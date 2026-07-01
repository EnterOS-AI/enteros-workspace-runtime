"""AGENTS.md auto-generation for Molecule AI workspaces.

Implements the AAIF / Linux Foundation AGENTS.md standard so that peer agents
and orchestration tools can discover this workspace's identity, role, A2A
endpoint, and available tools without reading the full system prompt.

Usage::

    from molecule_runtime.agents_md import generate_agents_md

    generate_agents_md(config_dir="/configs", output_path="/workspace/AGENTS.md")

The function is called automatically at container startup (see main.py).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_agents_md(config_dir: str, output_path: str) -> None:
    """Generate (or regenerate) AGENTS.md from the workspace config.yaml.

    Always overwrites ``output_path`` — no stale-file guard.  Re-calling
    after editing config.yaml produces a fresh file reflecting the changes.

    Args:
        config_dir: Directory containing config.yaml (same convention as
            ``load_config`` in config.py).
        output_path: Absolute path where AGENTS.md will be written.
            The parent directory is expected to exist.
    """
    from molecule_runtime.config import load_config

    cfg = load_config(config_dir)

    # ── A2A Endpoint ─────────────────────────────────────────────────────────
    # AGENT_URL env var takes priority (production deployments behind a proxy).
    # Otherwise derive from the configured a2a.port (default 8000).
    endpoint = os.environ.get("AGENT_URL") or f"http://localhost:{cfg.a2a.port}/a2a"

    # ── Role ─────────────────────────────────────────────────────────────────
    # Fall back to description when the role field is absent so legacy
    # config.yaml files (without a role key) still produce meaningful output.
    role = cfg.role if cfg.role else cfg.description

    # ── MCP Tools ────────────────────────────────────────────────────────────
    # tools (skill names) + plugins (installed plugin names) form the combined
    # capability surface visible to peer agents.
    all_tools = list(cfg.tools) + list(cfg.plugins)
    if all_tools:
        tools_section = "\n".join(f"- {t}" for t in all_tools)
    else:
        tools_section = "None"

    content = (
        f"# {cfg.name}\n"
        f"\n"
        f"**Role:** {role}\n"
        f"\n"
        f"## Description\n"
        f"{cfg.description}\n"
        f"\n"
        f"## A2A Endpoint\n"
        f"{endpoint}\n"
        f"\n"
        f"## MCP Tools\n"
        f"{tools_section}\n"
    )

    Path(output_path).write_text(content, encoding="utf-8")
    logger.info("Generated AGENTS.md at %s for workspace %r", output_path, cfg.name)

    # MUST-FIX (memory WRITE-path reconciliation): with the mailbox kernel ON,
    # also write AGENTS.md into the durable mailbox memory dir
    # (/workspace/.molecule/memory) — the directory prompt.py READS memory
    # snapshots from. The AAIF copy at output_path (typically /workspace/AGENTS.md)
    # stays for peer/tool discovery; this durable copy is what gets injected
    # into the system prompt and survives a restart, so a stale /configs copy
    # can never shadow it. Kernel OFF: unchanged (only output_path is written).
    import molecule_runtime.mailbox_dir as mailbox_dir

    if mailbox_dir.kernel_enabled():
        try:
            mem_target = mailbox_dir.memory_file("AGENTS.md")
            mem_target.write_text(content, encoding="utf-8")
            logger.info("Mirrored AGENTS.md to durable memory %s", mem_target)
        except OSError as exc:
            logger.warning("Could not mirror AGENTS.md to mailbox memory: %s", exc)
