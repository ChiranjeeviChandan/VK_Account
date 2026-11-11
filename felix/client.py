"""A small client for a running Felix Core, used by the CLI and the desktop overlay."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

from .config import Config
from .paths import Paths
from .secrets import SecretStore


class CoreUnavailable(Exception):
    """Felix Core is not running (or not reachable) on this machine."""


class CoreClient:
    def __init__(self, home: str | None = None, port: int | None = None, token: str | None = None) -> None:
        paths = Paths(home)
        config = Config(paths.config) if paths.config.exists() else Config()
        self.port = port or int(config.get("server.port", 8765))
        self.token = token or SecretStore(paths.secrets).get("ui_token") or ""
        self.base = f"http://127.0.0.1:{self.port}"
        # Talk to the laptop directly even when a system proxy is configured.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                message = parsed.get("message") or parsed.get("error") or detail
            except ValueError:
                message = detail
            if exc.code == 401:
                raise CoreUnavailable("Felix is running but this terminal is not signed in to it.") from exc
            raise RuntimeError(message) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CoreUnavailable(f"Felix is not running on port {self.port}. Start it with: felix start") from exc

    def get(self, path: str, timeout: float = 30) -> Any:
        with self._request("GET", path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, body: dict[str, Any] | None = None, timeout: float = 120) -> Any:
        with self._request("POST", path, body or {}, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def running(self) -> bool:
        try:
            self.get("/api/status", timeout=2)
            return True
        except (CoreUnavailable, RuntimeError):
            return False

    def stream(self, path: str, body: dict[str, Any], timeout: float = 600) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield (event, data) pairs from a server-sent-event endpoint."""
        with self._request("POST", path, body, timeout=timeout) as response:
            event = ""
            for raw in response:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    yield event, json.loads(line[6:])
                    event = ""

    def chat(self, text: str, thread: str = "cli", on_delta: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Ask Felix something; ``on_delta`` receives the reply as it is written."""
        result: dict[str, Any] = {}
        for event, data in self.stream("/api/chat/stream", {"text": text, "thread": thread}):
            if event == "delta" and on_delta:
                on_delta(data.get("text", ""))
            elif event == "done":
                result = data
            elif event == "error":
                raise RuntimeError(data.get("message", "Felix failed"))
        return result
