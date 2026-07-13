"""Private local A2A transport for plugin-declared channel daemons.

Issue #215 PR-2 does not define a second channel protocol.  It exposes the
workspace's existing A2A ASGI application over Unix HTTP and gives each
installed plugin identity its own socket binding.  A daemon therefore sends
the same ``message/send`` or ``message/stream`` JSON-RPC body it would send to
the platform HTTP lane and receives the same result / SSE events back.

The per-plugin binding is also the runtime's provenance anchor.  The wrapper
on that binding overwrites ``params.metadata.source`` (and the mirrored
``params.message.metadata.source``) with the plugin identity discovered from
``plugin.yaml``.  A client-supplied source is never trusted.  Other existing
channel metadata (``chat_id``, ``user_id``, ``username``, ``message_id``) stays
in the platform wire shape unchanged.

Security boundary: sockets live in a runtime-created mode-0700 directory and
are chmod 0600 before a daemon is spawned.  There is deliberately no parallel
token/auth protocol: filesystem access plus the runtime-selected binding is
the local capability described by issue #215.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable

import httpx
import uvicorn

logger = logging.getLogger(__name__)


CHANNEL_A2A_SOCKET_ENV = "MOLECULE_CHANNEL_A2A_SOCKET"
"""Unix socket path injected into a manifest-declared daemon."""

CHANNEL_PLUGIN_ID_ENV = "MOLECULE_CHANNEL_PLUGIN_ID"
"""Runtime-assigned plugin identity stamped on requests from the socket."""

# Match molecule-core's public A2A proxy request ceiling.  The local fast path
# must not accept a larger body than the remote fallback path.
MAX_A2A_REQUEST_BYTES = 16 << 20

_SOCKET_PATH_MAX_BYTES = 100  # conservative across Linux (108) and macOS (104)
_SOURCE_KEY = "source"


class ChannelEventUnavailable(RuntimeError):
    """The runtime did not publish a usable local channel capability."""


class ChannelEventProtocolError(RuntimeError):
    """The local A2A listener returned a non-contract response."""


def build_channel_message_send_request(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical A2A ``message/send`` JSON-RPC request.

    This is a thin transport-facing wrapper around
    :func:`a2a_client.build_message_send_params`, the runtime's existing
    a2a-sdk-backed wire builder.  Channel plugins should use this rather than
    copying a JSON literal.  ``source`` may be omitted (and any supplied value
    is ignored by the server); the per-plugin socket stamps it runtime-side.
    """
    from molecule_runtime.a2a_client import build_message_send_params

    request_id = request_id or str(uuid.uuid4())
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": build_message_send_params(
            text,
            message_id=message_id or request_id,
            metadata=metadata,
            attachments=attachments,
        ),
    }


def channel_message_response_text(payload: dict[str, Any]) -> str:
    """Extract reply text from current A2A v1 or legacy message results.

    a2a-sdk 1.x returns a completed ``Task`` whose terminal agent ``Message``
    lives at ``result.status.message``.  Older v0.3-compatible responders may
    return a ``Message`` directly at ``result``.  Keep this compatibility
    adapter beside the local sender so channel plugins do not fork response
    parsing.  JSON-RPC errors raise :class:`ChannelEventProtocolError`.
    """
    error = payload.get("error")
    if error is not None:
        if isinstance(error, dict):
            detail = error.get("message") or str(error)
        else:
            detail = str(error)
        raise ChannelEventProtocolError(f"local channel A2A error: {detail[:500]}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise ChannelEventProtocolError("local channel A2A result was not an object")

    status = result.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict):
            text = _text_from_parts(message.get("parts"))
            if text:
                return text

    # v0.3 / direct-message compatibility.
    text = _text_from_parts(result.get("parts"))
    if text:
        return text

    # Defensive fallback for a task implementation that omits status.message
    # but carries the emitted text artifacts.
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        artifact_text = "".join(
            _text_from_parts(artifact.get("parts"))
            for artifact in artifacts
            if isinstance(artifact, dict)
        )
        if artifact_text:
            return artifact_text
    return ""


def _text_from_parts(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("kind") == "text"
    )


async def send_channel_message(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    socket_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    """Send one canonical ``message/send`` turn over the local Unix binding.

    Returns the decoded, unchanged JSON-RPC response (either ``result`` or
    ``error``).  Absence of the runtime-issued socket raises
    :class:`ChannelEventUnavailable`; connection failures remain ordinary
    :class:`httpx.TransportError` instances so a plugin can select its existing
    platform HTTP/poll fallback without misclassifying an agent JSON-RPC error.
    """
    resolved_path = os.fspath(socket_path) if socket_path is not None else ""
    resolved_path = (
        resolved_path.strip() or os.environ.get(CHANNEL_A2A_SOCKET_ENV, "").strip()
    )
    if not resolved_path:
        raise ChannelEventUnavailable(
            f"{CHANNEL_A2A_SOCKET_ENV} is absent; use the platform HTTP/poll fallback"
        )

    request = build_channel_message_send_request(
        text,
        metadata=metadata,
        request_id=request_id,
        message_id=message_id,
        attachments=attachments,
    )
    transport = httpx.AsyncHTTPTransport(uds=resolved_path)
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://molecule.local",
        timeout=timeout,
    ) as client:
        response = await client.post("/", json=request)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as error:
        raise ChannelEventProtocolError(
            "local channel A2A response was not valid JSON"
        ) from error
    if not (
        isinstance(payload, dict)
        and payload.get("jsonrpc") == "2.0"
        and ("result" in payload or "error" in payload)
    ):
        raise ChannelEventProtocolError(
            "local channel A2A response was not a JSON-RPC result/error envelope"
        )
    return payload


class RuntimeStampedChannelProvenance:
    """ASGI wrapper that stamps the runtime-owned channel plugin identity."""

    def __init__(
        self,
        app: Any,
        plugin_id: str,
        *,
        max_request_bytes: int = MAX_A2A_REQUEST_BYTES,
    ) -> None:
        identity = plugin_id.strip()
        if not identity:
            raise ValueError(
                "channel event binding requires a non-empty plugin identity"
            )
        self.app = app
        self.plugin_id = identity
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope, receive, send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/"
        ):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_bytes:
                await _jsonrpc_error(
                    send,
                    status=413,
                    request_id=None,
                    code=-32600,
                    message=(
                        "A2A request body exceeds the "
                        f"{self.max_request_bytes}-byte limit"
                    ),
                )
                return
            if not message.get("more_body", False):
                break

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _jsonrpc_error(
                send,
                status=400,
                request_id=None,
                code=-32700,
                message="invalid JSON",
            )
            return

        request_id = payload.get("id") if isinstance(payload, dict) else None
        problem = _stamp_source(payload, self.plugin_id)
        if problem:
            await _jsonrpc_error(
                send,
                status=400,
                request_id=request_id,
                code=-32600,
                message=problem,
            )
            return

        stamped_body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        stamped_scope = dict(scope)
        headers = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != b"content-length"
        ]
        headers.append((b"content-length", str(len(stamped_body)).encode("ascii")))
        stamped_scope["headers"] = headers

        delivered = False

        async def stamped_receive():
            nonlocal delivered
            if delivered:
                # Keep delegating disconnect detection to the real protocol.
                # Returning a synthetic disconnect here cancels Starlette's
                # StreamingResponse after its first SSE chunk.
                return await receive()
            delivered = True
            return {"type": "http.request", "body": stamped_body, "more_body": False}

        await self.app(stamped_scope, stamped_receive, send)


def _stamp_source(payload: object, plugin_id: str) -> str | None:
    """Stamp ``plugin_id`` into a valid single-request A2A envelope.

    Returns a JSON-RPC-safe error message when the provenance-bearing shape is
    malformed.  The local lane rejects batches and malformed metadata rather
    than forwarding a request whose source could not be stamped.
    """
    if not isinstance(payload, dict):
        return "local channel A2A requests must be a JSON object"
    params = payload.get("params")
    if not isinstance(params, dict):
        return "local channel A2A request must contain object params"

    metadata = params.get("metadata")
    if metadata is None:
        metadata = {}
        params["metadata"] = metadata
    if not isinstance(metadata, dict):
        return "params.metadata must be an object"
    metadata[_SOURCE_KEY] = plugin_id

    message = params.get("message")
    if isinstance(message, dict):
        message_metadata = message.get("metadata")
        if message_metadata is None:
            message_metadata = {}
            message["metadata"] = message_metadata
        if not isinstance(message_metadata, dict):
            return "params.message.metadata must be an object"
        # Mirror the trusted identity onto the message surface as well.  Some
        # A2A SDK versions surface params metadata on RequestContext and message
        # metadata on Message; stamping both prevents an ambiguous second claim.
        message_metadata[_SOURCE_KEY] = plugin_id
    return None


async def _jsonrpc_error(
    send,
    *,
    status: int,
    request_id: object,
    code: int,
    message: str,
) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _LocalUvicornServer(uvicorn.Server):
    """Uvicorn server that leaves process signal ownership to main()."""

    # uvicorn 0.30 used this hook directly; newer versions wrap serve() in
    # capture_signals().  Override both because the runtime supports >=0.30.
    def install_signal_handlers(self) -> None:
        return None

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class ChannelEventSocketManager:
    """Own one private A2A Unix binding per discovered plugin identity."""

    def __init__(
        self,
        app: Any,
        specs: Iterable[Any],
        *,
        socket_dir: str | os.PathLike[str] | None = None,
        log_level: str = "warning",
        startup_timeout_seconds: float = 10.0,
    ) -> None:
        self.app = app
        self.specs = list(specs)
        self.log_level = log_level
        self.startup_timeout_seconds = startup_timeout_seconds
        self._socket_dir = Path(socket_dir) if socket_dir is not None else None
        self._owns_socket_dir = socket_dir is None
        self._servers: dict[str, _LocalUvicornServer] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._paths: dict[str, Path] = {}
        self._started = False

    async def start(self) -> bool:
        """Bind every socket, chmod it, then publish paths to daemon env."""
        if self._started:
            return True
        # Manifest env is untrusted for these reserved keys.  Clear claims up
        # front, including on malformed specs that have no plugin identity;
        # authoritative values are injected only after every bind is ready.
        self.clear_daemon_env()
        plugin_ids = self._plugin_ids()
        if not plugin_ids:
            return True

        try:
            socket_dir = self._prepare_socket_dir()
            for plugin_id in plugin_ids:
                path = socket_dir / _socket_name(plugin_id)
                self._prepare_socket_path(path)
                wrapped_app = RuntimeStampedChannelProvenance(self.app, plugin_id)
                config = uvicorn.Config(
                    wrapped_app,
                    uds=str(path),
                    log_level=self.log_level,
                    access_log=False,
                    # The main TCP server owns the shared app lifespan.  Running
                    # startup/shutdown once per plugin would duplicate hooks.
                    lifespan="off",
                )
                server = _LocalUvicornServer(config)
                self._servers[plugin_id] = server
                self._paths[plugin_id] = path
                self._tasks[plugin_id] = asyncio.create_task(
                    server.serve(), name=f"channel-a2a:{plugin_id}"
                )

            await self._wait_until_bound()
            for path in self._paths.values():
                _secure_socket(path)
            self._inject_daemon_env()
            self._started = True
            logger.info(
                "channel events: %d private A2A socket(s) ready for %d daemon(s)",
                len(self._paths),
                len(self.specs),
            )
            return True
        except BaseException:
            self.clear_daemon_env()
            await self._shutdown_servers()
            self._cleanup_paths()
            raise

    async def stop(self) -> None:
        """Stop local listeners and remove their filesystem capabilities."""
        await self._shutdown_servers()
        self._cleanup_paths()
        self._started = False

    def clear_daemon_env(self) -> None:
        """Remove reserved capability variables after a bind failure."""
        for spec in self.specs:
            spec.env.pop(CHANNEL_A2A_SOCKET_ENV, None)
            spec.env.pop(CHANNEL_PLUGIN_ID_ENV, None)

    def _plugin_ids(self) -> list[str]:
        identities: list[str] = []
        seen: set[str] = set()
        for spec in self.specs:
            identity = str(getattr(spec, "plugin", "") or "").strip()
            if not identity:
                logger.warning(
                    "plugin daemon %s has no owning plugin identity; local A2A "
                    "capability withheld",
                    getattr(spec, "name", "?"),
                )
                continue
            if identity not in seen:
                identities.append(identity)
                seen.add(identity)
        return identities

    def _prepare_socket_dir(self) -> Path:
        if self._socket_dir is None:
            # macOS's default TMPDIR lives under a very long /var/folders path
            # that can exceed sockaddr_un.sun_path before the filename is even
            # added.  Prefer the conventional short /tmp alias when writable;
            # the directory itself is still an unpredictable mode-0700 mkdtemp.
            short_tmp = (
                "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
            )
            self._socket_dir = Path(tempfile.mkdtemp(prefix="mc-", dir=short_tmp))
        else:
            if self._socket_dir.is_symlink():
                raise RuntimeError(
                    f"channel event socket directory is a symlink: {self._socket_dir}"
                )
            self._socket_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

        directory = self._socket_dir
        if directory.is_symlink():
            raise RuntimeError(
                f"channel event socket directory is a symlink: {directory}"
            )
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(
                f"channel event socket path is not a directory: {directory}"
            )
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError(
                f"channel event socket directory is not owned by this runtime: {directory}"
            )
        os.chmod(directory, 0o700)
        return directory

    @staticmethod
    def _prepare_socket_path(path: Path) -> None:
        if len(os.fsencode(path)) > _SOCKET_PATH_MAX_BYTES:
            raise RuntimeError(
                f"channel event socket path is too long for a portable Unix socket: {path}"
            )
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(
                f"refusing to replace non-socket channel event path: {path}"
            )
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError(
                f"refusing to replace socket not owned by this runtime: {path}"
            )
        path.unlink()

    async def _wait_until_bound(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        pending = set(self._servers)
        while pending:
            for plugin_id in list(pending):
                server = self._servers[plugin_id]
                task = self._tasks[plugin_id]
                if server.started:
                    pending.remove(plugin_id)
                elif task.done():
                    error = task.exception()
                    raise RuntimeError(
                        f"channel event socket for {plugin_id} stopped before bind: {error}"
                    )
            if not pending:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    "timed out binding channel event socket(s): "
                    + ", ".join(sorted(pending))
                )
            await asyncio.sleep(0.01)

    def _inject_daemon_env(self) -> None:
        for spec in self.specs:
            plugin_id = str(getattr(spec, "plugin", "") or "").strip()
            path = self._paths.get(plugin_id)
            if path is None:
                continue
            # Reserved runtime values overwrite manifest env claims.
            spec.env[CHANNEL_A2A_SOCKET_ENV] = str(path)
            spec.env[CHANNEL_PLUGIN_ID_ENV] = plugin_id

    async def _shutdown_servers(self) -> None:
        for server in self._servers.values():
            server.should_exit = True
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks.values(), return_exceptions=True),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                for task in self._tasks.values():
                    task.cancel()
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._servers.clear()
        self._tasks.clear()

    def _cleanup_paths(self) -> None:
        for path in self._paths.values():
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISSOCK(info.st_mode) and (
                not hasattr(os, "getuid") or info.st_uid == os.getuid()
            ):
                path.unlink()
        self._paths.clear()
        if self._owns_socket_dir and self._socket_dir is not None:
            try:
                self._socket_dir.rmdir()
            except OSError:
                logger.warning(
                    "channel events: could not remove runtime socket directory %s",
                    self._socket_dir,
                )


def _socket_name(plugin_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", plugin_id).strip("-")[:24] or "plugin"
    digest = hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.sock"


def _secure_socket(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"channel event listener did not create a socket: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"channel event socket is not owned by this runtime: {path}")
    os.chmod(path, 0o600)
