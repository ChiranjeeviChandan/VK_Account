"""A scripted stand-in for the Claude Messages API (streaming, tool use).

The real `anthropic` SDK talks to it over HTTP, so Felix's online engine is
exercised through the same client code it uses in production. Each test
queues the replies the "model" should give; the server records every request
so the test can check what Felix actually sent.

It is a test double. It does not validate requests the way the real API does.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(name: str, arguments: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id or f"toolu_{name}", "name": name, "input": arguments}


def thinking(signature: str = "sig-1") -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": signature}


class FakeClaude:
    def __init__(self) -> None:
        self.script: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.counter = 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.requests.append(body)
                fake.headers.append({k.lower(): v for k, v in self.headers.items()})
                step = fake.script.pop(0) if fake.script else {"content": [text("(script exhausted)")]}
                if "error" in step:
                    data = json.dumps({"type": "error", "error": {"type": step["error"], "message": step["message"]}}).encode()
                    self.send_response(step["status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                payload = fake.render(step, body.get("model", "claude-opus-5-5"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("request-id", f"req_{fake.counter}")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    # -- scripting ---------------------------------------------------------------
    def reply(self, *content: dict[str, Any], stop: str | None = None, model: str | None = None) -> "FakeClaude":
        blocks = list(content)
        if stop is None:
            stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        self.script.append({"content": blocks, "stop": stop, "model": model})
        return self

    def fail(self, status: int, kind: str, message: str) -> "FakeClaude":
        self.script.append({"error": kind, "status": status, "message": message})
        return self

    # -- wire format ---------------------------------------------------------------
    def render(self, step: dict[str, Any], model: str) -> bytes:
        self.counter += 1
        events: list[tuple[str, dict[str, Any]]] = [("message_start", {"type": "message_start", "message": {
            "id": f"msg_{self.counter}", "type": "message", "role": "assistant", "model": step.get("model") or model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 800,
                      "cache_creation_input_tokens": 0}}})]
        for index, block in enumerate(step["content"]):
            if block["type"] == "text":
                events.append(("content_block_start", {"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "text", "text": ""}}))
                words = block["text"].split(" ")
                for position, word in enumerate(words):       # several deltas, like real streaming
                    piece = word + (" " if position < len(words) - 1 else "")
                    events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                                                           "delta": {"type": "text_delta", "text": piece}}))
            elif block["type"] == "tool_use":
                events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                    "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}))
                raw = json.dumps(block["input"])
                half = len(raw) // 2
                for piece in (raw[:half], raw[half:]):
                    events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                                                           "delta": {"type": "input_json_delta", "partial_json": piece}}))
            elif block["type"] == "thinking":
                events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                    "type": "thinking", "thinking": "", "signature": ""}}))
                events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                    "type": "signature_delta", "signature": block["signature"]}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
        events.append(("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": step["stop"], "stop_sequence": None},
                                         "usage": {"output_tokens": 40}}))
        events.append(("message_stop", {"type": "message_stop"}))
        return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode("utf-8")

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
