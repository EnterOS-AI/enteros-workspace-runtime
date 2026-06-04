import asyncio
from pathlib import Path
from subprocess import CompletedProcess

from molecule_runtime import a2a_tools_desktop as desktop


def _png_bytes(width: int, height: int) -> bytes:
    """A PNG signature + IHDR header carrying the given dimensions.

    _png_dimensions only reads the first 24 bytes (sig + IHDR length/type +
    width + height), so a header is enough to exercise it without an image lib.
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + int(width).to_bytes(4, "big")
        + int(height).to_bytes(4, "big")
    )


def test_desktop_status_reports_available_when_host_tools_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(desktop, "_host_ok", lambda binary: binary in {"xdotool", "scrot"})

    out = asyncio.run(desktop.tool_desktop_status())

    assert '"available": true' in out
    assert '"xdotool": true' in out
    assert '"scrot": true' in out


def test_desktop_screenshot_rejects_parent_paths():
    out = asyncio.run(desktop.tool_desktop_screenshot("../secret"))

    assert '"ok": false' in out
    assert "screenshots directory" in out or "must be a filename" in out


def test_desktop_screenshot_rejects_nested_paths():
    out = asyncio.run(desktop.tool_desktop_screenshot("nested/shot.png"))

    assert '"ok": false' in out
    assert "must be a filename" in out


def test_desktop_click_invokes_xdotool(monkeypatch):
    calls = []

    def fake_host_cmd(args, timeout=10):
        calls.append(args)
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(desktop, "_host_cmd", fake_host_cmd)

    out = asyncio.run(desktop.tool_desktop_click(10, 20))

    assert '"ok": true' in out
    assert calls == [["xdotool", "mousemove", "10", "20", "click", "1"]]


def test_desktop_open_url_requires_browser(monkeypatch):
    monkeypatch.setattr(desktop, "_host_ok", lambda _binary: False)

    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com"))

    assert '"ok": false' in out
    assert "no supported desktop browser" in out


def test_desktop_open_url_prefers_firefox_when_available(monkeypatch, tmp_path):
    spawn_calls = []
    host_calls = []

    monkeypatch.setattr(desktop, "_host_ok", lambda binary: binary in {"firefox", "falkon", "google-chrome"})
    monkeypatch.setattr(desktop, "_host_spawn", lambda args: spawn_calls.append(args))
    monkeypatch.setattr(desktop, "_host_cmd", lambda args, timeout=10: host_calls.append((args, timeout)))
    monkeypatch.setattr(desktop, "Path", lambda value: tmp_path / value.lstrip("/"))

    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com/?q=$(id)"))

    assert '"ok": true' in out
    assert '"browser": "firefox"' in out
    assert spawn_calls == [[
        "runuser",
        "-u",
        "ubuntu",
        "--",
        "/usr/bin/env",
        "DISPLAY=:99",
        "XDG_RUNTIME_DIR=/tmp/runtime-ubuntu",
        "MOZ_DISABLE_RDD_SANDBOX=1",
        "MOZ_DISABLE_CONTENT_SANDBOX=1",
        "firefox",
        "--no-remote",
        "--new-window",
        "https://example.com/?q=$(id)",
    ]]
    assert host_calls == [([
        "xdotool",
        "search",
        "--sync",
        "--onlyvisible",
        "--class",
        "firefox",
        "windowmove",
        "%@",
        "10",
        "37",
        "windowsize",
        "%@",
        "1280",
        "800",
        "windowactivate",
        "%@",
        "key",
        "F11",
    ], 20)]
    assert "/bin/sh" not in spawn_calls[0]


def test_desktop_open_url_falls_back_to_falkon(monkeypatch, tmp_path):
    spawn_calls = []
    host_calls = []

    monkeypatch.setattr(desktop, "_host_ok", lambda binary: binary in {"falkon", "google-chrome"})
    monkeypatch.setattr(desktop, "_host_spawn", lambda args: spawn_calls.append(args))
    monkeypatch.setattr(desktop, "_host_cmd", lambda args, timeout=10: host_calls.append((args, timeout)))
    monkeypatch.setattr(desktop, "Path", lambda value: tmp_path / value.lstrip("/"))

    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com/?q=$(id)"))

    assert '"ok": true' in out
    assert '"browser": "falkon"' in out
    assert "falkon" in spawn_calls[0]
    assert host_calls[0][0][5] == "falkon"


def test_desktop_open_url_spawns_chrome_with_xvfb_flags(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(desktop, "_host_ok", lambda binary: binary == "google-chrome")
    monkeypatch.setattr(desktop, "_host_spawn", lambda args: calls.append(args))
    monkeypatch.setattr(desktop, "Path", lambda value: tmp_path / value.lstrip("/"))

    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com/?q=$(id)"))

    assert '"ok": true' in out
    assert '"browser": "google-chrome"' in out
    assert calls == [[
        "runuser",
        "-u",
        "ubuntu",
        "--",
        "/usr/bin/env",
        "DISPLAY=:99",
        "google-chrome",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={tmp_path / 'workspace/.browser-profile'}",
        "https://example.com/?q=$(id)",
    ]]
    assert "/bin/sh" not in calls[0]


# internal#734 Ask-3 (SSOT Option A): the browser profile path comes from the
# control-plane-published MOLECULE_BROWSER_PROFILE_DIR when present, so the path
# is defined ONCE (in the provisioner). The literal above is only the fallback.
def test_desktop_open_url_uses_browser_profile_dir_env(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("MOLECULE_BROWSER_PROFILE_DIR", "/workspace/.custom-profile")
    monkeypatch.setattr(desktop, "_host_ok", lambda binary: binary == "google-chrome")
    monkeypatch.setattr(desktop, "_host_spawn", lambda args: calls.append(args))
    monkeypatch.setattr(desktop, "Path", lambda value: tmp_path / value.lstrip("/"))

    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com"))

    assert '"ok": true' in out
    assert f"--user-data-dir={tmp_path / 'workspace/.custom-profile'}" in calls[0], calls
    # and the historical literal must NOT be used when the env is set.
    assert f"--user-data-dir={tmp_path / 'workspace/.browser-profile'}" not in calls[0]


def test_png_dimensions_parses_ihdr(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png_bytes(1280, 800))

    assert desktop._png_dimensions(shot) == (1280, 800)


def test_png_dimensions_rejects_non_png(tmp_path):
    not_png = tmp_path / "nope.png"
    not_png.write_bytes(b"not a real png file at all")

    assert desktop._png_dimensions(not_png) is None


def test_png_dimensions_missing_file_returns_none(tmp_path):
    assert desktop._png_dimensions(tmp_path / "absent.png") is None


def _screenshot_with_dims(monkeypatch, tmp_path, width, height):
    """Run tool_desktop_screenshot with scrot mocked to produce width x height."""
    monkeypatch.setattr(desktop, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        desktop,
        "_host_cmd",
        lambda args, timeout=10: CompletedProcess(args=args, returncode=0, stdout="", stderr=""),
    )

    def fake_copy(host_tmp_path, out_path):
        Path(out_path).write_bytes(_png_bytes(width, height))

    monkeypatch.setattr(desktop, "_copy_screenshot_from_host_tmp", fake_copy)
    return asyncio.run(desktop.tool_desktop_screenshot("shot.png"))


def test_desktop_screenshot_reports_dimensions_and_vision_safe(monkeypatch, tmp_path):
    # core#2200: a WXGA (1280x800) capture stays within the vision-safe bound, so
    # the model sees it 1:1 — screenshot coords == click coords. The tool must
    # surface the exact pixel space so the agent never infers a scale factor.
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1280, 800)

    assert '"ok": true' in out
    assert '"width": 1280' in out
    assert '"height": 800' in out
    assert '"vision_safe": true' in out
    assert "warning" not in out


def test_desktop_screenshot_warns_when_display_exceeds_vision_bound(monkeypatch, tmp_path):
    # 1920x1080 = 2.07 MP > the ~1.15 MP / 1568px vision bound -> Claude
    # downscales the image the model reasons over, desyncing click coords. The
    # tool must flag this loudly instead of letting clicks silently miss.
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1920, 1080)

    assert '"ok": true' in out
    assert '"width": 1920' in out
    assert '"vision_safe": false' in out
    assert "warning" in out
    assert "1280x800" in out


def test_desktop_screenshot_vision_safe_boundary_each_clause(monkeypatch, tmp_path):
    # vision_safe is an AND of two clauses (<=1.15 MP AND long edge <=1568).
    # Exercise each clause independently so a refactor that drops one is caught.
    # 1600x700 = 1.12 MP (pixel clause OK) but 1600 > 1568 (edge clause fails).
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1600, 700)
    assert '"vision_safe": false' in out, "edge clause must veto a wide-but-low-MP capture"
    assert "warning" in out


def test_desktop_screenshot_vision_safe_boundary_pixel_clause(monkeypatch, tmp_path):
    # 1400x900 = 1.26 MP (pixel clause fails) but 1400 < 1568 (edge clause OK).
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1400, 900)
    assert '"vision_safe": false' in out, "pixel clause must veto a high-MP capture"
    assert "warning" in out
