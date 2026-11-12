"""The offline brain with language: a model served by Ollama on this laptop.

Optional. If Ollama is running with a tool-capable model (for example
`ollama pull llama3.1`), Felix can hold a free-form conversation and use its
tools with no internet at all. Without it, the command engine takes over.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

from ..tools import ToolContext
from .claude import EngineError
from .prompts import LOCAL_SYSTEM

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.brain.local")

Emit = Callable[[dict[str, Any]], None]
MAX_TOOL_RESULT_CHARS = 8000       # small models have small context windows
CHECK_EVERY = 20.0                 # seconds between looks for a local model
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _on_this_machine(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalEngine:
    name = "local"

    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._checked_at = 0.0
        self._up = False
        self._lock = threading.Lock()
        self._looking = False

    def config(self) -> dict[str, Any]:
        return self.app.config.get("brain.local", {}) or {}

    def url(self) -> str:
        return str(self.config().get("url", "http://127.0.0.1:11434")).rstrip("/")

    def _open(self, request: Any, timeout: float):
        """Open a request to Ollama. On this machine it is reached directly, whatever
        proxy the system is set up to use: a proxy cannot reach this laptop's own ports."""
        if _on_this_machine(urllib.parse.urlsplit(self.url()).hostname or ""):
            return _DIRECT.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

    def available(self) -> bool:
        """True when Ollama answers on this machine with the chosen model.

        Looked up at most every 20 seconds. Only the first look is waited for.
        After that the last answer is returned and the next look runs in the
        background, because finding nothing there can take the whole timeout
        (Windows does not refuse a closed port straight away) and no request
        should have to wait for it.
        """
        if not self.config().get("enabled", True):
            return False
        with self._lock:
            if time.monotonic() - self._checked_at < CHECK_EVERY:
                return self._up
            if self._checked_at == 0.0:
                self._up = self._answers()
                self._checked_at = time.monotonic()
                return self._up
            self._checked_at = time.monotonic()
            if not self._looking:
                self._looking = True
                threading.Thread(target=self._look_again, name="felix-local-model", daemon=True).start()
            return self._up

    def _look_again(self) -> None:
        try:
            self._up = self._answers()
        finally:
            self._looking = False

    def _answers(self) -> bool:
        try:
            with self._open(self.url() + "/api/tags", timeout=0.6) as response:
                models = json.loads(response.read().decode("utf-8")).get("models", [])
        except (urllib.error.URLError, OSError, ValueError):
            return False
        wanted = str(self.config().get("model", "")).split(":")[0]
        return any(str(m.get("name", "")).split(":")[0] == wanted for m in models) if wanted else bool(models)

    def _tool_specs(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description.split(". ")[0] + ".", "parameters": tool.schema,
        }} for tool in self.app.tools.all()]

    def run(self, history: list[dict[str, Any]], ctx: ToolContext, emit: Emit) -> dict[str, Any]:
        """Same contract as ClaudeEngine.run, over Ollama's /api/chat."""
        produced: list[dict[str, Any]] = []
        texts: list[str] = []
        limit = int(self.app.config.get("brain.max_iterations", 12))
        for _ in range(limit):
            message = self._chat([{"role": "system", "content": LOCAL_SYSTEM}] + history + produced, emit, bool(texts))
            produced.append(message)
            if message.get("content", "").strip():
                texts.append(message["content"].strip())
            calls = message.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name", ""))
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                emit({"type": "tool", "name": name, "status": "start"})
                result = self.app.tools.execute(name, arguments if isinstance(arguments, dict) else {}, ctx)
                emit({"type": "tool", "name": name, "status": "end", "ok": result.ok, "pending": bool(result.pending)})
                if result.pending:
                    emit({"type": "approval", "id": result.pending["id"], "tool": name,
                          "preview": result.pending.get("preview"), "risk": result.pending.get("risk")})
                produced.append({"role": "tool", "tool_name": name,
                                 "content": result.for_model()[:MAX_TOOL_RESULT_CHARS]})
        return {"text": "\n\n".join(texts) or "(no reply)", "messages": produced, "input_tokens": 0,
                "stop": "end_turn", "model": self.config().get("model", "local")}

    def _chat(self, messages: list[dict[str, Any]], emit: Emit, separate: bool) -> dict[str, Any]:
        body = json.dumps({
            "model": self.config().get("model", "llama3.1"), "messages": messages, "tools": self._tool_specs(),
            "stream": True, "options": {"temperature": 0.2},
        }).encode("utf-8")
        request = urllib.request.Request(self.url() + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
        content: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        try:
            with self._open(request, timeout=float(self.config().get("timeout", 120))) as response:
                for raw in response:
                    line = raw.strip()
                    if not line:
                        continue
                    chunk = json.loads(line.decode("utf-8"))
                    if chunk.get("error"):
                        raise EngineError(f"The local model failed: {chunk['error']}", "unavailable")
                    part = chunk.get("message") or {}
                    if part.get("content"):
                        if separate and not content:
                            emit({"type": "delta", "text": "\n\n"})
                        content.append(part["content"])
                        emit({"type": "delta", "text": part["content"]})
                    tool_calls += part.get("tool_calls") or []
                    if chunk.get("done"):
                        break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            self._checked_at = 0.0
            raise EngineError(f"The local model refused the request ({exc.code}): {detail}", "unavailable") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._checked_at = 0.0
            raise EngineError(f"The local model is not answering ({getattr(exc, 'reason', exc)}).", "unavailable") from exc
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message
