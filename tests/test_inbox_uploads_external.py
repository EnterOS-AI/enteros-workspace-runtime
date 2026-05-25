from pathlib import Path


def test_stage_to_disk_external_dir_returns_file_uri(monkeypatch, tmp_path):
    from molecule_runtime import inbox_uploads

    upload_dir = tmp_path / "channel-inbox"
    monkeypatch.setattr(inbox_uploads, "CHAT_UPLOAD_DIR", str(upload_dir))

    uri = inbox_uploads.stage_to_disk(b"png-bytes", "pasted.png")

    assert uri.startswith("file://")
    assert uri.endswith("-pasted.png")
    path = Path(uri.removeprefix("file://"))
    assert path.read_bytes() == b"png-bytes"
