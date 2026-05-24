"""Image attachment description helpers for text-only runtimes."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Iterable, Mapping

import httpx

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_VLM_URL = "https://api.minimax.io/v1/coding_plan/vlm"


async def describe_image_attachments(files: Iterable[Mapping[str, object]]) -> str:
    """Return text descriptions for image attachments using MiniMax VLM.

    MiniMax M2.7 is the default smoke/provider model for several adapters, but
    its chat-completions surface is text-only. The token plan exposes a
    separate VLM endpoint; use it to turn uploaded images into ordinary text
    context before handing the turn to text-only agents.
    """
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        return ""

    descriptions: list[str] = []
    async with httpx.AsyncClient(timeout=45.0) as client:
        for file in files:
            mime_type = str(file.get("mime_type") or "")
            path = str(file.get("path") or "")
            if not mime_type.startswith("image/") or not path:
                continue
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                logger.warning("attachment vision: cannot read %s: %s", path, exc)
                continue
            if len(data) > _MAX_IMAGE_BYTES:
                logger.warning("attachment vision: skipping %s over 5 MB", path)
                continue

            data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
            try:
                resp = await client.post(
                    _VLM_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "prompt": (
                            "Describe this image in one concise sentence. "
                            "Mention visible colors, shapes, text, and layout."
                        ),
                        "image_url": data_url,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                logger.warning("attachment vision: VLM request failed for %s: %s", path, exc)
                continue

            content = str(body.get("content") or "").strip()
            if content:
                name = str(file.get("name") or Path(path).name)
                descriptions.append(f"- {name}: {content}")

    if not descriptions:
        return ""
    return "\n\nImage attachment descriptions:\n" + "\n".join(descriptions)


async def append_image_descriptions(text: str, files: Iterable[Mapping[str, object]]) -> str:
    """Append VLM-generated image descriptions to a user prompt."""
    descriptions = await describe_image_attachments(files)
    if not descriptions:
        return text
    return (text.rstrip() + descriptions).strip()
