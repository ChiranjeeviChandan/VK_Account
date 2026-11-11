"""The offline brain with language: a model served by Ollama on this laptop.

Optional. If Ollama is running with a tool-capable model (for example
`ollama pull llama3.1`), Felix can hold a free-form conversation and use its
tools with no internet at all. Without it, the command engine takes over.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
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


class LocalEngine:
    name = "local"

    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._checked_at = 0.0
        self._up = False

    def config(self) -> dict[str, Any]:
        return self.app.config.get("brain.local", {}) or {}

    def url(self) -> str:
        return str(self.config().get("url", "http://127.0.0.1:11434")).rstrip("/")

    def available(self) -> bool:
        """True when Ollama answers on this machine (checked at most every 20 s)."""
        if not self.config().get("enabled", True):
            return False
        if time.monotonic() - self._checked_at < 20:
            return self._up
        self._checked_at = time.monotonic()
        try:
            with urllib.request.urlopen(self.url() + "/api/tags", timeout=0.6) as response:
                models = json.loads(response.read().decode("utf-8")).get("models", [])
            wanted = str(self.config().get("model", "")).split(":")[0]
            self._up = any(str(m.get("name", "")).split(":")[0] == wanted for m in models) if wanted else bool(models)
        except (urllib.error.URLError, OSError, ValueError):
            self._up = False
        return self._up

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
            with urllib.request.urlopen(request, timeout=float(self.config().get("timeout", 120))) as response:
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
