"""Phone hub: pairing, event intake and the command queue for paired phones.

Implements Core's side of docs/PHONE_PROTOCOL.md. The phone always calls in;
Core answers long-polls with queued commands and stores what the phone reports.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as pysecrets
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from ..contacts import norm_phone
from ..outbox import Retry
from ..tls import TlsUnavailable, ensure_certificate, local_addresses
from ..tools import DANGER, LOCAL, OUTBOUND, READ, Registry, ToolContext, ToolError
from ..util import clip, squash

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.phone")

PAIRING_TTL = 600
POLL_WAIT = 25
MAX_PAIR_FAILURES = 8
RESEND_AFTER = 20          # seconds before an unanswered command is offered again
CHAT_CHANNELS = {"whatsapp", "whatsapp_business", "sms", "telegram", "teams", "slack", "signal", "other"}

CONTROL_ACTIONS = {
    # action -> (command type, builder)
    "torch_on": ("torch", lambda a: {"on": True}),
    "torch_off": ("torch", lambda a: {"on": False}),
    "volume": ("volume", lambda a: {"stream": a.get("stream", "media"), "level": _level(a)}),
    "ringer": ("ringer", lambda a: {"mode": _choice(a, "mode", ("normal", "vibrate", "silent"))}),
    "dnd_on": ("dnd", lambda a: {"on": True}),
    "dnd_off": ("dnd", lambda a: {"on": False}),
    "brightness": ("brightness", lambda a: {"level": _level(a)}),
    "alarm": ("alarm.set", lambda a: {"hour": _need(a, "hour"), "minute": a.get("minute", 0), "label": a.get("label", "")}),
    "timer": ("timer.set", lambda a: {"seconds": _need(a, "seconds"), "label": a.get("label", "")}),
    "open_app": ("app.open", lambda a: {"name": _need(a, "name")}),
    "open_url": ("url.open", lambda a: {"url": _need(a, "url")}),
    "media": ("media", lambda a: {"action": _choice(a, "mode", ("play_pause", "next", "previous", "stop"))}),
    "speak": ("tts", lambda a: {"text": _need(a, "text")}),
    "ring": ("locate", lambda a: {"seconds": a.get("seconds", 30)}),
    "notify": ("notify", lambda a: {"title": a.get("label") or "Felix", "text": _need(a, "text")}),
    "clipboard_set": ("clipboard.set", lambda a: {"text": _need(a, "text")}),
    "location": ("location", lambda a: {}),
}
UI_ACTIONS = ("back", "home", "recents", "notifications", "quick_settings", "lock", "screenshot",
              "click", "type", "scroll", "read_screen")


def _need(args: dict[str, Any], key: str) -> Any:
    if args.get(key) in (None, ""):
        raise ToolError(f"This action needs `{key}`.")
    return args[key]


def _level(args: dict[str, Any]) -> int:
    level = _need(args, "level")
    return max(0, min(100, int(level)))


def _choice(args: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = str(_need(args, key)).lower()
    if value not in allowed:
        raise ToolError(f"`{key}` must be one of {', '.join(allowed)}.")
    return value


class PhoneHub:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._pairing: dict[str, float] = {}         # code -> expiry
        self._pair_failures: list[float] = []
        self._last_poll: dict[str, float] = {}
        app.outbox.handler("sms", self._deliver_sms)

    # -- pairing -------------------------------------------------------------
    def new_pairing(self) -> dict[str, Any]:
        """Create a one-time code and the deep link / QR that carries it."""
        config = self.app.config.get("phone", {})
        code = f"{pysecrets.randbelow(10**6):06d}"
        with self._lock:
            now = time.time()
            self._pairing = {c: exp for c, exp in self._pairing.items() if exp > now}
            self._pairing[code] = now + PAIRING_TTL
        scheme, pin = "http", ""
        if config.get("tls", True):
            try:
                _, _, pin = ensure_certificate(self.app.paths.tls)
                scheme = "https"
            except TlsUnavailable as exc:
                raise ToolError(str(exc)) from exc
        hosts = list(dict.fromkeys([*(config.get("advertise_hosts") or []), *local_addresses()]))
        name = self.app.config.get("owner.name") or "Felix"
        query = {"v": "1", "s": scheme, "h": ",".join(hosts), "p": str(config.get("port", 8766)), "c": code,
                 "n": f"{name}'s Felix" if name != "Felix" else "Felix"}
        if pin:
            query["fp"] = pin
        link = "felix://pair?" + urllib.parse.urlencode(query, safe=",")
        return {"code": code, "expires_in": PAIRING_TTL, "link": link, "hosts": hosts,
                "port": config.get("port", 8766), "scheme": scheme, "fingerprint": pin, "qr_svg": _qr(link)}

    def pair(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """Exchange a pairing code for a device token. None = refused."""
        code = str(body.get("code", "")).strip()
        device_id = str(body.get("device_id", "")).strip()[:80]
        now = time.time()
        with self._lock:
            self._pair_failures = [t for t in self._pair_failures if now - t < PAIRING_TTL]
            if len(self._pair_failures) >= MAX_PAIR_FAILURES:
                raise PermissionError("too many attempts")
            expiry = self._pairing.get(code)
            valid = bool(code and device_id and expiry and expiry > now)
            if not valid:
                self._pair_failures.append(now)
                self.app.store.audit("phone", "pair", {"device_id": device_id}, "refused")
                return None
            del self._pairing[code]           # single use
        token = pysecrets.token_urlsafe(32)
        self.app.store.insert("devices", {
            "id": device_id, "name": clip(str(body.get("name") or body.get("model") or "Android phone"), 60),
            "token_hash": _hash(token), "model": clip(str(body.get("model", "")), 80),
            "os": clip(str(body.get("os", "")), 40), "app_version": clip(str(body.get("app_version", "")), 20),
            "paired_at": now, "last_seen": now, "state": {}, "capabilities": {}, "revoked": 0,
        }, replace=True)
        self.app.store.audit("owner", "pair", {"device_id": device_id, "name": body.get("name")})
        self.app.bus.publish("phone.paired", {"device_id": device_id})
        return {"token": token, "device_id": device_id, "poll_wait": POLL_WAIT,
                "core": {"name": self.app.config.get("owner.name") or "Felix", "version": self.app.version}}

    def authenticate(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        digest = _hash(token)
        for device in self.app.store.query("SELECT * FROM devices WHERE revoked = 0"):
            if hmac.compare_digest(device["token_hash"], digest):
                return device
        return None

    def devices(self) -> list[dict[str, Any]]:
        out = []
        for device in self.app.store.query("SELECT * FROM devices WHERE revoked = 0 ORDER BY paired_at"):
            item = {k: device[k] for k in ("id", "name", "model", "os", "app_version", "paired_at", "last_seen", "state")}
            item["connected"] = self.connected(device["id"])
            out.append(item)
        return out

    def revoke(self, device_id: str) -> bool:
        changed = self.app.store.update("devices", {"revoked": 1}, "id = ?", (device_id,))
        self.app.store.update("phone_commands", {"status": "expired"}, "device_id = ? AND status IN ('queued', 'sent')",
                              (device_id,))
        with self._wake:
            self._last_poll.pop(device_id, None)
            self._wake.notify_all()
        if changed:
            self.app.store.audit("owner", "unpair", {"device_id": device_id})
        return bool(changed)

    # -- connection state -----------------------------------------------------------
    def connected(self, device_id: str | None = None) -> bool:
        """True while a paired phone is actively long-polling."""
        now = time.time()
        with self._lock:
            if device_id:
                return now - self._last_poll.get(device_id, 0) < POLL_WAIT + 15
            return any(now - seen < POLL_WAIT + 15 for seen in self._last_poll.values())

    def primary(self) -> str | None:
        """The device commands go to: the connected one seen most recently."""
        with self._lock:
            live = [(seen, device_id) for device_id, seen in self._last_poll.items()
                    if time.time() - seen < POLL_WAIT + 15]
        if live:
            return max(live)[1]
        row = self.app.store.one("SELECT id FROM devices WHERE revoked = 0 ORDER BY last_seen DESC LIMIT 1")
        return row["id"] if row else None

    def status(self) -> dict[str, Any]:
        devices = self.devices()
        return {"paired": bool(devices), "connected": self.connected(), "devices": devices}

    def _touch(self, device_id: str) -> None:
        was = self.connected(device_id)
        with self._lock:
            self._last_poll[device_id] = time.time()
        self.app.store.update("devices", {"last_seen": time.time()}, "id = ?", (device_id,))
        if not was:
            self.app.bus.publish("phone.connected", {"device_id": device_id})

    # -- commands ---------------------------------------------------------------------
    def send(self, kind: str, args: dict[str, Any] | None = None, expires_in: float = 120,
             device_id: str | None = None) -> int:
        """Queue a command without waiting for its result."""
        device_id = device_id or self.primary()
        if not device_id:
            raise ToolError("No phone is paired with Felix yet. Open the dashboard and choose Pair phone.")
        now = time.time()
        with self._wake:
            command_id = self.app.store.insert("phone_commands", {
                "device_id": device_id, "type": kind, "args": args or {}, "status": "queued",
                "created_at": now, "expires_at": now + expires_in,
            })
            self._wake.notify_all()
        return command_id

    def request(self, kind: str, args: dict[str, Any] | None = None, timeout: float = 20,
                device_id: str | None = None) -> dict[str, Any]:
        """Run a command on the phone and wait for the answer."""
        device_id = device_id or self.primary()
        if not device_id:
            raise ToolError("No phone is paired with Felix yet. Open the dashboard and choose Pair phone.")
        if not self.connected(device_id):
            raise ToolError("Your phone is not reachable right now (is the Felix app running and on the same network?).")
        command_id = self.send(kind, args, expires_in=timeout + 5, device_id=device_id)
        deadline = time.time() + timeout
        with self._wake:
            while True:
                row = self.app.store.one("SELECT status, result FROM phone_commands WHERE id = ?", (command_id,))
                if row and row["status"] in ("done", "failed"):
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.app.store.update("phone_commands", {"status": "expired"}, "id = ?", (command_id,))
                    raise ToolError("The phone did not answer in time.")
                self._wake.wait(min(remaining, 1.0))
        result = row["result"] if isinstance(row["result"], dict) else {}
        if row["status"] == "failed":
            raise ToolError(_explain(kind, result))
        return result.get("data") or {}

    def poll(self, device: dict[str, Any], wait: float = POLL_WAIT) -> list[dict[str, Any]]:
        """Long-poll: return queued commands for this phone, or [] after ``wait``."""
        device_id = device["id"]
        self._touch(device_id)
        deadline = time.time() + max(0.0, min(wait, 55))
        with self._wake:
            while True:
                commands = self._take(device_id)
                if commands:
                    return commands
                remaining = deadline - time.time()
                if remaining <= 0 or self.app.store.scalar("SELECT revoked FROM devices WHERE id = ?", (device_id,)):
                    return []
                self._wake.wait(min(remaining, 5.0))
                self._last_poll[device_id] = time.time()

    def _take(self, device_id: str) -> list[dict[str, Any]]:
        now = time.time()
        self.app.store.update("phone_commands", {"status": "expired"},
                              "device_id = ? AND status IN ('queued', 'sent') AND expires_at < ?", (device_id, now))
        rows = self.app.store.query(
            "SELECT * FROM phone_commands WHERE device_id = ? AND (status = 'queued' OR (status = 'sent' AND sent_at < ?)) "
            "ORDER BY id LIMIT 20", (device_id, now - RESEND_AFTER))
        for row in rows:
            self.app.store.update("phone_commands", {"status": "sent", "sent_at": now}, "id = ?", (row["id"],))
        return [{"id": r["id"], "type": r["type"], "args": r["args"] or {}, "expires": int(r["expires_at"] * 1000)}
                for r in rows]

    def results(self, device: dict[str, Any], results: list[dict[str, Any]]) -> None:
        with self._wake:
            for item in results or []:
                try:
                    command_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                row = self.app.store.one("SELECT * FROM phone_commands WHERE id = ? AND device_id = ?",
                                         (command_id, device["id"]))
                if row is None or row["status"] in ("done", "failed"):
                    continue
                ok = bool(item.get("ok"))
                self.app.store.update("phone_commands", {
                    "status": "done" if ok else "failed", "done_at": time.time(),
                    "result": {"data": item.get("data") or {}, "error": item.get("error"), "message": item.get("message")},
                }, "id = ?", (command_id,))
                self.app.bus.publish("phone.result", {"id": command_id, "type": row["type"], "ok": ok,
                                                      "error": item.get("error")})
            self._wake.notify_all()

    # -- events -----------------------------------------------------------------------
    def ingest(self, device: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
        """Store what the phone reports. Returns the ids that are now safe to delete on the phone."""
        accepted: list[str] = []
        for event in (events or [])[:100]:
            event_id = str(event.get("id", ""))[:80]
            if not event_id:
                continue
            if self.app.store.scalar("SELECT 1 FROM phone_seen WHERE id = ?", (event_id,)):
                accepted.append(event_id)        # a repeat: acknowledge, do nothing
                continue
            try:
                self._handle(device, str(event.get("type", "")), event.get("data") or {}, event.get("ts"))
            except Exception:
                log.exception("could not process phone event %s", event.get("type"))
                continue                          # not acknowledged: the phone will retry
            self.app.store.insert("phone_seen", {"id": event_id, "ts": time.time()}, ignore=True)
            accepted.append(event_id)
        self.app.store.update("devices", {"last_seen": time.time()}, "id = ?", (device["id"],))
        self.app.store.delete("phone_seen", "ts < ?", (time.time() - 14 * 86400,))
        return accepted

    def _handle(self, device: dict[str, Any], kind: str, data: dict[str, Any], ts_ms: Any) -> None:
        ts = _seconds(ts_ms) or time.time()
        if kind == "message":
            channel = data.get("channel") if data.get("channel") in CHAT_CHANNELS else "other"
            self.app.whatsapp.receive(
                channel="whatsapp" if channel in ("whatsapp", "whatsapp_business") else channel,
                key=str(data.get("chat_key") or data.get("chat_name") or "unknown"),
                chat_name=squash(str(data.get("chat_name") or "")), is_group=bool(data.get("is_group")),
                sender=squash(str(data.get("sender") or data.get("chat_name") or "")),
                text=str(data.get("text") or ""), ts=_seconds(data.get("msg_ts")) or ts, transport="phone",
                can_reply=bool(data.get("can_reply")))
        elif kind == "sms":
            number = norm_phone(str(data.get("from") or "")) or str(data.get("from") or "unknown")
            name = self.app.contacts.name_for(number, fallback=number)
            self.app.whatsapp.receive(channel="sms", key=f"sms|{number}", chat_name=name, is_group=False, sender=name,
                                      text=str(data.get("text") or ""), ts=_seconds(data.get("msg_ts")) or ts,
                                      transport="phone", address=number, can_reply=True)
        elif kind == "notification":
            self._notification(device, data, ts)
        elif kind == "notification_removed":
            self.app.store.update("notifications", {"removed": 1}, "device_id = ? AND key = ?",
                                  (device["id"], str(data.get("key", ""))))
        elif kind == "call":
            state = str(data.get("state", ""))
            number = norm_phone(str(data.get("number") or ""))
            name = squash(str(data.get("name") or "")) or self.app.contacts.name_for(number, fallback=number or "Unknown")
            if state in ("missed", "ringing"):
                self.app.store.insert("notifications", {
                    "device_id": device["id"], "package": "call", "app": "Phone", "key": f"call|{number}|{int(ts)}",
                    "title": f"{'Missed call' if state == 'missed' else 'Incoming call'} from {name}",
                    "text": number, "ts": ts, "category": state}, ignore=True)
                self.app.bus.publish("phone.call", {"state": state, "name": name, "number": number})
        elif kind == "status":
            self.app.store.update("devices", {"state": {**data, "at": ts}}, "id = ?", (device["id"],))
            self.app.bus.publish("phone.status", {"device_id": device["id"], **data})

    def _notification(self, device: dict[str, Any], data: dict[str, Any], ts: float) -> None:
        if data.get("ongoing"):
            return
        title = squash(str(data.get("title") or ""))
        text = squash(str(data.get("big_text") or data.get("text") or ""))
        if not title and not text:
            return
        post_ts = _seconds(data.get("post_ts")) or ts
        row_id = self.app.store.insert("notifications", {
            "device_id": device["id"], "package": str(data.get("package", ""))[:120],
            "app": squash(str(data.get("app") or data.get("package") or ""))[:60], "key": str(data.get("key", ""))[:200],
            "title": clip(title, 200), "text": clip(text, 1000), "ts": post_ts,
            "category": str(data.get("category") or "")[:40]}, ignore=True)
        if row_id:
            # Alerts from monitoring or banking apps can be the first sign something broke.
            self.app.pulse.ingest("notification", row_id, post_ts, text, title=title,
                                  who=squash(str(data.get("app") or "")), who_addr=str(data.get("package", "")),
                                  source="phone", automated=True, topic=f"app:{data.get('package', '')}:{title[:40]}")

    def recent_notifications(self, limit: int = 30, app: str | None = None, hours: float = 24) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notifications WHERE ts >= ?"
        params: list[Any] = [self.app.clock.ts() - hours * 3600]
        if app:
            sql += " AND (lower(app) LIKE ? OR lower(package) LIKE ?)"
            params += [f"%{app.lower()}%", f"%{app.lower()}%"]
        return self.app.store.query(sql + " ORDER BY ts DESC LIMIT ?", [*params, limit])

    def missed_calls(self, hours: float = 24) -> list[dict[str, Any]]:
        return self.app.store.query(
            "SELECT * FROM notifications WHERE package = 'call' AND category = 'missed' AND ts >= ? ORDER BY ts DESC",
            (self.app.clock.ts() - hours * 3600,))

    # -- sms through the outbox ---------------------------------------------------------
    def _deliver_sms(self, payload: dict[str, Any]) -> None:
        if not self.connected():
            raise Retry("phone not connected")
        self.request("sms.send", {"to": payload["to"], "text": payload["text"]}, timeout=30)
        self.app.whatsapp.record_sent("sms", f"sms|{payload['to']}", payload.get("name") or payload["to"],
                                      payload["text"], transport="phone", address=payload["to"])

    def sync_contacts(self) -> int:
        data = self.request("contacts.dump", {"limit": 5000}, timeout=60)
        count = 0
        for person in data.get("contacts", []):
            if self.app.contacts.upsert(person.get("name", ""), person.get("emails"), person.get("phones"), source="phone"):
                count += 1
        return count

    # -- tools --------------------------------------------------------------------------
    def register(self, tools: Registry) -> None:
        @tools.tool(
            "phone_status",
            "Report the paired Android phone's state: connected or not, battery, network, ringer, do-not-disturb, "
            "and which permissions the Felix app has. Use for 'phone battery', 'is my phone connected'.",
            {}, risk=READ, group="phone",
        )
        def phone_status(args: dict[str, Any], ctx: ToolContext) -> Any:
            status = self.status()
            if not status["paired"]:
                return {"paired": False, "message": "No phone is paired yet. Open the dashboard and choose Pair phone."}
            if status["connected"]:
                try:
                    live = self.request("status", {}, timeout=8)
                    self.app.store.update("devices", {"state": {**live, "at": time.time()}}, "id = ?", (self.primary(),))
                except ToolError:
                    pass
            device = self.devices()[-1]
            state = device["state"] or {}
            out: dict[str, Any] = {"paired": True, "connected": device["connected"], "name": device["name"]}
            for key in ("battery", "charging", "network", "ringer", "dnd", "locked", "permissions"):
                if key in state:
                    out[key] = state[key]
            if not device["connected"] and device["last_seen"]:
                out["last_seen"] = self.app.clock.from_ts(device["last_seen"]).isoformat(timespec="minutes")
            return out

        @tools.tool(
            "phone_control",
            "Do something on the owner's Android phone. Actions: torch_on, torch_off, volume (level 0-100, "
            "stream media|ring|alarm|notification), ringer (mode normal|vibrate|silent), dnd_on, dnd_off, "
            "brightness (level), alarm (hour, minute, label), timer (seconds, label), open_app (name), "
            "open_url (url), media (mode play_pause|next|previous|stop), speak (text), ring (find my phone: "
            "rings loudly), notify (text, label), clipboard_set (text), location (where the phone is).",
            {
                "action": {"type": "string", "enum": sorted(CONTROL_ACTIONS)},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
                "stream": {"type": "string", "enum": ["media", "ring", "alarm", "notification"]},
                "mode": {"type": "string"},
                "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                "seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                "label": {"type": "string"},
                "name": {"type": "string"},
                "url": {"type": "string"},
                "text": {"type": "string"},
            },
            ["action"], risk=LOCAL, channel="phone_control", group="phone",
        )
        def phone_control(args: dict[str, Any], ctx: ToolContext) -> Any:
            kind, build = CONTROL_ACTIONS[args["action"]]
            data = self.request(kind, build(args), timeout=25)
            message = f"Phone: {args['action'].replace('_', ' ')} done."
            if args["action"] == "location" and data:
                message = f"Your phone is at {data.get('lat')}, {data.get('lon')} (within {data.get('accuracy', '?')} m)."
            return {"message": message, **({"result": data} if data else {})}

        @tools.tool(
            "phone_call",
            "Place a phone call from the owner's phone to a contact or number. Needs the owner's approval.",
            {"to": {"type": "string", "description": "Contact name or phone number"}},
            ["to"], risk=OUTBOUND, channel="call", group="phone",
            recipients=lambda args, ctx: [self.app.contacts.address_of(args["to"], "phone")[0]],
            preview=lambda args, ctx: "Call\n" + " - ".join(reversed(self.app.contacts.address_of(args["to"], "phone"))),
        )
        def phone_call(args: dict[str, Any], ctx: ToolContext) -> Any:
            number, name = self.app.contacts.address_of(args["to"], "phone")
            data = self.request("call.dial", {"number": number}, timeout=20)
            if data.get("placed") is False:
                return {"message": f"Opened the dialler for {name} ({number}); tap call on the phone."}
            return {"message": f"Calling {name} ({number})."}

        @tools.tool(
            "phone_sms",
            "Send a text message (SMS) from the owner's phone. Needs the owner's approval; it is queued if the "
            "phone is not connected.",
            {"to": {"type": "string", "description": "Contact name or phone number"}, "text": {"type": "string"}},
            ["to", "text"], risk=OUTBOUND, channel="sms", group="phone",
            recipients=lambda args, ctx: [self.app.contacts.address_of(args["to"], "phone")[0]],
            preview=lambda args, ctx: "Send SMS\nTo: {1} ({0})\n---\n".format(
                *self.app.contacts.address_of(args["to"], "phone")) + args["text"],
        )
        def phone_sms(args: dict[str, Any], ctx: ToolContext) -> Any:
            number, name = self.app.contacts.address_of(args["to"], "phone")
            item = self.app.outbox.enqueue("sms", {"to": number, "name": name, "text": args["text"]},
                                           f"SMS to {name}", ctx.approval_id)
            if item["status"] == "sent":
                return {"message": f"Text sent to {name}."}
            return {"message": f"Text to {name} is queued; it goes out when your phone reconnects.", "status": "queued"}

        @tools.tool(
            "phone_notifications",
            "List recent notifications and missed calls captured from the phone (not chat messages - use "
            "whatsapp_chats for those). Works from the local cache when the phone is away.",
            {"app": {"type": "string", "description": "Only this app, e.g. 'bank'"},
             "hours": {"type": "number", "minimum": 1, "maximum": 168},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            risk=READ, group="phone",
        )
        def phone_notifications(args: dict[str, Any], ctx: ToolContext) -> Any:
            rows = self.recent_notifications(args.get("limit", 25), args.get("app"), args.get("hours", 24))
            return {"notifications": [{
                "app": r["app"], "title": r["title"], "text": r["text"],
                "when": self.app.clock.from_ts(r["ts"]).isoformat(timespec="minutes"),
            } for r in rows], "missed_calls": len(self.missed_calls(args.get("hours", 24)))}

        @tools.tool(
            "phone_contacts",
            "Search the phone's address book (and save the matches into Felix's contacts).",
            {"query": {"type": "string"}}, ["query"], risk=READ, group="phone",
        )
        def phone_contacts(args: dict[str, Any], ctx: ToolContext) -> Any:
            data = self.request("contacts.search", {"query": args["query"], "limit": 10}, timeout=20)
            for person in data.get("contacts", []):
                self.app.contacts.upsert(person.get("name", ""), person.get("emails"), person.get("phones"), source="phone")
            return {"contacts": data.get("contacts", [])}

        @tools.tool(
            "phone_ui",
            "Operate the phone's screen through accessibility: back, home, recents, notifications, "
            "quick_settings, lock, screenshot, click (text of the thing to tap), type (text, into the focused "
            "field), scroll (direction up|down|left|right), read_screen (what is visible now). "
            "Everything except read_screen needs the owner's approval.",
            {
                "action": {"type": "string", "enum": list(UI_ACTIONS)},
                "text": {"type": "string"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "submit": {"type": "boolean", "description": "Press enter after typing"},
            },
            ["action"], risk=DANGER, channel="phone_ui", group="phone",
            extra={"risk_for": lambda args: READ if args.get("action") == "read_screen" else DANGER},
            preview=lambda args, ctx: "Operate phone screen\n" + args["action"]
            + (f": {args['text']}" if args.get("text") else ""),
        )
        def phone_ui(args: dict[str, Any], ctx: ToolContext) -> Any:
            action = args["action"]
            if action == "read_screen":
                data = self.request("ui.dump", {}, timeout=20)
                lines = [n.get("text") or n.get("desc") for n in data.get("nodes", []) if n.get("text") or n.get("desc")]
                return {"app": data.get("package", ""), "visible_text": lines[:120]}
            if action == "click":
                self.request("ui.click", {"text": _need(args, "text")}, timeout=20)
            elif action == "type":
                self.request("ui.type", {"text": _need(args, "text"), "submit": bool(args.get("submit"))}, timeout=20)
            elif action == "scroll":
                self.request("ui.scroll", {"direction": args.get("direction", "down")}, timeout=20)
            else:
                self.request("ui.global", {"action": action}, timeout=20)
            return {"message": f"Phone screen: {action} done."}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _seconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000 if number > 1e11 else number


_ERRORS = {
    "permission_denied": "the Felix app on your phone does not have permission for that yet (open the app to grant it)",
    "accessibility_off": "Felix's accessibility service is switched off on the phone",
    "screen_locked": "the phone is locked",
    "no_reply_action": "that chat has no open notification to reply through",
    "not_found": "the phone could not find that",
    "not_installed": "that app is not installed on the phone",
    "unsupported": "the Felix app on your phone is too old for that - update it",
    "expired": "the phone picked the request up too late",
    "timeout": "the phone gave up waiting",
    "unavailable": "that is not available on the phone right now",
    "bad_args": "the request was incomplete",
    "failed": "something went wrong on the phone",
}


def _explain(kind: str, result: dict[str, Any]) -> str:
    code = str(result.get("error") or "failed")
    reason = _ERRORS.get(code) or result.get("message") or code
    return f"The phone could not do that: {reason}."


def _qr(text: str) -> str | None:
    """SVG QR code for the pairing link, when the optional 'segno' package is present."""
    try:
        import io

        import segno

        buffer = io.BytesIO()
        segno.make(text, error="m").save(buffer, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, nl=False)
        return buffer.getvalue().decode("utf-8")
    except Exception:
        return None
