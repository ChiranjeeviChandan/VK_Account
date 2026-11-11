"""A small threaded HTTP server with routing, JSON, server-sent events and TLS.

Standard library only. Felix serves two listeners from the same routes:

* the **local** listener on 127.0.0.1 for the dashboard, CLI and overlay
* the **phone link** on the LAN over TLS, limited to the routes a paired
  phone needs

Each route says which callers may use it.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

from ..util import json_dumps

log = logging.getLogger("felix.http")

MAX_BODY = 4 * 1024 * 1024
OWNER, DEVICE, ANYONE = "owner", "device", "anyone"


class HttpError(Exception):
    def __init__(self, status: int, error: str, message: str = "") -> None:
        super().__init__(message or error)
        self.status, self.error, self.message = status, error, message


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any = None
    params: tuple[str, ...] = ()
    who: str = ANYONE              # owner | device | anyone
    device: dict[str, Any] | None = None
    channel: str = "local"         # which listener the request came in on

    def json(self) -> dict[str, Any]:
        if not isinstance(self.body, dict):
            raise HttpError(400, "bad_request", "a JSON object is required")
        return self.body

    def text(self, key: str, default: str = "") -> str:
        value = self.json().get(key, default)
        return value if isinstance(value, str) else default


@dataclass
class Response:
    body: Any = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json; charset=utf-8"


class Stream:
    """A server-sent-event response. ``events`` yields (event name, data) pairs;
    a pair of (None, None) is sent as a keep-alive comment."""

    def __init__(self, events: Iterator[tuple[str | None, Any]]) -> None:
        self.events = events


Handler = Callable[[Request], "Response | Stream | dict | list | None"]


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    allow: tuple[str, ...]


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, allow: tuple[str, ...] = (OWNER,)) -> None:
        self.routes.append(Route(method, re.compile("^" + pattern + "$"), handler, allow))

    def get(self, pattern: str, allow: tuple[str, ...] = (OWNER,)):
        return lambda fn: (self.add("GET", pattern, fn, allow), fn)[1]

    def post(self, pattern: str, allow: tuple[str, ...] = (OWNER,)):
        return lambda fn: (self.add("POST", pattern, fn, allow), fn)[1]

    def delete(self, pattern: str, allow: tuple[str, ...] = (OWNER,)):
        return lambda fn: (self.add("DELETE", pattern, fn, allow), fn)[1]

    def match(self, method: str, path: str) -> tuple[Route, tuple[str, ...]] | None:
        found_path = False
        for route in self.routes:
            match = route.pattern.match(path)
            if match:
                found_path = True
                if route.method == method:
                    return route, match.groups()
        if found_path:
            raise HttpError(405, "method_not_allowed")
        return None


Authenticator = Callable[[Request], None]


class Listener:
    """One bound socket serving the router to one kind of caller."""

    def __init__(self, router: Router, host: str, port: int, channel: str, authenticate: Authenticator,
                 tls: ssl.SSLContext | None = None, extra_headers: dict[str, str] | None = None) -> None:
        self.router, self.channel, self.authenticate = router, channel, authenticate
        self.extra_headers = extra_headers or {}
        listener = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "Felix"
            sys_version = ""

            def setup(self) -> None:
                self.request.settimeout(90)
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                super().setup()

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("%s %s", self.address_string(), fmt % args)

            def do_GET(self) -> None:  # noqa: N802
                listener.handle(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                listener.handle(self, "POST")

            def do_DELETE(self) -> None:  # noqa: N802
                listener.handle(self, "DELETE")

            def do_OPTIONS(self) -> None:  # noqa: N802
                listener.reply(self, Response({"error": "forbidden"}, 403))    # no cross-origin access at all

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 64

            def handle_error(self, request: Any, client_address: Any) -> None:
                log.debug("connection error from %s", client_address, exc_info=True)

        self.server = Server((host, port), RequestHandler)
        if tls is not None:
            # The handshake happens on first read, inside the worker thread, so one
            # slow client cannot stall everybody else at accept().
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True, do_handshake_on_connect=False)
        self.port = self.server.server_address[1]
        self.host = host
        self.tls = tls is not None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name=f"felix-http-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- request handling ------------------------------------------------------------
    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        try:
            response = self._dispatch(handler, method)
        except HttpError as exc:
            response = Response({"error": exc.error, **({"message": exc.message} if exc.message else {})}, exc.status)
        except (ConnectionError, TimeoutError, ssl.SSLError):
            return
        except Exception:
            log.exception("unhandled error in %s %s", method, handler.path)
            response = Response({"error": "internal_error"}, 500)
        try:
            if isinstance(response, Stream):
                self._stream(handler, response)
            else:
                self.reply(handler, response)
        except (ConnectionError, TimeoutError, ssl.SSLError, OSError):
            pass      # the client went away; nothing to do

    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> "Response | Stream":
        parsed = urllib.parse.urlsplit(handler.path)
        path = urllib.parse.unquote(parsed.path)
        found = self.router.match(method, path)
        if found is None:
            raise HttpError(404, "not_found")
        route, params = found
        headers = {key.lower(): value for key, value in handler.headers.items()}
        request = Request(method=method, path=path, headers=headers, params=params, channel=self.channel,
                          query={k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()})
        self.authenticate(request)
        if request.who not in route.allow and ANYONE not in route.allow:
            raise HttpError(401 if request.who == ANYONE else 403,
                            "unauthorized" if request.who == ANYONE else "forbidden")
        if method in ("POST", "DELETE"):
            length = int(headers.get("content-length") or 0)
            if length > MAX_BODY:
                raise HttpError(413, "too_large")
            raw = handler.rfile.read(length) if length else b""
            if raw:
                try:
                    request.body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise HttpError(400, "bad_json") from None
        result = route.handler(request)
        if isinstance(result, (Response, Stream)):
            return result
        return Response(result if result is not None else {"ok": True})

    def reply(self, handler: BaseHTTPRequestHandler, response: Response) -> None:
        body = response.body
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json_dumps(body).encode("utf-8")
        handler.send_response(response.status)
        handler.send_header("Content-Type", response.content_type)
        handler.send_header("Content-Length", str(len(data)))
        if response.content_type.startswith("application/json"):
            handler.send_header("Cache-Control", "no-store")
        for key, value in {**self.extra_headers, **response.headers}.items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)

    def _stream(self, handler: BaseHTTPRequestHandler, stream: Stream) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        for key, value in self.extra_headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.close_connection = True
        try:
            for name, data in stream.events:
                if name is None:
                    handler.wfile.write(b": keep-alive\n\n")
                else:
                    handler.wfile.write(f"event: {name}\ndata: {json_dumps(data)}\n\n".encode("utf-8"))
                handler.wfile.flush()
        finally:
            close = getattr(stream.events, "close", None)
            if close:
                close()
