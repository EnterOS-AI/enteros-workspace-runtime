"""Desktop-control tools for display-enabled workspaces.

The agent-facing action tools (screenshot / click / type / key) call the
platform's authenticated desktop **gateway route**
(``/workspaces/<id>/desktop/{screenshot,input}``) — design decision B. The
platform gateway owns control-lock arbitration, scale-from-zero, and
per-sidecar auth; the desktop itself runs in the ``wsdesk-<id>`` sidecar
container (design RFC: molecule-core
docs/superpowers/specs/2026-07-27-agent-desktop-sidecar-design.md §9). Keeping
this tool surface in the runtime (rather than a platform MCP tool) is what makes
it packageable as a native ``kind:mcp`` plugin later.

All tools — screenshot, click, type, key, open_url (navigate), status — now go
through the gateway; the old host-co-located model (``chroot /host`` + Xvfb) and
its ``_host_*`` helpers have been retired.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import httpx

SCREENSHOT_DIR = Path("/workspace/.molecule/display/screenshots")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# core#2200: the desktop is captured at native pixels (scrot, no resize) and
# clicked at native pixels (xdotool), so screenshot(x,y) == click(x,y) ONLY as
# long as the model sees the screenshot at those same pixels. Claude's vision
# silently DOWNSCALES any image above ~1.15 MP / 1568px on the long edge before
# the model reasons over it — so a 1920x1080 (2.07 MP) display desyncs the two
# coordinate spaces and clicks miss. The provisioner pins :99 to 1280x800
# (WXGA, Anthropic's recommended computer-use resolution: 1.02 MP, 1280<1568 ->
# no downscale -> 1:1). These bounds let the screenshot tool surface the pixel
# space and warn loudly if a larger display ever slips through.
_VISION_SAFE_PIXELS = 1_150_000
_VISION_SAFE_EDGE = 1568

# Design decision B: the agent-facing desktop tools call the platform's
# authenticated desktop gateway route (/workspaces/<id>/desktop/{screenshot,
# input}) rather than driving the host display via `chroot /host`. The platform
# gateway owns control-lock arbitration, scale-from-zero, and per-sidecar auth;
# the desktop itself runs in the wsdesk-<id> sidecar container. Keeping the tool
# surface HERE (not a platform MCP tool) is what makes it plugin-extractable —
# the static platform mcp.go bridge can never host a plugin-contributed tool.
_DESKTOP_TIMEOUT = 30.0


async def _desktop_screenshot_bytes() -> bytes:
    from .a2a_client import PLATFORM_URL, WORKSPACE_ID, auth_headers

    url = f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/desktop/screenshot"
    async with httpx.AsyncClient(timeout=_DESKTOP_TIMEOUT) as client:
        resp = await client.get(
            url, headers={"X-Workspace-ID": WORKSPACE_ID, **auth_headers(WORKSPACE_ID)}
        )
    resp.raise_for_status()
    return resp.content


async def _desktop_input(action: dict) -> None:
    from .a2a_client import PLATFORM_URL, WORKSPACE_ID, auth_headers

    url = f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/desktop/input"
    async with httpx.AsyncClient(timeout=_DESKTOP_TIMEOUT) as client:
        resp = await client.post(
            url,
            json=action,
            headers={"X-Workspace-ID": WORKSPACE_ID, **auth_headers(WORKSPACE_ID)},
        )
    # 409 = a human holds the control lock; the agent must pause (§8), not fight
    # for the cursor.
    if resp.status_code == 409:
        raise RuntimeError(
            "a human currently holds desktop control; pause and retry when released"
        )
    resp.raise_for_status()


def _json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def _ensure_safe_screenshot_dir() -> None:
    current = Path("/")
    for part in SCREENSHOT_DIR.parts[1:]:
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValueError(f"{current} must be a real directory")
            continue
        current.mkdir(mode=0o700 if current == SCREENSHOT_DIR else 0o755)


def _clean_screenshot_path(value: str | None) -> Path:
    if not value:
        value = f"screenshot-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
    value = value.strip()
    if "/" in value or "\\" in value:
        raise ValueError("path must be a filename under the screenshots directory")
    value = _SAFE_FILENAME_RE.sub("_", value)
    path = Path(value)
    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")
    if ".." in path.parts:
        raise ValueError("path must stay under the screenshots directory")
    return SCREENSHOT_DIR / path


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    """Read (width, height) from a PNG's IHDR chunk without an image library."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


async def tool_desktop_status() -> str:
    """Report desktop availability via the sidecar gateway (design decision B).

    The desktop no longer lives on the host (no ``chroot /host`` / host binaries);
    it runs in the wsdesk-<id> sidecar reached through the authenticated gateway.
    A screenshot round-trip is the liveness probe — it exercises auth + the
    control server end-to-end, and returns the scale-from-zero result too.
    """
    from .a2a_client import WORKSPACE_ID

    out: dict = {"mode": "sidecar-gateway", "workspace_id": WORKSPACE_ID}
    try:
        await _desktop_screenshot_bytes()
        out["available"] = True
    except Exception as exc:
        out["available"] = False
        out["error"] = str(exc)
    return _json(out)


async def tool_desktop_screenshot(path: str = "") -> str:
    """Capture a PNG screenshot of the workspace desktop."""
    try:
        out_path = _clean_screenshot_path(path)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})
    try:
        png = await _desktop_screenshot_bytes()
    except Exception as exc:
        return _json({"ok": False, "error": f"screenshot failed: {exc}"})
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(png)
    except OSError as exc:
        return _json({"ok": False, "error": str(exc)})
    payload: dict = {"ok": True, "path": str(out_path)}
    # Surface the exact pixel space so the agent never has to infer DPI/scale
    # (core#2200): the coordinates it reads off this screenshot are the same
    # coordinates tool_desktop_click expects, as long as the image stays within
    # the vision-safe bounds (i.e. Claude does not downscale it).
    dims = _png_dimensions(out_path)
    if dims is not None:
        width, height = dims
        vision_safe = width * height <= _VISION_SAFE_PIXELS and max(width, height) <= _VISION_SAFE_EDGE
        payload["width"] = width
        payload["height"] = height
        payload["vision_safe"] = vision_safe
        if not vision_safe:
            payload["warning"] = (
                f"screenshot is {width}x{height} which exceeds the vision-safe bound "
                f"({_VISION_SAFE_EDGE}px long edge / {_VISION_SAFE_PIXELS // 1000}kpx); the "
                "model sees a DOWNSCALED copy, so click coordinates read off it will be "
                "misaligned. Reduce the desktop to <=1280x800 via MOLECULE_DISPLAY_WIDTH/"
                "MOLECULE_DISPLAY_HEIGHT."
            )
    return _json(payload)


async def tool_desktop_click(x: int, y: int, button: int = 1) -> str:
    """Move the mouse and click at absolute desktop coordinates."""
    if button not in (1, 2, 3):
        return _json({"ok": False, "error": "button must be 1, 2, or 3"})
    btn = {1: "left", 2: "middle", 3: "right"}[button]
    try:
        await _desktop_input({"type": "click", "x": int(x), "y": int(y), "button": btn})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "x": int(x), "y": int(y), "button": button})


async def tool_desktop_type(text: str, delay_ms: int = 20) -> str:
    """Type text into the focused desktop application."""
    _ = delay_ms  # kept for signature back-compat; the control server owns timing
    try:
        await _desktop_input({"type": "type", "text": text})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "chars": len(text)})


async def tool_desktop_key(keys: str) -> str:
    """Press a key chord such as Return, ctrl+l, alt+Tab, or Escape."""
    keys = (keys or "").strip()
    if not keys:
        return _json({"ok": False, "error": "keys is required"})
    try:
        await _desktop_input({"type": "key", "keys": keys})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "keys": keys})


async def tool_desktop_open_url(url: str) -> str:
    """Navigate the workspace desktop browser to a URL.

    Re-pointed to the sidecar (design decision B): sends a ``navigate`` action
    through the authenticated, lock-gated desktop gateway (``/desktop/input``),
    which the sidecar control server turns into a Chromium single-instance
    hand-off that navigates the pinned kiosk window. No host browser is spawned;
    the fixed-resolution kiosk stays the one window (§3). A human holding control
    yields a 409 (the agent pauses), same as every other input.
    """
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return _json({"ok": False, "error": "url must start with http:// or https://"})
    try:
        await _desktop_input({"type": "navigate", "url": url})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "url": url})




async def _desktop_control_status() -> dict:
    """GET /desktop/control — {human_in_control, agent_can_control}."""
    from .a2a_client import PLATFORM_URL, WORKSPACE_ID, auth_headers

    url = f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/desktop/control"
    async with httpx.AsyncClient(timeout=_DESKTOP_TIMEOUT) as client:
        resp = await client.get(
            url, headers={"X-Workspace-ID": WORKSPACE_ID, **auth_headers(WORKSPACE_ID)}
        )
    resp.raise_for_status()
    return resp.json()


async def tool_desktop_wait_for_control(timeout_s: int = 60) -> str:
    """Block until the agent can control the desktop (a human has released control).

    When a click/type/key/navigate returns "a human currently holds desktop
    control", pause and call this: it polls the gateway's /desktop/control status
    until human_in_control clears or the timeout elapses, then the agent can
    resume driving (design decision B, §8 arbitration — the human always wins the
    one cursor, and the agent waits its turn).
    """
    timeout_s = max(1, min(int(timeout_s), 600))
    poll = 2.0
    waited = 0.0
    while True:
        try:
            if not (await _desktop_control_status()).get("human_in_control", False):
                return _json({"ok": True, "control": "available", "waited_s": round(waited, 1)})
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})
        if waited >= timeout_s:
            return _json(
                {"ok": False, "control": "human", "error": f"still human-controlled after {timeout_s}s"}
            )
        await asyncio.sleep(poll)
        waited += poll
