"""Linked-device bridge: supervises the optional Node sidecar that connects to
WhatsApp as a companion device (like WhatsApp Web).

Core and the sidecar talk JSON lines over stdin/stdout, so no port is opened.
This transport is opt-in: it relies on an unofficial client library, which
WhatsApp's terms do not permit (see docs/WHATSAPP.md).

The sidecar's source ships inside this package (``sidecar/``). Because a
package folder may be read-only, it is copied to the data folder and its Node
packages are installed there, once, by ``felix connect whatsapp``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix
    from .service import WhatsApp

log = logging.getLogger("felix.whatsapp.bridge")

SOURCE = Path(__file__).resolve().parent / "sidecar"
SIDECAR_FILES = ("index.js", "bridge.js", "package.json")
INSTALLED_MARK = ".installed"
FELIX_PREFIX = "Felix: "


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self, app: "Felix", service: "WhatsApp") -> None:
        self.app = app
        self.service = service
        self.state = "stopped"
        self.qr: str | None = None
        self.pairing_code: str | None = None
        self.me: dict[str, str] = {}
        self.last_error = ""
        self._process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._replies: dict[int, dict[str, Any]] = {}
        self._reply_ready = threading.Condition(self._lock)
        self._next_id = 1
        self._sent_ids: list[str] = []
        self._stopping = False
        self._restarts = 0

    # -- lifecycle -------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.app.config.get("whatsapp.bridge.enabled", False))

    def ready(self) -> bool:
        return self.state == "open" and self._process is not None and self._process.poll() is None

    # -- installation ------------------------------------------------------------
    def prepare(self) -> Path:
        """Copy the sidecar's source next to where its Node packages live. Returns that folder."""
        runtime = self.app.paths.bridge_runtime
        runtime.mkdir(parents=True, exist_ok=True)
        for name in SIDECAR_FILES:
            source, target = SOURCE / name, runtime / name
            if not source.exists():
                raise BridgeError(f"The bridge file is missing from this installation: {source}")
            if not target.exists() or target.read_bytes() != source.read_bytes():
                shutil.copyfile(source, target)
        return runtime

    def installed(self) -> bool:
        """True when the Node packages match the package list this version of Felix ships."""
        runtime = self.app.paths.bridge_runtime
        mark = runtime / INSTALLED_MARK
        return (runtime / "node_modules").is_dir() and mark.exists() and mark.read_text().strip() == _digest(SOURCE / "package.json")

    def install(self, timeout: float = 600) -> tuple[bool, str]:
        """Download the bridge's Node packages (needs the internet once). Returns (ok, what happened)."""
        if not (shutil.which("node") or shutil.which("nodejs")):
            return False, "Node.js is not installed (https://nodejs.org)."
        npm = shutil.which("npm")
        if not npm:
            return False, "npm was not found; it normally comes with Node.js."
        try:
            runtime = self.prepare()
            done = subprocess.run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=runtime,
                                  capture_output=True, text=True, timeout=timeout)
        except BridgeError as exc:
            return False, str(exc)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"npm could not run: {exc}"
        if done.returncode != 0:
            return False, (done.stderr or done.stdout or "npm install failed").strip()[-600:]
        (runtime / INSTALLED_MARK).write_text(_digest(SOURCE / "package.json"))
        return True, "installed"

    def command(self) -> list[str]:
        """The sidecar command line. Tests replace this with a stand-in."""
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise BridgeError("Node.js is not installed; the WhatsApp bridge needs it (https://nodejs.org).")
        runtime = self.prepare()
        if not self.installed():
            raise BridgeError("The WhatsApp bridge is not installed yet (or needs updating). Run: felix connect whatsapp")
        return [node, str(runtime / "index.js")]

    def start(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._stopping = False
            if self.app.sandbox:
                self._set_state("error", error="The demo runs on sample data and cannot link WhatsApp.")
                return
            if self.app.net.mode == "offline":
                self._set_state("stopped", error="Offline mode is on. The WhatsApp link starts again when you leave it.")
                return
            try:
                argv = self.command()
            except BridgeError as exc:
                self._set_state("error", error=str(exc))
                return
            session = self.app.paths.bridge_session
            session.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "FELIX_BRIDGE_SESSION": str(session)}
            try:
                self._process = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=env, text=True, encoding="utf-8", bufsize=1)
            except OSError as exc:
                self._set_state("error", error=f"could not start the bridge: {exc}")
                return
            self._set_state("starting")
            threading.Thread(target=self._read, args=(self._process,), name="felix-wa-bridge", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                self._write(process, {"cmd": "shutdown"})
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        self._set_state("stopped")

    def describe(self) -> dict[str, Any]:
        return {"enabled": self.enabled(), "state": self.state, "qr": self.qr, "pairing_code": self.pairing_code,
                "me": self.me, "error": self.last_error}

    # -- requests ----------------------------------------------------------------
    def send(self, to: str, text: str, timeout: float = 30) -> str:
        reply = self._call({"cmd": "send", "to": to, "text": text}, timeout)
        return str(reply.get("id", ""))

    def send_self(self, text: str, timeout: float = 30) -> str:
        if not self.me.get("jid"):
            raise BridgeError("the bridge does not know the owner's WhatsApp id yet")
        return self.send(self.me["jid"], text, timeout)

    def request_pairing_code(self, phone: str) -> str:
        return str(self._call({"cmd": "pairing_code", "phone": phone}, 30).get("code", ""))

    def logout(self) -> None:
        self._call({"cmd": "logout"}, 20)

    def _call(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise BridgeError("the WhatsApp bridge is not running")
            request_id = self._next_id
            self._next_id += 1
            self._write(process, {**message, "id": request_id})
            deadline = time.time() + timeout
            while request_id not in self._replies:
                remaining = deadline - time.time()
                if remaining <= 0 or process.poll() is not None:
                    raise BridgeError("the WhatsApp bridge did not answer")
                self._reply_ready.wait(min(remaining, 1.0))
            reply = self._replies.pop(request_id)
        if not reply.get("ok"):
            raise BridgeError(str(reply.get("error") or "the bridge refused"))
        return reply

    def _write(self, process: subprocess.Popen, message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    # -- events from the sidecar ---------------------------------------------------
    def _read(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                try:
                    self._dispatch(message)
                except Exception:
                    log.exception("bridge event failed")
        finally:
            code = process.wait()
            with self._lock:
                is_current = self._process is process
                if is_current:
                    self._process = None
                self._reply_ready.notify_all()
            if is_current and not self._stopping:
                if self.state != "logged_out":
                    self._set_state("closed", error=f"bridge exited ({code})")
                    self._restart_later()

    def _restart_later(self) -> None:
        self._restarts += 1
        delay = min(60, 2 ** min(self._restarts, 6))

        def again() -> None:
            time.sleep(delay)
            if not self._stopping and self.enabled():
                self.start()

        threading.Thread(target=again, name="felix-wa-restart", daemon=True).start()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "reply" in message:
            with self._lock:
                # Remember ids we sent before reading on: WhatsApp echoes our own
                # message straight back, and the echo must be recognised as ours.
                if message.get("ok") and message.get("id"):
                    self._sent_ids = (self._sent_ids + [str(message["id"])])[-200:]
                self._replies[int(message["reply"])] = message
                self._reply_ready.notify_all()
            return
        event = message.get("event")
        if event == "state":
            self.qr = message.get("qr")
            self.pairing_code = message.get("pairing_code")
            if message.get("me"):
                self.me = {"jid": str(message["me"].get("jid", "")), "name": str(message["me"].get("name", ""))}
            if message.get("state") == "open":
                self._restarts = 0
            self._set_state(str(message.get("state", "starting")), error=str(message.get("error") or ""))
        elif event == "message":
            self._on_message(message)
        elif event == "contacts":
            for contact in message.get("contacts", [])[:5000]:
                number = "+" + contact["jid"].split("@")[0] if str(contact.get("jid", "")).endswith("@s.whatsapp.net") else ""
                if contact.get("name") and number:
                    self.app.contacts.upsert(contact["name"], phones=[number], source="whatsapp")

    def _on_message(self, message: dict[str, Any]) -> None:
        text = str(message.get("text") or "")
        chat = str(message.get("chat") or "")
        if not text or not chat:
            return
        ts = float(message.get("ts") or time.time())
        message_id = str(message.get("id") or "")
        from_me = bool(message.get("from_me"))
        is_self_chat = bool(self.me.get("jid")) and _same_user(chat, self.me["jid"])
        if from_me:
            with self._lock:
                echoed = message_id in self._sent_ids
            if is_self_chat:
                if not echoed and not text.startswith(FELIX_PREFIX):
                    self._command_from_owner(text)
                return
            if not echoed:      # the owner answered from their phone: note it and clear "needs reply"
                self.service.record_sent("whatsapp", chat, str(message.get("chat_name") or ""), text,
                                         transport="bridge", remote_id=message_id, ts=ts)
            return
        self.service.receive(
            channel="whatsapp", key=chat, chat_name=str(message.get("chat_name") or ""),
            is_group=bool(message.get("is_group")), sender=str(message.get("sender_name") or message.get("chat_name") or ""),
            text=text, ts=ts, transport="bridge", remote_id=message_id, can_reply=True)

    def _command_from_owner(self, text: str) -> None:
        """A message the owner typed in their own 'Message yourself' chat is a command for Felix."""
        if not self.app.config.get("whatsapp.bridge.command_chat", True):
            return
        brain = getattr(self.app, "brain", None)
        if brain is None:
            return

        def work() -> None:
            try:
                answer = brain.respond(text, thread="whatsapp", source="whatsapp")
                self.send_self(FELIX_PREFIX + answer["reply"])
            except Exception:
                log.exception("could not answer a WhatsApp command")

        threading.Thread(target=work, name="felix-wa-command", daemon=True).start()

    def _set_state(self, state: str, error: str = "") -> None:
        changed = state != self.state
        self.state = state
        self.last_error = error
        if state not in ("qr", "pairing"):
            self.qr = None if state != "qr" else self.qr
        if changed:
            log.info("whatsapp bridge: %s %s", state, error)
            self.app.bus.publish("whatsapp.bridge", self.describe())
            if state == "open":
                self.app.outbox.flush_async()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _same_user(a: str, b: str) -> bool:
    return a.split("@")[0].split(":")[0] == b.split("@")[0].split(":")[0]
