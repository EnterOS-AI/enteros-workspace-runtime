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
        "900",
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
