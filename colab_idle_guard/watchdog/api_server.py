"""Authenticated local HTTP API for the Chrome and VS Code integrations.

Security (ARCHITECTURE ADR-6):
* bound to 127.0.0.1 only, with SO_EXCLUSIVEADDRUSE so no other process can bind the port;
* ``Host`` must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding);
* any ``Origin`` must be a pinned ``chrome-extension://<id>``; web origins are refused,
  and no CORS headers are ever sent, so web pages cannot read responses;
* every endpoint except a minimal health check needs ``Authorization: Bearer <token>``,
  compared in constant time;
* POST bodies must be JSON, at most 64 KiB, and are validated field by field.
Only event kinds, counts and opaque hashed notebook IDs are accepted; no content.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sys
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .. import __version__
from .activity_monitor import ActivityMonitor
from .commands import CommandQueue
from .configuration import ConfigManager
from .execution_monitor import ExecState, ExecutionMonitor, NotebookReport, RuntimeConn
from .integrations import INTEGRATION_KINDS, IntegrationRegistry
from .logging_manager import log_event
from .security import tokens_equal

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
MAX_EVENTS = 200
MAX_NOTEBOOKS = 50
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_EXT_ID_RE = re.compile(r"^[a-p]{32}$")
# ID of the official Chrome extension, fixed by the public "key" in its manifest
# (the same whatever folder it is loaded from). Always accepted; any other
# extension ID must be pinned (first authenticated use, or the configuration).
OFFICIAL_CHROME_EXTENSION_ID = "glchpgialnkmebkjemimjgfijpnkdcdi"


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ApiContext:
    token: Callable[[], str]
    config: ConfigManager
    registry: IntegrationRegistry
    activity: ActivityMonitor
    execution: ExecutionMonitor
    commands: CommandQueue
    status: Callable[[], dict[str, Any]]
    on_command_result: Callable[[Any], None] | None = None


# --------------------------------------------------------------- validation

def _require(body: dict[str, Any], key: str, kind: type, *, optional: bool = False) -> Any:
    if key not in body:
        if optional:
            return None
        raise RequestError(HTTPStatus.BAD_REQUEST, f"missing field: {key}")
    value = body[key]
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"invalid field: {key}")
    return value


def _ident(body: dict[str, Any], key: str) -> str:
    value = _require(body, key, str)
    if not _ID_RE.match(value):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"invalid identifier: {key}")
    return value


def _integration(body: dict[str, Any]) -> str:
    kind = _require(body, "integration", str)
    if kind not in INTEGRATION_KINDS:
        raise RequestError(HTTPStatus.BAD_REQUEST, "unknown integration")
    return kind


def _notebooks(body: dict[str, Any]) -> list[NotebookReport]:
    raw = _require(body, "notebooks", list)
    if len(raw) > MAX_NOTEBOOKS:
        raise RequestError(HTTPStatus.BAD_REQUEST, "too many notebooks")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid notebook entry")
        try:
            execution = ExecState(_require(item, "execution", str))
            runtime = RuntimeConn(_require(item, "runtime", str))
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid execution or runtime value") from None
        cells = _require(item, "running_cells", int)
        if not 0 <= cells <= 10_000:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid running_cells")
        out.append(NotebookReport(_ident(item, "notebook_id"), execution, cells, runtime))
    return out


# ------------------------------------------------------------------- server

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows would let another process hijack the port.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], ctx: ApiContext) -> None:
        self.ctx = ctx
        self.failed_auth_last_log = 0.0
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "ColabIdleGuard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("http %s", format % args)

    # -- dispatch

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        # No CORS preflight is ever approved: web pages cannot call this API.
        self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})

    def do_PUT(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    do_DELETE = do_PATCH = do_PUT

    def _dispatch(self, method: str) -> None:
        try:
            self._check_transport()
            path = self.path.split("?", 1)[0]
            if method == "GET" and path == "/v1/health":
                self._send(HTTPStatus.OK, {"app": "colab-idle-guard", "version": __version__,
                                           "authenticated": self._authorized()})
                return
            route = _ROUTES.get((method, path))
            if route is None:
                raise RequestError(HTTPStatus.NOT_FOUND, "not found")
            if not self._authorized():
                self._log_failed_auth()
                raise RequestError(HTTPStatus.UNAUTHORIZED, "unauthorized")
            body = self._read_json() if method == "POST" else {}
            self._check_integration_origin(body)
            self._send(HTTPStatus.OK, route(self.server.ctx, body))
        except RequestError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception:
            log.exception("api handler error")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    # -- checks

    def _check_transport(self) -> None:
        if self.client_address[0] != "127.0.0.1":
            raise RequestError(HTTPStatus.FORBIDDEN, "forbidden")
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise RequestError(HTTPStatus.FORBIDDEN, "invalid host")
        origin = self.headers.get("Origin")
        if origin is None:
            return
        if not origin.startswith("chrome-extension://"):
            raise RequestError(HTTPStatus.FORBIDDEN, "origin not allowed")
        ext_id = origin[len("chrome-extension://"):].rstrip("/")
        if not _EXT_ID_RE.match(ext_id):
            raise RequestError(HTTPStatus.FORBIDDEN, "origin not allowed")
        if ext_id == OFFICIAL_CHROME_EXTENSION_ID:
            return
        allowed = self.server.ctx.config.get("allowed_chrome_extension_ids")
        if allowed and ext_id not in allowed:
            raise RequestError(HTTPStatus.FORBIDDEN, "extension not allowed")

    def _check_integration_origin(self, body: dict[str, Any]) -> None:
        """Pin the Chrome extension ID on the first authenticated request (trust on first use)."""
        origin = self.headers.get("Origin")
        kind = body.get("integration")
        if origin is not None and kind not in (None, "chrome"):
            raise RequestError(HTTPStatus.FORBIDDEN, "origin does not match integration")
        if origin is None:
            return
        ext_id = origin[len("chrome-extension://"):].rstrip("/")
        cfg = self.server.ctx.config
        if ext_id != OFFICIAL_CHROME_EXTENSION_ID and not cfg.get("allowed_chrome_extension_ids"):
            cfg.update({"allowed_chrome_extension_ids": [ext_id]})
            log_event(log, "chrome_extension_pinned", extension_id=ext_id)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        return tokens_equal(header[7:].strip(), self.server.ctx.token())

    def _log_failed_auth(self) -> None:
        import time

        now = time.monotonic()
        if now - self.server.failed_auth_last_log > 30:
            self.server.failed_auth_last_log = now
            log_event(log, "api_auth_failed", logging.WARNING, path=self.path.split("?", 1)[0])

    def _read_json(self) -> dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0:
            raise RequestError(HTTPStatus.LENGTH_REQUIRED, "content length required")
        if length > MAX_BODY:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length)
        self._body_consumed = True
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid json") from None
        if not isinstance(body, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "json object expected")
        return body

    def _drain_body(self) -> None:
        """Discard an unread request body (bounded), so closing the socket does not
        reset the connection before the client has read the error response."""
        if getattr(self, "_body_consumed", False):
            return
        self._body_consumed = True
        try:
            remaining = min(int(self.headers.get("Content-Length") or 0), 1024 * 1024)
        except ValueError:
            return
        self.connection.settimeout(2)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if status >= 400:
            # A rejected request's body may be unread; closing the connection
            # stops it being parsed as the next keep-alive request.
            self.close_connection = True
            self.send_header("Connection", "close")
            self._drain_body()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


# ------------------------------------------------------------------- routes

def _touch(ctx: ApiContext, body: dict[str, Any]) -> tuple[str, str]:
    instance_id, kind = _ident(body, "instance_id"), _integration(body)
    raw_version = body.get("version")
    version = raw_version[:32] if isinstance(raw_version, str) else ""
    try:
        _, came_online = ctx.registry.touch(instance_id, kind, version)
    except ValueError as exc:
        raise RequestError(HTTPStatus.CONFLICT, str(exc)) from None
    if came_online:
        log_event(log, "integration_connected", integration=kind, instance=instance_id, version=version)
    return instance_id, kind


def _session_info(ctx: ApiContext, kind: str) -> dict[str, Any]:
    cfg = ctx.config.snapshot()
    return {"heartbeat_interval_seconds": cfg["heartbeat_interval_seconds"], "monitored": bool(cfg[f"monitor_{kind}"])}


def _hello(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    _, kind = _touch(ctx, body)
    return _session_info(ctx, kind)


def _heartbeat(ctx: ApiContext, body: dict[str, Any], default_full: bool = True) -> dict[str, Any]:
    instance_id, kind = _touch(ctx, body)
    full = body.get("full_snapshot", default_full)
    if not isinstance(full, bool):
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid field: full_snapshot")
    ctx.execution.report(instance_id, kind, _notebooks(body), full_snapshot=full)
    return {**_session_info(ctx, kind), "commands": ctx.commands.pull(instance_id)}


def _state(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    return _heartbeat(ctx, body, default_full=False)


def _activity(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    _, kind = _touch(ctx, body)
    raw = _require(body, "events", list)
    if len(raw) > MAX_EVENTS:
        raise RequestError(HTTPStatus.BAD_REQUEST, "too many events")
    events = []
    for item in raw:
        if not isinstance(item, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid event")
        age_ms = _require(item, "age_ms", int)
        events.append((_require(item, "kind", str)[:32], age_ms / 1000.0))
    result = ctx.activity.record(kind, events)
    return {"accepted": result.accepted, "rejected": result.rejected}


def _goodbye(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    instance_id, kind = _ident(body, "instance_id"), _integration(body)
    final = _notebooks(body) if "notebooks" in body else None
    ctx.execution.goodbye(instance_id, kind, final)
    ctx.registry.close(instance_id)
    log_event(log, "integration_disconnected", integration=kind, instance=instance_id)
    return {"ok": True}


def _command_result(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    instance_id, _kind = _touch(ctx, body)
    ok = _require(body, "ok", bool)
    detail = _require(body, "detail", str, optional=True) or ""
    cmd = ctx.commands.complete(instance_id, _ident(body, "command_id"), _require(body, "nonce", str), ok, detail)
    if cmd is None:
        raise RequestError(HTTPStatus.CONFLICT, "unknown, expired or already completed command")
    log_event(log, "command_result", command=cmd.type, ok=ok, detail=detail[:200])
    if ctx.on_command_result:
        ctx.on_command_result(cmd)
    return {"ok": True}


def _status(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    return ctx.status()


_ROUTES: dict[tuple[str, str], Callable[[ApiContext, dict[str, Any]], dict[str, Any]]] = {
    ("POST", "/v1/hello"): _hello,
    ("POST", "/v1/heartbeat"): _heartbeat,
    ("POST", "/v1/state"): _state,
    ("POST", "/v1/activity"): _activity,
    ("POST", "/v1/goodbye"): _goodbye,
    ("POST", "/v1/command_result"): _command_result,
    ("GET", "/v1/status"): _status,
}


class ApiServer:
    def __init__(self, ctx: ApiContext, port: int) -> None:
        self._ctx = ctx
        self._requested_port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server not started")
        return self._server.server_address[1]

    def start(self) -> None:
        self._server = _Server(("127.0.0.1", self._requested_port), self._ctx)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="api-server", daemon=True)
        self._thread.start()
        log_event(log, "api_server_started", port=self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log_event(log, "api_server_stopped")
