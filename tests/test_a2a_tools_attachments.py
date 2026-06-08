import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_dispatch_preserves_object_attachments(monkeypatch):
    import molecule_runtime.mcp_tools as mcp_tools

    calls = []

    async def fake_send_message_to_user(message: str, attachments=None, workspace_id=None) -> str:
        calls.append((message, attachments, workspace_id))
        return "ok"

    spec = SimpleNamespace(name="send_message_to_user", impl=fake_send_message_to_user)
    monkeypatch.setattr(mcp_tools, "_tool_permission_check", lambda _name, _args: None)
    monkeypatch.setattr(mcp_tools, "by_name", lambda _name: spec)

    result = _run(
        mcp_tools.handle_molecule_tool_call(
            "send_message_to_user",
            {
                "message": "see attached",
                "attachments": [
                    {
                        "uri": "workspace:/workspace/org_chart_v2.png",
                        "name": "org_chart_v2.png",
                        "mimeType": "image/png",
                        "size": 12345,
                    }
                ],
            },
        )
    )

    assert result == "ok"
    assert calls == [
        (
            "see attached",
            [
                {
                    "uri": "workspace:/workspace/org_chart_v2.png",
                    "name": "org_chart_v2.png",
                    "mimeType": "image/png",
                    "size": 12345,
                }
            ],
            None,
        )
    ]


def test_send_message_to_user_forwards_uploaded_attachment_refs(monkeypatch):
    import molecule_runtime.a2a_client as a2a_client
    import molecule_runtime.a2a_tools_messaging as messaging

    monkeypatch.setenv("WORKSPACE_ID", "ws-test")
    a2a_client._WORKSPACE_ID_cache = None
    monkeypatch.setattr(messaging, "_resolve_platform_url", lambda _ws: "http://platform.test")
    monkeypatch.setattr(messaging, "_auth_headers_for_heartbeat", lambda _ws: {})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = SimpleNamespace(status_code=200)

        result = _run(
            messaging.tool_send_message_to_user(
                "see attached",
                [
                    {
                        "uri": "workspace:/workspace/org_chart_v2.png",
                        "name": "org_chart_v2.png",
                        "mimeType": "image/png",
                        "size": 12345,
                    }
                ],
            )
        )

    assert result == "Message sent to user with 1 attachment(s)"
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args.kwargs["json"] == {
        "message": "see attached",
        "attachments": [
            {
                "uri": "workspace:/workspace/org_chart_v2.png",
                "name": "org_chart_v2.png",
                "mimeType": "image/png",
                "size": 12345,
            }
        ],
    }


def test_send_message_to_user_uploads_local_attachment_paths(monkeypatch, tmp_path):
    import molecule_runtime.a2a_client as a2a_client
    import molecule_runtime.a2a_tools_messaging as messaging

    monkeypatch.setenv("WORKSPACE_ID", "ws-test")
    a2a_client._WORKSPACE_ID_cache = None
    monkeypatch.setattr(messaging, "_resolve_platform_url", lambda _ws: "http://platform.test")
    monkeypatch.setattr(messaging, "_auth_headers_for_heartbeat", lambda _ws: {})
    report = tmp_path / "org_chart_v2.png"
    report.write_bytes(b"png")

    upload_response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "files": [
                {
                    "uri": "workspace:/workspace/.molecule/chat-uploads/org_chart_v2.png",
                    "name": "org_chart_v2.png",
                    "mimeType": "image/png",
                    "size": 3,
                }
            ]
        },
        text="",
    )
    notify_response = SimpleNamespace(status_code=200)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.side_effect = [upload_response, notify_response]

        result = _run(
            messaging.tool_send_message_to_user(
                "see attached",
                [{"path": str(report)}],
            )
        )

    assert result == "Message sent to user with 1 attachment(s)"
    assert mock_client.post.call_count == 2
    upload_call, notify_call = mock_client.post.call_args_list
    assert upload_call.args[0] == "http://platform.test/workspaces/ws-test/chat/uploads"
    assert notify_call.args[0] == "http://platform.test/workspaces/ws-test/notify"
    assert notify_call.kwargs["json"]["attachments"][0]["name"] == "org_chart_v2.png"
