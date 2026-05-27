"""Regression tests for set_current_task() phantom-busy fix (issue #1372)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class MockHeartbeat:
    """Minimal heartbeat object matching the shape used by adapters."""

    def __init__(self):
        self.current_task = ""
        self.active_tasks = 0


def _run(coro):
    """Run an async coroutine synchronously (no pytest-asyncio available)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _install_mock_http_client(monkeypatch) -> AsyncMock:
    """Pin a mock onto the shared `executor_helpers._http_client`.

    runtime #2914: `set_current_task` re-exports the authenticated
    `executor_helpers` implementation, which posts via the shared
    `get_http_client()` (and attaches `platform_auth.auth_headers()`) rather
    than opening a raw `httpx.AsyncClient`. Tests must mock that shared client.
    """
    import molecule_runtime.executor_helpers as eh

    client = AsyncMock()
    client.is_closed = False
    monkeypatch.setattr(eh, "_http_client", client)
    return client


class TestSetCurrentTask:
    """set_current_task() must push an AUTHENTICATED heartbeat on SET and CLEAR."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ID", "test-workspace-001")
        monkeypatch.setenv("PLATFORM_URL", "http://test.platform:8080")

    @pytest.fixture(autouse=True)
    def _reset_http_client(self):
        import molecule_runtime.executor_helpers as eh

        eh.reset_http_client_for_tests()
        yield
        eh.reset_http_client_for_tests()

    @pytest.fixture
    def heartbeat(self):
        return MockHeartbeat()

    def test_set_pushes_with_active_tasks_1(self, heartbeat, monkeypatch):
        """Setting a task posts active_tasks=1 immediately via the shared client."""
        from molecule_runtime.adapters.shared_runtime import set_current_task

        client = _install_mock_http_client(monkeypatch)
        client.post = AsyncMock(return_value=AsyncMock(status_code=200))

        _run(set_current_task(heartbeat, "Summarising docs"))

        client.post.assert_called_once()
        call_args = client.post.call_args
        assert call_args.kwargs["json"]["active_tasks"] == 1
        assert call_args.kwargs["json"]["current_task"] == "Summarising docs"
        # The authenticated path always passes a headers kwarg (auth_headers()).
        assert "headers" in call_args.kwargs

        assert heartbeat.active_tasks == 1
        assert heartbeat.current_task == "Summarising docs"

    def test_clear_pushes_with_active_tasks_0(self, heartbeat, monkeypatch):
        """Clearing a task posts active_tasks=0 immediately (phantom-busy fix)."""
        from molecule_runtime.adapters.shared_runtime import set_current_task

        heartbeat.current_task = "Previous task"
        heartbeat.active_tasks = 1

        client = _install_mock_http_client(monkeypatch)
        client.post = AsyncMock(return_value=AsyncMock(status_code=200))

        _run(set_current_task(heartbeat, ""))

        client.post.assert_called_once()
        call_args = client.post.call_args
        assert call_args.kwargs["json"]["active_tasks"] == 0
        assert call_args.kwargs["json"]["current_task"] == ""

        assert heartbeat.active_tasks == 0
        assert heartbeat.current_task == ""

    def test_clear_updates_heartbeat_object_even_if_post_fails(self, heartbeat, monkeypatch):
        """Heartbeat object is updated even when the HTTP POST raises."""
        from molecule_runtime.adapters.shared_runtime import set_current_task

        heartbeat.current_task = "Long running task"
        heartbeat.active_tasks = 1

        client = _install_mock_http_client(monkeypatch)
        client.post = AsyncMock(side_effect=Exception("network error"))

        _run(set_current_task(heartbeat, ""))

        # Heartbeat object must still be updated even if post fails
        assert heartbeat.active_tasks == 0
        assert heartbeat.current_task == ""

    def test_no_env_vars_skips_post(self, monkeypatch):
        """When WORKSPACE_ID or PLATFORM_URL is absent, post is skipped."""
        from molecule_runtime.adapters.shared_runtime import set_current_task

        heartbeat = MockHeartbeat()
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        monkeypatch.setenv("PLATFORM_URL", "http://test.platform:8080")

        client = _install_mock_http_client(monkeypatch)
        client.post = AsyncMock()

        _run(set_current_task(heartbeat, "Any task"))

        client.post.assert_not_called()


def test_extract_message_text_appends_attachment_manifest(monkeypatch, tmp_path):
    """Shared-runtime adapters should see fetched/local attachments in the
    prompt, not just the text parts."""
    from molecule_runtime.adapters.shared_runtime import extract_message_text

    attached = tmp_path / "report.pdf"
    attached.write_bytes(b"%PDF")
    monkeypatch.setattr(
        "molecule_runtime.executor_helpers.WORKSPACE_MOUNT",
        str(tmp_path),
    )

    context = SimpleNamespace(message=SimpleNamespace(parts=[
        SimpleNamespace(text="summarize this"),
        SimpleNamespace(root=SimpleNamespace(kind="file", file=SimpleNamespace(
            uri=f"workspace:{attached}",
            name="report.pdf",
            mimeType="application/pdf",
        ))),
    ]))

    text = extract_message_text(context)

    assert "summarize this" in text
    assert "Attached files:" in text
    assert "report.pdf (application/pdf)" in text
    assert str(attached) in text


def test_extract_message_text_file_only_message_returns_manifest(monkeypatch, tmp_path):
    """File-only requests should be actionable instead of becoming an empty
    prompt for shared-runtime adapters."""
    from molecule_runtime.adapters.shared_runtime import extract_message_text

    image = tmp_path / "diagram.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(
        "molecule_runtime.executor_helpers.WORKSPACE_MOUNT",
        str(tmp_path),
    )

    context = SimpleNamespace(message=SimpleNamespace(parts=[
        SimpleNamespace(root=SimpleNamespace(kind="file", file=SimpleNamespace(
            uri=f"workspace:{image}",
            name="diagram.png",
            mimeType="image/png",
        ))),
    ]))

    text = extract_message_text(context)

    assert text.startswith("Attached files:")
    assert "diagram.png (image/png)" in text
    assert str(image) in text


def test_none_heartbeat_skips_post(monkeypatch):
    """Passing None as heartbeat object skips post (no-op, no crash).

    When heartbeat is None the function must not raise even if env vars
    are present — None is valid when heartbeat isn't wired yet. (This used to
    be dead code nested inside another test; lifted to module scope in
    runtime #2914 and re-pointed at the shared authenticated client.)
    """
    from molecule_runtime.adapters.shared_runtime import set_current_task

    # Ensure no env vars so the heartbeat push is definitely skipped.
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    monkeypatch.delenv("PLATFORM_URL", raising=False)

    client = _install_mock_http_client(monkeypatch)
    client.post = AsyncMock()

    # Must not raise — None is valid when heartbeat isn't wired yet
    _run(set_current_task(None, "Task"))

    client.post.assert_not_called()
