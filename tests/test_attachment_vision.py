from pathlib import Path

import pytest

from molecule_runtime.attachment_vision import append_image_descriptions


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"content": "A red square and a blue circle are visible."}


class _Client:
    def __init__(self, *args, **kwargs):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None):
        self.requests.append((url, headers, json))
        assert headers["Authorization"] == "Bearer token"
        assert json["image_url"].startswith("data:image/png;base64,")
        return _Resp()


@pytest.mark.asyncio
async def test_append_image_descriptions_uses_minimax_vlm(monkeypatch, tmp_path):
    img = tmp_path / "probe.png"
    img.write_bytes(b"png")
    monkeypatch.setenv("MINIMAX_API_KEY", "token")
    monkeypatch.setattr("molecule_runtime.attachment_vision.httpx.AsyncClient", _Client)

    got = await append_image_descriptions(
        "Describe this.",
        [{"name": "probe.png", "mime_type": "image/png", "path": str(img)}],
    )

    assert "Describe this." in got
    assert "Image attachment descriptions:" in got
    assert "probe.png: A red square and a blue circle are visible." in got


@pytest.mark.asyncio
async def test_append_image_descriptions_no_key_is_noop(monkeypatch, tmp_path):
    img = tmp_path / "probe.png"
    img.write_bytes(b"png")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    got = await append_image_descriptions(
        "Describe this.",
        [{"name": "probe.png", "mime_type": "image/png", "path": str(img)}],
    )

    assert got == "Describe this."
