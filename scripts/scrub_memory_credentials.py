#!/usr/bin/env python3
"""Operator scrub workflow for already-captured credentials in stored memories.

Issue #2832 (SECURITY). Reads every memory entry for a workspace from the
platform API, runs the same redactor used by the live auto-memory write path,
and writes the redacted content back in place.

Usage::

    export WORKSPACE_ID=...
    export PLATFORM_URL=https://platform.example.com
    # Auth: either set MOLECULE_AUTH_TOKEN or rely on CONFIGS_DIR/.auth_token
    python scripts/scrub_memory_credentials.py [--dry-run]

The script is idempotent: re-running it on already-redacted content is a
no-op (it will detect no change and skip the PUT).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

from molecule_runtime.memory_redaction import redact_credentials_text

logger = logging.getLogger(__name__)


def _auth_token() -> str | None:
    """Resolve bearer token from env or persisted auth file."""
    if token := os.environ.get("MOLECULE_AUTH_TOKEN"):
        return token
    configs_dir = os.environ.get("CONFIGS_DIR", "/configs")
    token_path = Path(configs_dir) / ".auth_token"
    if token_path.exists():
        return token_path.read_text().strip()
    return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def _fetch_memories(
    client: httpx.AsyncClient,
    platform_url: str,
    workspace_id: str,
    token: str,
) -> list[dict]:
    memories: list[dict] = []
    page = 1
    while True:
        resp = await client.get(
            f"{platform_url}/workspaces/{workspace_id}/memories",
            headers=_headers(token),
            params={"limit": 250, "page": page},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data if isinstance(data, list) else data.get("data", [])
        if not batch:
            break
        memories.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return memories


async def _update_memory(
    client: httpx.AsyncClient,
    platform_url: str,
    workspace_id: str,
    token: str,
    memory_id: str,
    content: str,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True
    # Try the update endpoint; fall back to a generic error if unsupported.
    resp = await client.put(
        f"{platform_url}/workspaces/{workspace_id}/memories/{memory_id}",
        headers=_headers(token),
        json={"content": content},
        timeout=30.0,
    )
    if resp.status_code in (200, 201, 204):
        return True
    logger.warning("Update failed for memory %s: HTTP %s - %s", memory_id, resp.status_code, resp.text)
    return False


async def scrub_workspace(
    platform_url: str,
    workspace_id: str,
    token: str,
    dry_run: bool,
) -> dict[str, int]:
    async with httpx.AsyncClient() as client:
        memories = await _fetch_memories(client, platform_url, workspace_id, token)
        stats = {"scanned": len(memories), "updated": 0, "unchanged": 0, "errors": 0}
        for entry in memories:
            original = entry.get("content", "")
            memory_id = entry.get("id")
            if not memory_id:
                stats["errors"] += 1
                continue
            scrubbed = redact_credentials_text(original)
            if scrubbed == original:
                stats["unchanged"] += 1
                continue
            logger.info(
                "Redacting memory %s (len %d -> %d)", memory_id, len(original), len(scrubbed)
            )
            ok = await _update_memory(
                client, platform_url, workspace_id, token, memory_id, scrubbed, dry_run
            )
            if ok:
                stats["updated"] += 1
            else:
                stats["errors"] += 1
        return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description="Scrub credentials from stored memories")
    parser.add_argument("--dry-run", action="store_true", help="Scan and report but do not write back")
    parser.add_argument("--workspace-id", default=os.environ.get("WORKSPACE_ID"))
    parser.add_argument("--platform-url", default=os.environ.get("PLATFORM_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=_auth_token())
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args.workspace_id:
        logger.error("WORKSPACE_ID or --workspace-id is required")
        return 1
    if not args.token:
        logger.error("No auth token found (set MOLECULE_AUTH_TOKEN or CONFIGS_DIR/.auth_token)")
        return 1

    stats = await scrub_workspace(args.platform_url, args.workspace_id, args.token, args.dry_run)
    logger.info(
        "Scrub complete: scanned=%(scanned)d updated=%(updated)d unchanged=%(unchanged)d errors=%(errors)d",
        stats,
    )
    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
