"""Snapshot scrubbing — strip secrets and internal details from hibernation snapshots.

Issue #823 (sub of #799). Before the workspace runtime serializes a memory
snapshot for hibernation, every memory entry's content must pass through
this scrubber so an attacker who obtains a snapshot blob cannot recover
API keys, tokens, or env-var credentials.

The actual credential-pattern library lives in
``molecule_runtime.memory_redaction`` (#2832) and is shared with the
runtime's live memory write path.
"""
from __future__ import annotations

from typing import Any

from molecule_runtime.memory_redaction import redact_credentials_text


# Substring markers that identify content from the run_code sandbox tool.
# Any memory entry tagged with this source is excluded wholesale from the
# snapshot — the arbitrary subprocess output cannot be safely scrubbed by
# pattern alone (attacker could print `echo "innocent"` but have hidden
# secrets in stderr or file handles).
_SANDBOX_TOOL_MARKERS = (
    "source=sandbox",
    "tool=run_code",
    "[sandbox_output]",
)


def scrub_content(content: str) -> str:
    """Return `content` with credential-shaped values replaced.

    Idempotent — running scrub_content on already-redacted output is a no-op
    because ``[REDACTED:<kind>]`` does not match any credential pattern.
    """
    if not content:
        return content
    return redact_credentials_text(content)


def is_sandbox_content(content: str) -> bool:
    """Return True if `content` originates from the run_code sandbox tool.

    Sandbox output can contain arbitrary subprocess stdout/stderr that may
    include secrets the scrubber wouldn't recognize (e.g. printed via a
    custom format). Entries matching this check should be excluded from
    the snapshot entirely rather than scrubbed.
    """
    if not content:
        return False
    lower = content.lower()
    return any(marker in lower for marker in _SANDBOX_TOOL_MARKERS)


def scrub_memory_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Scrub a single memory entry for snapshot inclusion.

    Returns a new dict with secrets redacted, or None if the entry must be
    excluded entirely (sandbox-sourced content).

    The input dict is treated as read-only — callers should use the returned
    value and not mutate the original.
    """
    content = entry.get("content", "")
    if is_sandbox_content(content):
        return None
    scrubbed = dict(entry)
    scrubbed["content"] = scrub_content(content)
    return scrubbed


def scrub_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Scrub a full snapshot payload before serialization.

    Walks the `memories` list, scrubs each entry's content, and drops
    sandbox-sourced entries. Other snapshot fields (workspace metadata,
    config, etc.) pass through unchanged — they are not expected to contain
    user-supplied secret-bearing content.

    Returns a new dict; the input is not mutated.
    """
    out = dict(snapshot)
    memories = snapshot.get("memories") or []
    scrubbed_list = []
    for entry in memories:
        cleaned = scrub_memory_entry(entry)
        if cleaned is not None:
            scrubbed_list.append(cleaned)
    out["memories"] = scrubbed_list
    return out
