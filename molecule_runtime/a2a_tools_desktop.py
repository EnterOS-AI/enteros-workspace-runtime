"""Desktop-control MCP tools for display-enabled workspaces.

The display session runs on the EC2 host, not inside the runtime
container. T4 workspaces mount the host at /host and grant sudo inside
the privileged container, so these tools enter the host namespace with
``chroot /host`` and drive the private Xvfb display on ``:99``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

DISPLAY = os.environ.get("MOLECULE_DISPLAY", ":99")
SCREENSHOT_DIR = Path("/workspace/.molecule/display/screenshots")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _host_cmd(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    cmd = [
        "sudo",
        "chroot",
        "/host",
        "/usr/bin/env",
        f"DISPLAY={DISPLAY}",
        *args,
    ]
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _host_spawn(args: list[str]) -> subprocess.Popen[str]:
    cmd = [
        "sudo",
        "chroot",
        "/host",
        "/usr/bin/env",
        f"DISPLAY={DISPLAY}",
        *args,
    ]
    return subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _host_ok(binary: str) -> bool:
    result = _host_cmd(["/bin/sh", "-lc", f"command -v {binary}"], timeout=5)
    return result.returncode == 0


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


def _copy_screenshot_from_host_tmp(host_tmp_path: str, out_path: Path) -> None:
    _ensure_safe_screenshot_dir()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(out_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as dst:
            with open(Path("/host") / host_tmp_path.lstrip("/"), "rb") as src:
                shutil.copyfileobj(src, dst)
    except Exception:
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        raise


async def tool_desktop_status() -> str:
    """Return whether host desktop-control dependencies are available."""
    checks = {
        "display": DISPLAY,
        "host_mount": Path("/host").is_dir(),
        "xdotool": _host_ok("xdotool"),
        "scrot": _host_ok("scrot"),
        "google_chrome": _host_ok("google-chrome"),
        "chromium": _host_ok("chromium-browser") or _host_ok("chromium"),
    }
    checks["available"] = bool(checks["host_mount"] and checks["xdotool"] and checks["scrot"])
    return _json(checks)


async def tool_desktop_screenshot(path: str = "") -> str:
    """Capture a PNG screenshot of the workspace desktop."""
    try:
        out_path = _clean_screenshot_path(path)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})
    host_path = f"/tmp/molecule-desktop-shot-{uuid.uuid4().hex}.png"
    result = _host_cmd(["scrot", host_path], timeout=15)
    if result.returncode != 0:
        return _json({"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
    try:
        _copy_screenshot_from_host_tmp(host_path, out_path)
    except FileExistsError:
        return _json({"ok": False, "error": "screenshot path already exists"})
    except OSError as exc:
        return _json({"ok": False, "error": str(exc)})
    finally:
        _host_cmd(["rm", "-f", host_path], timeout=5)
    return _json({"ok": True, "path": str(out_path)})


async def tool_desktop_click(x: int, y: int, button: int = 1) -> str:
    """Move the mouse and click at absolute desktop coordinates."""
    if button not in (1, 2, 3):
        return _json({"ok": False, "error": "button must be 1, 2, or 3"})
    result = _host_cmd(["xdotool", "mousemove", str(int(x)), str(int(y)), "click", str(button)])
    if result.returncode != 0:
        return _json({"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
    return _json({"ok": True, "x": int(x), "y": int(y), "button": button})


async def tool_desktop_type(text: str, delay_ms: int = 20) -> str:
    """Type text into the focused desktop application."""
    delay = max(0, min(int(delay_ms), 1000))
    result = _host_cmd(["xdotool", "type", "--clearmodifiers", "--delay", str(delay), text], timeout=30)
    if result.returncode != 0:
        return _json({"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
    return _json({"ok": True, "chars": len(text)})


async def tool_desktop_key(keys: str) -> str:
    """Press a key chord such as Return, ctrl+l, alt+Tab, or Escape."""
    keys = (keys or "").strip()
    if not keys:
        return _json({"ok": False, "error": "keys is required"})
    result = _host_cmd(["xdotool", "key", "--clearmodifiers", keys])
    if result.returncode != 0:
        return _json({"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
    return _json({"ok": True, "keys": keys})


async def tool_desktop_open_url(url: str) -> str:
    """Open a URL in the host desktop browser."""
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return _json({"ok": False, "error": "url must start with http:// or https://"})
    browser = "google-chrome" if _host_ok("google-chrome") else ""
    if not browser:
        browser = "chromium-browser" if _host_ok("chromium-browser") else ""
    if not browser and _host_ok("chromium"):
        browser = "chromium"
    if not browser:
        return _json({"ok": False, "error": "no supported desktop browser is installed"})
    profile = Path("/workspace/.browser-profile")
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        _host_spawn([
            "runuser",
            "-u",
            "ubuntu",
            "--",
            "/usr/bin/env",
            f"DISPLAY={DISPLAY}",
            browser,
            "--disable-dev-shm-usage",
            f"--user-data-dir={profile}",
            url,
        ])
    except OSError as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "browser": browser, "url": url})
