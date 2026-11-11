"""Connectivity: is Felix allowed to use the network, and is it reachable?

Three modes:
  auto     - probe and follow reality (default)
  offline  - never touch the network, even if a connection exists
  online   - assume connected (for networks where probes are blocked)
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable

log = logging.getLogger("felix.net")

DEFAULT_TCP_TARGETS: tuple[tuple[str, int], ...] = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 53))
DEFAULT_HTTP_TARGETS: tuple[str, ...] = ("https://api.anthropic.com/", "https://www.gstatic.com/generate_204")


def tcp_probe(targets: Iterable[tuple[str, int]] = DEFAULT_TCP_TARGETS, timeout: float = 1.5) -> bool:
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def http_probe(urls: Iterable[str] = DEFAULT_HTTP_TARGETS, timeout: float = 3.0) -> bool:
    """Any HTTP answer at all - even an error status - proves a route out.

    Goes through the system proxy settings, so it also works on networks that
    only allow web traffic via a proxy.
    """
    for url in urls:
        try:
            request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "felix-assistant"})
            with urllib.request.urlopen(request, timeout=timeout):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            continue
    return False


def default_probe() -> bool:
    if tcp_probe():
        return True
    proxied = any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"))
    return http_probe() if proxied else http_probe(DEFAULT_HTTP_TARGETS[:1], timeout=2.0)


class Connectivity:
    def __init__(
        self,
        mode: str = "auto",
        probe: Callable[[], bool] | None = None,
        ttl: float = 10.0,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._mode = mode if mode in ("auto", "online", "offline") else "auto"
        self._probe = probe or default_probe
        self._ttl = ttl
        self._on_change = on_change
        self._lock = threading.Lock()
        self._reachable: bool | None = None
        self._checked_at = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "online", "offline"):
            raise ValueError("mode must be auto, online or offline")
        before = self.online
        self._mode = mode
        self._checked_at = 0.0
        after = self.online
        if before != after:
            self._notify(after)

    @property
    def online(self) -> bool:
        """True when Felix may use the network right now."""
        if self._mode == "offline":
            return False
        if self._mode == "online":
            return True
        return self.reachable()

    def reachable(self, force: bool = False) -> bool:
        with self._lock:
            fresh = time.monotonic() - self._checked_at < self._ttl
            if not force and fresh and self._reachable is not None:
                return self._reachable
        try:
            result = bool(self._probe())
        except Exception:
            result = False
        with self._lock:
            previous = self._reachable
            self._reachable = result
            self._checked_at = time.monotonic()
        if previous is not None and previous != result and self._mode == "auto":
            self._notify(result)
        return result

    def describe(self) -> dict:
        return {
            "mode": self._mode,
            "online": self.online,
            "reachable": self._reachable if self._mode != "offline" else None,
        }

    def _notify(self, state: bool) -> None:
        log.info("connectivity changed: %s", "online" if state else "offline")
        if self._on_change:
            try:
                self._on_change(state)
            except Exception:
                log.exception("connectivity listener failed")

    # -- background monitor ----------------------------------------------
    def start(self, interval: float = 15.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval):
                if self._mode == "auto":
                    self.reachable(force=True)

        self._thread = threading.Thread(target=loop, name="felix-net", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
