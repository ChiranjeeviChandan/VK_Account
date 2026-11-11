"""A stand-in for the Android companion that drives the hub's Python API."""

from __future__ import annotations

import threading
import time
import uuid


class PhoneFailure(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code, self.message = code, message


class SimPhone:
    def __init__(self, app, name: str = "Pixel 8") -> None:
        self.app, self.name = app, name
        self.device = None
        self.token = None
        self.calls: list[tuple[str, dict]] = []
        self.handlers = {
            "ping": lambda a: {"pong": True},
            "status": lambda a: {"battery": 81, "charging": False, "network": "wifi", "ringer": "normal", "dnd": False},
        }
        self.failures: dict[str, PhoneFailure] = {}
        self._stop = threading.Event()
        self._thread = None

    def pair(self) -> "SimPhone":
        pairing = self.app.phone.new_pairing()
        answer = self.app.phone.pair({"code": pairing["code"], "device_id": f"dev-{uuid.uuid4().hex[:8]}",
                                      "name": self.name, "model": "Google Pixel 8", "os": "Android 15",
                                      "app_version": "0.1.0"})
        self.token = answer["token"]
        self.device = self.app.phone.authenticate(self.token)
        return self

    def start(self) -> "SimPhone":
        if self.device is None:
            self.pair()
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                commands = self.app.phone.poll(self.device, wait=0.05)
                results = []
                for command in commands:
                    self.calls.append((command["type"], command["args"]))
                    failure = self.failures.get(command["type"])
                    if failure is not None:
                        results.append({"id": command["id"], "ok": False, "error": failure.code, "message": failure.message})
                        continue
                    handler = self.handlers.get(command["type"], lambda a: {})
                    try:
                        results.append({"id": command["id"], "ok": True, "data": handler(command["args"]) or {}})
                    except PhoneFailure as exc:
                        results.append({"id": command["id"], "ok": False, "error": exc.code, "message": exc.message})
                if results:
                    self.app.phone.results(self.device, results)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        deadline = time.time() + 2
        while not self.app.phone.connected(self.device["id"]) and time.time() < deadline:
            time.sleep(0.005)
        return self

    def stop(self) -> None:
        """Go out of reach (flight mode, app killed)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self.app.phone._lock:
            self.app.phone._last_poll.pop(self.device["id"], None)

    def emit(self, kind: str, data: dict, event_id: str | None = None) -> list[str]:
        event = {"id": event_id or uuid.uuid4().hex, "type": kind, "ts": int(self.app.clock.ts() * 1000), "data": data}
        return self.app.phone.ingest(self.device, [event])

    def message(self, chat: str, text: str, sender: str | None = None, key: str | None = None, group: bool = False,
                can_reply: bool = True, minutes_ago: float = 0, channel: str = "whatsapp") -> None:
        self.emit("message", {
            "channel": channel, "package": "com.whatsapp", "chat_key": key or f"com.whatsapp|{chat}",
            "chat_name": chat, "is_group": group, "sender": sender or chat, "text": text,
            "msg_ts": int((self.app.clock.ts() - minutes_ago * 60) * 1000), "can_reply": can_reply,
            "notification_key": f"0|com.whatsapp|1|{chat}|10123"})

    def commands(self, kind: str) -> list[dict]:
        return [args for name, args in self.calls if name == kind]
