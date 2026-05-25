import asyncio
from subprocess import CompletedProcess

from molecule_runtime import a2a_tools_desktop as desktop


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


def test_desktop_open_url_spawns_browser_without_shell(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(desktop, "_host_ok", lambda binary: binary == "google-chrome")
    monkeypatch.setattr(desktop, "_host_spawn", lambda args: calls.append(args))
    monkeypatch.setattr(desktop, "Path", lambda value: tmp_path / value.lstrip("/"))

    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com/?q=$(id)"))

    assert '"ok": true' in out
    assert calls == [[
        "runuser",
        "-u",
        "ubuntu",
        "--",
        "/usr/bin/env",
        "DISPLAY=:99",
        "google-chrome",
        "--disable-dev-shm-usage",
        f"--user-data-dir={tmp_path / 'workspace/.browser-profile'}",
        "https://example.com/?q=$(id)",
    ]]
    assert "/bin/sh" not in calls[0]
