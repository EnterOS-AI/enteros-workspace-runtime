"""Private local A2A transport for plugin-declared channel daemons.

Issue #215 PR-2 does not define a second channel protocol.  It exposes the
workspace's existing A2A ASGI application over Unix HTTP and gives each
installed plugin identity its own socket binding.  A daemon therefore sends
the same ``message/send`` or ``message/stream`` JSON-RPC body it would send to
the platform HTTP lane and receives the same result / SSE events back.

The per-plugin binding is also the runtime's provenance anchor.  The wrapper
on that binding overwrites only the canonical ``params.metadata.source`` with
the plugin identity discovered from ``plugin.yaml``.  A client-supplied source
is never trusted, and a second source claim in ``params.message.metadata`` is
rejected.  Other existing channel metadata (``chat_id``, ``user_id``,
``username``, ``message_id``) stays in the platform wire shape unchanged.

Security boundary: sockets live in a runtime-created mode-0700 directory and
are chmod 0600 before a daemon is spawned. Because plugin daemons share the
workspace UID, filesystem mode alone is not an identity boundary. Each plugin
therefore receives a distinct ephemeral capability token, and provenance is
stamped only after constant-time token verification on its selected binding.
Plugins remain trusted same-UID code; this prevents accidental cross-plugin
routing but does not claim process isolation from a malicious plugin.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable

import uvicorn

from molecule_runtime.channel_sdk import (
    CHANNEL_A2A_SOCKET_ENV,
    CHANNEL_A2A_TOKEN_ENV,
    CHANNEL_API_VERSION,
    CHANNEL_API_VERSION_ENV,
    CHANNEL_CAPABILITY_HEADER,
    CHANNEL_PLUGIN_ID_ENV,
    TRIGGER_A2A_SOCKET_ENV,
    TRIGGER_A2A_TOKEN_ENV,
    TRIGGER_API_VERSION_ENV,
    TRIGGER_PLUGIN_ID_ENV,
    ChannelCapabilityUnavailable,
    ChannelDeliveryUnknown,
    ChannelMessageSender,
    ChannelProtocolError,
    build_channel_message_send_request,
    channel_message_response_text,
    send_channel_message,
)

logger = logging.getLogger(__name__)


# Compatibility aliases for existing runtime consumers. Plugin authors import
# the SDK-owned names from ``molecule_plugin.channel`` instead.
ChannelEventUnavailable = ChannelCapabilityUnavailable
ChannelEventProtocolError = ChannelProtocolError
ChannelEventDeliveryUnknown = ChannelDeliveryUnknown

_CAPABILITY_HEADER = CHANNEL_CAPABILITY_HEADER.encode("ascii")

__all__ = [
    "CHANNEL_A2A_SOCKET_ENV",
    "CHANNEL_A2A_TOKEN_ENV",
    "CHANNEL_API_VERSION",
    "CHANNEL_API_VERSION_ENV",
    "CHANNEL_CAPABILITY_HEADER",
    "CHANNEL_PLUGIN_ID_ENV",
    "ChannelCapabilityUnavailable",
    "ChannelDeliveryUnknown",
    "ChannelEventDeliveryUnknown",
    "ChannelEventProtocolError",
    "ChannelEventSocketManager",
    "ChannelEventUnavailable",
    "ChannelMessageSender",
    "ChannelProtocolError",
    "RuntimeStampedChannelProvenance",
    "build_channel_message_send_request",
    "channel_message_response_text",
    "send_channel_message",
]

# Match molecule-core's public A2A proxy request ceiling.  The local fast path
# must not accept a larger body than the public A2A lane.
MAX_A2A_REQUEST_BYTES = 16 << 20

_SOCKET_PATH_MAX_BYTES = 100  # conservative across Linux (108) and macOS (104)
_SOURCE_KEY = "source"
_SOURCE_TYPE_KEY = "source_type"

# The kinds that receive a private local-A2A lane. `channel` bridges an external
# comms surface (source = plugin id); `trigger` fires autonomous self-turns
# (source_type in the allow-list below). Both share the socket + capability
# mechanism; they differ only in how provenance is stamped and which env vars
# carry the capability.
_LANE_KINDS = ("channel", "trigger")

# The ONLY source_type values a trigger plugin may cause. A scheduler is the
# first (today only) trigger type. This allow-list is the security boundary: it
# stops a trigger plugin forging a user turn, a channel turn, or any self-turn
# class it was not granted. Widen it deliberately when a new trigger type ships.
TRIGGER_ALLOWED_SOURCE_TYPES = frozenset({"self-scheduler"})
TRIGGER_DEFAULT_SOURCE_TYPE = "self-scheduler"


class RuntimeStampedChannelProvenance:
    """ASGI wrapper that stamps the runtime-owned channel plugin identity."""

    def __init__(
        self,
        app: Any,
        plugin_id: str,
        capability_token: str,
        *,
        max_request_bytes: int = MAX_A2A_REQUEST_BYTES,
        stamp: "Any | None" = None,
    ) -> None:
        identity = plugin_id.strip()
        if not identity:
            raise ValueError(
                "channel event binding requires a non-empty plugin identity"
            )
        self.app = app
        self.plugin_id = identity
        token = capability_token.strip()
        if not token:
            raise ValueError("channel event binding requires a non-empty capability token")
        self._capability_token = token.encode("utf-8")
        self.max_request_bytes = max_request_bytes
        # Per-lane provenance stamp (channel by default; a trigger lane passes
        # _stamp_trigger_source). Defaulted here rather than in the signature so
        # the module-level function need not exist at class-definition time.
        self._stamp = stamp if stamp is not None else _stamp_source

    async def __call__(self, scope, receive, send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/"
        ):
            await self.app(scope, receive, send)
            return

        provided = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == _CAPABILITY_HEADER
        ]
        if len(provided) != 1 or not secrets.compare_digest(
            provided[0], self._capability_token
        ):
            await _jsonrpc_error(
                send,
                request_id=None,
                status=401,
                code=-32001,
                message="invalid local channel capability",
            )
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
        problem = self._stamp(payload, self.plugin_id)
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
            if name.lower() not in {b"content-length", _CAPABILITY_HEADER}
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

    message = params.get("message")
    if isinstance(message, dict):
        message_metadata = message.get("metadata")
        if message_metadata is not None and not isinstance(message_metadata, dict):
            return "params.message.metadata must be an object"
        if isinstance(message_metadata, dict) and _SOURCE_KEY in message_metadata:
            return (
                "params.message.metadata.source is not allowed; channel provenance "
                "lives at params.metadata"
            )

    metadata = params.get("metadata")
    if metadata is None:
        metadata = {}
        params["metadata"] = metadata
    if not isinstance(metadata, dict):
        return "params.metadata must be an object"
    metadata[_SOURCE_KEY] = plugin_id
    return None


def _stamp_trigger_source(payload: object, plugin_id: str) -> str | None:
    """Stamp a trigger plugin's local A2A envelope as an autonomous self-turn.

    Unlike a channel (which represents an external party), a trigger fires the
    agent itself, so the turn is stamped ``source_type`` (the routine-self-ping
    classification the executor + autonomous-loop guard key on) — NOT an
    external ``source``. The trigger plugin controls only the prompt text; the
    runtime forces the identity:

      * Any daemon-supplied ``source`` / ``source_type`` at the message level is
        stripped (no message-level forging of provenance).
      * ``params.metadata.source_type`` must be in the allow-list; a value
        outside it is rejected, and an absent value defaults to the granted
        type. A trigger can therefore never impersonate a user turn, a channel,
        or an un-granted self-turn class.
      * ``params.metadata.source`` is set to the plugin id for audit provenance.
    """
    if not isinstance(payload, dict):
        return "local trigger A2A requests must be a JSON object"
    params = payload.get("params")
    if not isinstance(params, dict):
        return "local trigger A2A request must contain object params"

    message = params.get("message")
    if isinstance(message, dict):
        message_metadata = message.get("metadata")
        if message_metadata is not None and not isinstance(message_metadata, dict):
            return "params.message.metadata must be an object"
        if isinstance(message_metadata, dict):
            # strip forgeable provenance — the runtime owns the turn's identity
            message_metadata.pop(_SOURCE_KEY, None)
            message_metadata.pop(_SOURCE_TYPE_KEY, None)

    metadata = params.get("metadata")
    if metadata is None:
        metadata = {}
        params["metadata"] = metadata
    if not isinstance(metadata, dict):
        return "params.metadata must be an object"

    requested = metadata.get(_SOURCE_TYPE_KEY)
    if requested is not None and requested not in TRIGGER_ALLOWED_SOURCE_TYPES:
        return (
            f"source_type {requested!r} is not permitted for a trigger plugin "
            f"(allowed: {sorted(TRIGGER_ALLOWED_SOURCE_TYPES)})"
        )
    metadata[_SOURCE_TYPE_KEY] = (
        requested if requested in TRIGGER_ALLOWED_SOURCE_TYPES else TRIGGER_DEFAULT_SOURCE_TYPE
    )
    metadata[_SOURCE_KEY] = plugin_id
    return None


# Provenance stamper per lane kind. The socket/auth/body machinery in
# RuntimeStampedChannelProvenance is identical across kinds; only the stamp
# differs.
_STAMP_BY_KIND = {
    "channel": _stamp_source,
    "trigger": _stamp_trigger_source,
}


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
    """Own one private A2A Unix binding per ``kind: channel`` plugin."""

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
        self._tokens: dict[str, str] = {}
        self._kinds: dict[str, str] = {}  # plugin_id -> lane kind (channel|trigger)
        self._started = False

    async def start(self) -> bool:
        """Bind channel sockets, chmod them, then publish daemon env."""
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
                token = secrets.token_urlsafe(32)
                path = socket_dir / _socket_name(plugin_id)
                self._prepare_socket_path(path)
                wrapped_app = RuntimeStampedChannelProvenance(
                    self.app, plugin_id, token,
                    stamp=_STAMP_BY_KIND[self._kinds.get(plugin_id, "channel")],
                )
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
                self._tokens[plugin_id] = token
                self._tasks[plugin_id] = asyncio.create_task(
                    server.serve(), name=f"channel-a2a:{plugin_id}"
                )

            await self._wait_until_bound()
            for path in self._paths.values():
                _secure_socket(path)
            self._inject_daemon_env()
            self._started = True
            logger.info(
                "channel events: %d private A2A socket(s) ready for %d lane daemon(s) "
                "(%d channel, %d trigger)",
                len(self._paths),
                len(self._paths),
                sum(1 for k in self._kinds.values() if k == "channel"),
                sum(1 for k in self._kinds.values() if k == "trigger"),
            )
            return True
        except BaseException:
            self.clear_daemon_env()
            self._tokens.clear()
            await self._shutdown_servers()
            self._cleanup_paths()
            raise

    async def stop(self) -> None:
        """Stop local listeners and remove their filesystem capabilities."""
        await self._shutdown_servers()
        self.clear_daemon_env()
        self._cleanup_paths()
        self._tokens.clear()
        self._started = False

    def clear_daemon_env(self) -> None:
        """Remove reserved capability variables (both lanes) after a bind failure."""
        reserved = (
            CHANNEL_API_VERSION_ENV, CHANNEL_A2A_SOCKET_ENV,
            CHANNEL_A2A_TOKEN_ENV, CHANNEL_PLUGIN_ID_ENV,
            TRIGGER_API_VERSION_ENV, TRIGGER_A2A_SOCKET_ENV,
            TRIGGER_A2A_TOKEN_ENV, TRIGGER_PLUGIN_ID_ENV,
        )
        for spec in self.specs:
            for var in reserved:
                spec.env.pop(var, None)

    def _plugin_ids(self) -> list[str]:
        identities: list[str] = []
        seen: set[str] = set()
        self._kinds = {}
        for spec in self.specs:
            kind = str(getattr(spec, "kind", "") or "").strip()
            if kind not in _LANE_KINDS:
                continue
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
                self._kinds[identity] = kind
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
            kind = str(getattr(spec, "kind", "") or "").strip()
            if kind not in _LANE_KINDS:
                continue
            plugin_id = str(getattr(spec, "plugin", "") or "").strip()
            path = self._paths.get(plugin_id)
            token = self._tokens.get(plugin_id)
            if path is None or token is None:
                continue
            # Reserved runtime values overwrite manifest env claims. Each lane
            # kind gets its own env-var names so a channel and a trigger daemon
            # never see each other's socket/token.
            if kind == "trigger":
                spec.env[TRIGGER_API_VERSION_ENV] = CHANNEL_API_VERSION
                spec.env[TRIGGER_A2A_SOCKET_ENV] = str(path)
                spec.env[TRIGGER_A2A_TOKEN_ENV] = token
                spec.env[TRIGGER_PLUGIN_ID_ENV] = plugin_id
                # Point the daemon at the SAME durable state dir the runtime
                # schedule API resolves, so the API writes the grid this daemon
                # reads and reads the health/history it writes.
                from molecule_runtime.trigger_state import (
                    STATE_DIR_ENV as _TRIGGER_STATE_DIR_ENV,
                    resolve_trigger_state_dir,
                )

                spec.env[_TRIGGER_STATE_DIR_ENV] = str(resolve_trigger_state_dir())
            else:
                spec.env[CHANNEL_API_VERSION_ENV] = CHANNEL_API_VERSION
                spec.env[CHANNEL_A2A_SOCKET_ENV] = str(path)
                spec.env[CHANNEL_A2A_TOKEN_ENV] = token
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
