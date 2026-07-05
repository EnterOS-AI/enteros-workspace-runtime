"""Memory consolidation loop.

When an agent is idle (no active tasks for a configurable period),
the consolidation loop wakes up and summarizes noisy local memory
entries into dense, high-value knowledge facts.

Similar to human sleep consolidation — raw scratchpad entries get
compressed into reusable knowledge.
"""

import asyncio
import logging
import os

import httpx

from molecule_runtime.memory_redaction import redact_credentials_text
from molecule_runtime.platform_auth import auth_headers
from molecule_runtime.platform_auth import validate_workspace_id as _validate_workspace_id

logger = logging.getLogger(__name__)

if os.path.exists("/.dockerenv") or os.environ.get("DOCKER_VERSION"):
    PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://host.docker.internal:8080")
else:
    PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8080")
# Validate WORKSPACE_ID (CWE-20, issue #14) — interpolated into
# /workspaces/{WORKSPACE_ID}/memories URL paths below.
_WORKSPACE_ID_raw = os.environ.get("WORKSPACE_ID")
if not _WORKSPACE_ID_raw:
    raise RuntimeError("WORKSPACE_ID environment variable is required but not set")
try:
    WORKSPACE_ID = _validate_workspace_id(_WORKSPACE_ID_raw)
except ValueError as _exc:
    raise RuntimeError(f"WORKSPACE_ID failed validation: {_exc}") from _exc
CONSOLIDATION_INTERVAL = float(os.environ.get("CONSOLIDATION_INTERVAL", "300"))  # 5 min
CONSOLIDATION_THRESHOLD = int(os.environ.get("CONSOLIDATION_THRESHOLD", "10"))  # min memories before consolidating


def _mirror_consolidated_to_mailbox(text: str) -> None:
    """Append a consolidated-memory line to the durable mailbox memory snapshot.

    MUST-FIX (memory WRITE-path reconciliation): consolidation's primary sink
    is the platform ``/memories`` API, from which the platform pushes snapshots
    to ``/configs`` — a path that can go stale between provisions. With the
    mailbox kernel ON we ALSO append the consolidated fact to
    ``/workspace/.molecule/memory/MEMORY.md`` (the durable dir ``prompt.py``
    reads), so consolidated knowledge is injected on the next session even if
    the ``/configs`` push is stale, and a stale ``/configs`` copy can never
    shadow it. Best-effort + kernel-gated: no-op / never raises when off.
    """
    import molecule_runtime.mailbox_dir as mailbox_dir

    if not mailbox_dir.kernel_enabled():
        return
    try:
        target = mailbox_dir.memory_file("MEMORY.md")
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"- [Consolidated] {text}\n")
    except OSError as exc:
        logger.warning("Consolidation: could not mirror to mailbox memory: %s", exc)


class ConsolidationLoop:
    """Background loop that consolidates local memories when idle."""

    def __init__(self, agent=None):
        self.agent = agent
        self._running = False

    async def start(self):
        """Start the consolidation loop."""
        self._running = True
        logger.info("Memory consolidation loop started (interval=%ss, threshold=%d)",
                     CONSOLIDATION_INTERVAL, CONSOLIDATION_THRESHOLD)

        while self._running:
            await asyncio.sleep(CONSOLIDATION_INTERVAL)

            if not self._running:
                break

            try:
                await self._consolidate()
            except Exception as e:
                logger.warning("Consolidation error: %s", e)

    async def _consolidate(self):
        """Check if consolidation is needed and run it."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch local memories
            resp = await client.get(
                f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/memories",
                params={"scope": "LOCAL"},
                headers=auth_headers(),
            )
            if resp.status_code != 200:
                return

            memories = resp.json()
            if len(memories) < CONSOLIDATION_THRESHOLD:
                return

            logger.info("Consolidating %d local memories", len(memories))

            # Build a summary of all local memories
            contents = [m["content"] for m in memories]
            summary_prompt = (
                "Summarize the following workspace memories into 3-5 key facts. "
                "Each fact should be a single, clear sentence capturing the most "
                "important and reusable knowledge:\n\n"
                + "\n".join(f"- {c}" for c in contents)
            )

            # Use the agent to generate the summary if available
            summary = ""
            if self.agent:
                try:
                    result = await self.agent.ainvoke(
                        {"messages": [("user", summary_prompt)]},
                        config={"configurable": {"thread_id": "consolidation"}},
                    )
                    messages = result.get("messages", [])
                    summary = ""
                    for msg in reversed(messages):
                        content = getattr(msg, "content", "")
                        if isinstance(content, str) and content.strip():
                            msg_type = getattr(msg, "type", "")
                            if msg_type != "human":
                                summary = content
                                break

                    if summary:
                        summary = redact_credentials_text(summary)
                        # Store consolidated summary as a TEAM memory — only delete originals if POST succeeds
                        resp = await client.post(
                            f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/memories",
                            json={"content": f"[Consolidated] {summary}", "scope": "TEAM"},
                            headers=auth_headers(),
                        )
                        if resp.status_code in (200, 201):
                            # Safe to delete originals — consolidated version is saved
                            for m in memories:
                                await client.delete(
                                    f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/memories/{m['id']}",
                                    headers=auth_headers(),
                                )
                            logger.info("Consolidated %d memories into team knowledge", len(memories))
                            # Also persist to durable mailbox memory (kernel-on).
                            _mirror_consolidated_to_mailbox(summary)
                        else:
                            logger.warning("Consolidation POST failed (status %d) — keeping originals", resp.status_code)
                except Exception as e:
                    logger.error(
                        "CONSOLIDATION: Agent summarization failed (rate limit? model error?): %s. "
                        "Falling back to simple concatenation.", e
                    )
                    # Fall through to concatenation below

            # Fallback: concatenate without agent summarization
            if not (self.agent and summary):
                combined = " | ".join(contents[:20])
                combined = redact_credentials_text(combined)
                resp = await client.post(
                    f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/memories",
                    json={"content": f"[Consolidated] {combined}", "scope": "TEAM"},
                    headers=auth_headers(),
                )
                if resp.status_code not in (200, 201):
                    logger.warning(
                        "Consolidation concatenation POST failed (status %d) — "
                        "memories may not be persisted",
                        resp.status_code,
                    )
                else:
                    logger.info("Consolidated %d memories via concatenation fallback", len(memories))
                    # Also persist to durable mailbox memory (kernel-on).
                    _mirror_consolidated_to_mailbox(combined)

    def stop(self):
        self._running = False
