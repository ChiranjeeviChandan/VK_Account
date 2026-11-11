"""In-process event bus. Powers live dashboard updates and reconnect triggers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger("felix.bus")

Handler = Callable[[str, dict[str, Any]], None]


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: list[tuple[str, Handler]] = []
        self._queues: list[queue.Queue] = []

    def on(self, prefix: str, handler: Handler) -> None:
        """Call ``handler(topic, data)`` for topics starting with ``prefix`` ('' = all)."""
        with self._lock:
            self._handlers.append((prefix, handler))

    def publish(self, topic: str, data: dict[str, Any] | None = None) -> None:
        payload = dict(data or {})
        with self._lock:
            handlers = [h for p, h in self._handlers if topic.startswith(p)]
            queues = list(self._queues)
        for handler in handlers:
            try:
                handler(topic, payload)
            except Exception:  # a broken listener must never break the publisher
                log.exception("bus handler failed for %s", topic)
        event = {"topic": topic, "ts": time.time(), "data": payload}
        for q in queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow dashboard tab: drop rather than block Felix

    def open_stream(self, maxsize: int = 200) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._queues.append(q)
        return q

    def close_stream(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)
