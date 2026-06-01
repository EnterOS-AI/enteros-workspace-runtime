"""Shared runtime helpers for A2A-backed workspace executors."""

from __future__ import annotations

import json
from typing import Any

from a2a.server.agent_execution import RequestContext

# SSOT (runtime #2914): `set_current_task` previously had a SECOND, divergent
# definition here that opened a raw `httpx.AsyncClient` and posted the
# heartbeat UNAUTHENTICATED. Every adapter that imports through
# `shared_runtime` (langgraph + claude-code via a2a_executor, autogen via
# adapters.shared_runtime) silently used that un-auth'd path. The canonical,
# authenticated implementation lives in `executor_helpers` (it routes through
# the shared `get_http_client()` and attaches `platform_auth.auth_headers()`).
# Re-export it here so there is exactly one implementation and every caller
# gets the authenticated heartbeat push. The name and signature are unchanged,
# so existing `from ...shared_runtime import set_current_task` callers keep
# working.
from molecule_runtime.executor_helpers import set_current_task  # noqa: F401


def _extract_part_text(part) -> str:
    """Extract text from a message part, handling dicts and A2A objects."""
    if isinstance(part, dict):
        text = part.get("text", "")
        if text:
            return text
        root = part.get("root")
        if isinstance(root, dict):
            return root.get("text", "")
        return ""
    if hasattr(part, "text") and part.text:
        return part.text
    if hasattr(part, "root") and hasattr(part.root, "text") and part.root.text:
        return part.root.text
    return ""


def extract_message_text(context_or_parts) -> str:
    """Extract text plus a local-path attachment manifest from an A2A message.

    SSOT (runtime #2914): this is the single canonical implementation. It used
    to be duplicated in `executor_helpers` (text-only, dropped attachments);
    that copy now delegates here. Accepts any of three shapes so every former
    caller of either copy keeps working:

      * a ``RequestContext`` (``.message.parts``),
      * a bare A2A ``Message`` (``.parts``), or
      * a raw list/iterable of parts.

    Returns the joined text and, when the message carries file attachments,
    appends an ``Attached files:`` manifest of resolved local paths (built via
    ``executor_helpers.extract_attached_files``).
    """
    # Resolve (message, parts) across the three accepted input shapes.
    message = getattr(context_or_parts, "message", None)
    if message is not None and getattr(message, "parts", None) is not None:
        # RequestContext: parts live under .message.parts
        parts = message.parts
    elif getattr(context_or_parts, "parts", None) is not None:
        # Bare A2A Message: parts live directly on the object
        message = context_or_parts
        parts = context_or_parts.parts
    else:
        # Raw parts list (or an object with neither attr)
        message = context_or_parts
        parts = context_or_parts
    if isinstance(parts, (str, bytes, dict)):
        # Strings/bytes/dicts are never a parts *sequence* — fall back to empty.
        parts = []
    elif not isinstance(parts, (list, tuple)):
        # a2a-sdk >=1.0 migrated to protobuf, so message.parts is a
        # RepeatedCompositeContainer (iterable but NOT list/tuple). Per this
        # function's own docstring it accepts "a raw list/iterable of parts";
        # materialise any iterable. Only genuine non-iterables (e.g. an empty
        # SimpleNamespace) fall back to empty.
        try:
            parts = list(parts)
        except TypeError:
            parts = []
    text = " ".join(
        text for part in (parts or []) if (text := _extract_part_text(part))
    ).strip()
    try:
        from molecule_runtime.executor_helpers import extract_attached_files

        attached = extract_attached_files(message)
    except Exception:
        attached = []
    if not attached:
        return text
    manifest = "\n".join(
        f"- {f['name']} ({f['mime_type'] or 'unknown type'}) at {f['path']}"
        for f in attached
    )
    attachment_text = f"Attached files:\n{manifest}"
    return f"{text}\n\n{attachment_text}" if text else attachment_text


def extract_history(context: RequestContext) -> list[tuple[str, str]]:
    """Extract conversation history from A2A request metadata."""
    messages: list[tuple[str, str]] = []
    request = getattr(context, "request", None)
    metadata = getattr(request, "metadata", None) if request else None
    if not isinstance(metadata, dict):
        metadata = getattr(context, "metadata", None) or {}
    history = metadata.get("history", []) if isinstance(metadata, dict) else []
    if not isinstance(history, list):
        return messages

    for entry in history:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role", "user")
        parts = entry.get("parts", [])
        text = " ".join(
            text for part in (parts or []) if (text := _extract_part_text(part))
        ).strip()
        if text:
            mapped_role = "human" if role == "user" else "ai"
            messages.append((mapped_role, text))
    return messages


def format_conversation_history(history: list[tuple[str, str]]) -> str:
    """Render `(role, text)` history into a stable human-readable transcript."""
    return "\n".join(
        f"{'User' if role == 'human' else 'Agent'}: {text}" for role, text in history
    )


def build_task_text(user_message: str, history: list[tuple[str, str]]) -> str:
    """Build a single task/request string with optional prepended conversation history."""
    if not history:
        return user_message
    transcript = format_conversation_history(history)
    return f"Conversation so far:\n{transcript}\n\nCurrent request: {user_message}"


def append_peer_guidance(
    base_text: str | None,
    peers_info: str,
    *,
    default_text: str,
    tool_name: str,
) -> str:
    """Append peer guidance text when peers are available."""
    text = (base_text or default_text).strip()
    if peers_info:
        text += f"\n\n## Peers\n{peers_info}\nUse {tool_name} to communicate with them."
    return text


def summarize_peer_cards(peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return compact peer metadata for prompt rendering.

    Falls back to the registry row's `name` and `role` when `agent_card` is
    null or unparseable so peers stay visible to delegators even before
    their A2A discovery roundtrip has populated a card. Without this
    fallback a coordinator-tier workspace with N freshly-created worker
    peers would render an empty `## Your Peers` section and refuse to
    delegate (the regression behind the 2026-04-27 Design Director
    discovery bug).
    """
    summaries: list[dict[str, Any]] = []
    for peer in peers:
        agent_card = peer.get("agent_card")
        if isinstance(agent_card, str):
            try:
                agent_card = json.loads(agent_card)
            except Exception:
                agent_card = None
        if not isinstance(agent_card, dict):
            agent_card = None

        if agent_card:
            skills_raw = agent_card.get("skills") or []
            skills = [
                s.get("name", s.get("id", ""))
                for s in skills_raw
                if isinstance(s, dict)
            ]
            name = agent_card.get("name") or peer.get("name") or "Unknown"
        else:
            skills = []
            name = peer.get("name") or "Unknown"

        summaries.append(
            {
                "id": peer.get("id", "unknown"),
                "name": name,
                "role": peer.get("role") or "",
                "status": peer.get("status", "unknown"),
                "skills": skills,
            }
        )
    return summaries


def build_peer_section(
    peers: list[dict[str, Any]],
    *,
    heading: str = "## Your Peers (workspaces you can delegate to)",
    instruction: str = (
        "Use the `delegate_task_async` tool to send tasks to peers. "
        "Only delegate to peers listed above."
    ),
) -> str:
    """Render a stable peer section for system prompts."""
    summaries = summarize_peer_cards(peers)
    if not summaries:
        return ""

    parts = [heading, ""]
    for peer in summaries:
        parts.append(f"- **{peer['name']}** (id: `{peer['id']}`, status: {peer['status']})")
        if peer["skills"]:
            parts.append(f"  Skills: {', '.join(peer['skills'])}")
        elif peer.get("role"):
            parts.append(f"  Role: {peer['role']}")
        parts.append("")
    parts.append(instruction)
    return "\n".join(parts)


def brief_task(text: str, limit: int = 60) -> str:
    """Create a short human-readable task label for the heartbeat banner."""
    return text[:limit] + ("..." if len(text) > limit else "")


# `set_current_task` is re-exported from `executor_helpers` at the top of this
# module (see the SSOT note there). The previous local, unauthenticated
# implementation was removed in runtime #2914.
