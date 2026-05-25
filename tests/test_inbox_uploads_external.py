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


def test_rewrite_request_body_rewrites_cached_legacy_content_uri():
    from molecule_runtime import inbox_uploads

    cache = inbox_uploads.get_cache()
    cache.clear()
    legacy_uri = (
        "/workspaces/091a9180-b303-4a20-aefe-3a4a675b8aa4"
        "/content/44444444-4444-4444-4444-444444444444/content"
    )
    local_uri = "file:///tmp/molecule-inbox/pasted.png"
    cache.set(legacy_uri, local_uri)
    body = {
        "params": {
            "message": {
                "parts": [
                    {
                        "kind": "file",
                        "file": {
                            "uri": legacy_uri,
                            "name": "pasted.png",
                            "mime_type": "image/png",
                        },
                    }
                ]
            }
        }
    }

    inbox_uploads.rewrite_request_body(body)

    assert body["params"]["message"]["parts"][0]["file"]["uri"] == local_uri


def test_rewrite_flat_manifest_rewrites_cached_legacy_content_uri():
    from molecule_runtime import inbox_uploads

    cache = inbox_uploads.get_cache()
    cache.clear()
    legacy_uri = (
        "/workspaces/091a9180-b303-4a20-aefe-3a4a675b8aa4"
        "/content/55555555-5555-5555-5555-555555555555/content"
    )
    local_uri = "file:///tmp/molecule-inbox/flat.png"
    cache.set(legacy_uri, local_uri)
    body = {
        "uri": legacy_uri,
        "name": "flat.png",
        "mimeType": "image/png",
    }

    inbox_uploads.rewrite_request_body(body)

    assert body["uri"] == local_uri
