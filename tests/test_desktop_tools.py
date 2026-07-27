import asyncio

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


def _mock_input(monkeypatch):
    """Capture the action dicts open_url/click/type/key POST to the gateway."""
    actions: list[dict] = []

    async def fake_input(action):
        actions.append(action)

    monkeypatch.setattr(desktop, "_desktop_input", fake_input)
    return actions


def _mock_screenshot_bytes(monkeypatch, png: bytes | Exception):
    async def fake_bytes():
        if isinstance(png, Exception):
            raise png
        return png

    monkeypatch.setattr(desktop, "_desktop_screenshot_bytes", fake_bytes)


# ── status: now a gateway liveness probe (no host binaries) ──────────────────
def test_desktop_status_available_when_gateway_reachable(monkeypatch):
    monkeypatch.setenv("WORKSPACE_ID", "ws-1")
    _mock_screenshot_bytes(monkeypatch, _png_bytes(1280, 800))

    out = asyncio.run(desktop.tool_desktop_status())

    assert '"mode": "sidecar-gateway"' in out
    assert '"available": true' in out


def test_desktop_status_unavailable_when_gateway_errors(monkeypatch):
    monkeypatch.setenv("WORKSPACE_ID", "ws-1")
    _mock_screenshot_bytes(monkeypatch, RuntimeError("desktop down"))

    out = asyncio.run(desktop.tool_desktop_status())

    assert '"available": false' in out
    assert "desktop down" in out


# ── screenshot path validation (unchanged) ──────────────────────────────────
def test_desktop_screenshot_rejects_parent_paths():
    out = asyncio.run(desktop.tool_desktop_screenshot("../secret"))
    assert '"ok": false' in out
    assert "screenshots directory" in out or "must be a filename" in out


def test_desktop_screenshot_rejects_nested_paths():
    out = asyncio.run(desktop.tool_desktop_screenshot("nested/shot.png"))
    assert '"ok": false' in out
    assert "must be a filename" in out


# ── click / type / key: gateway input actions ───────────────────────────────
def test_desktop_click_posts_gateway_action(monkeypatch):
    actions = _mock_input(monkeypatch)
    out = asyncio.run(desktop.tool_desktop_click(10, 20))
    assert '"ok": true' in out
    assert actions == [{"type": "click", "x": 10, "y": 20, "button": "left"}]


def test_desktop_type_posts_gateway_action(monkeypatch):
    actions = _mock_input(monkeypatch)
    out = asyncio.run(desktop.tool_desktop_type("hello"))
    assert '"ok": true' in out
    assert actions == [{"type": "type", "text": "hello"}]


# ── open_url: now a navigate action through the gateway ──────────────────────
def test_desktop_open_url_posts_navigate_action(monkeypatch):
    actions = _mock_input(monkeypatch)
    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com/x"))
    assert '"ok": true' in out
    assert '"url": "https://example.com/x"' in out
    assert actions == [{"type": "navigate", "url": "https://example.com/x"}]


def test_desktop_open_url_requires_http_scheme(monkeypatch):
    actions = _mock_input(monkeypatch)
    for bad in ["file:///etc/passwd", "javascript:alert(1)", "ftp://x", ""]:
        out = asyncio.run(desktop.tool_desktop_open_url(bad))
        assert '"ok": false' in out
    assert actions == [], "rejected URLs must not reach the gateway"


def test_desktop_open_url_pauses_when_human_holds_control(monkeypatch):
    async def fake_input(action):
        raise RuntimeError("a human currently holds desktop control; pause and retry when released")

    monkeypatch.setattr(desktop, "_desktop_input", fake_input)
    out = asyncio.run(desktop.tool_desktop_open_url("https://example.com"))
    assert '"ok": false' in out
    assert "human currently holds desktop control" in out


# ── png dimension parsing (unchanged) ───────────────────────────────────────
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


# ── screenshot vision-safe reporting (via the gateway bytes) ─────────────────
def _screenshot_with_dims(monkeypatch, tmp_path, width, height):
    """Run tool_desktop_screenshot with the gateway returning a width x height PNG."""
    monkeypatch.setattr(desktop, "SCREENSHOT_DIR", tmp_path)
    _mock_screenshot_bytes(monkeypatch, _png_bytes(width, height))
    return asyncio.run(desktop.tool_desktop_screenshot("shot.png"))


def test_desktop_screenshot_reports_dimensions_and_vision_safe(monkeypatch, tmp_path):
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1280, 800)
    assert '"ok": true' in out
    assert '"width": 1280' in out
    assert '"height": 800' in out
    assert '"vision_safe": true' in out
    assert "warning" not in out


def test_desktop_screenshot_warns_when_display_exceeds_vision_bound(monkeypatch, tmp_path):
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1920, 1080)
    assert '"vision_safe": false' in out
    assert "warning" in out
    assert "1280x800" in out


def test_desktop_screenshot_vision_safe_boundary_edge_clause(monkeypatch, tmp_path):
    # 1600x700 = 1.12 MP (pixel clause OK) but 1600 > 1568 (edge clause fails).
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1600, 700)
    assert '"vision_safe": false' in out
    assert "warning" in out


def test_desktop_screenshot_vision_safe_boundary_pixel_clause(monkeypatch, tmp_path):
    # 1400x900 = 1.26 MP (pixel clause fails) but 1400 < 1568 (edge clause OK).
    out = _screenshot_with_dims(monkeypatch, tmp_path, 1400, 900)
    assert '"vision_safe": false' in out
    assert "warning" in out
