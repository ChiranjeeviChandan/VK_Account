"""Felix's HTTP API and the two listeners that serve it."""

from __future__ import annotations

import hmac
import logging
import mimetypes
import queue
import secrets as pysecrets
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from ..config import ConfigError
from ..phone.hub import POLL_WAIT
from ..policy import public_approval
from ..timeparse import parse_datetime_loose, parse_range
from ..tls import TlsUnavailable, ensure_certificate, server_context
from ..tools import ToolContext, ToolError
from ..util import elevated, qr_data_uri
from ..whatsapp.bridge import BridgeError
from .http import ANYONE, DEVICE, OWNER, HttpError, Listener, Request, Response, Router, Stream

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.api")

STATIC = Path(__file__).parent / "static"
COOKIE = "felix_session"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}
# Settings the dashboard may change. Secrets are never read or written through this list.
# The two switches that weaken the approval rules (policy.allow_unattended_danger and
# policy.remote_danger_approval) are deliberately absent: they are changed in a terminal on the laptop.
EDITABLE = ("owner", "mode", "brain", "briefs", "policy.autonomy", "policy.trusted_recipients",
            "policy.approval_ttl_minutes", "pulse", "calendar.work_hours", "calendar.default_minutes",
            "calendar.remind_minutes", "calendar.default_source", "mail.sync_minutes", "mail.sync_days",
            "mail.quote_replies", "whatsapp", "phone.enabled", "phone.port", "phone.advertise_hosts", "laptop", "ui")
SECRET_NAMES = ("anthropic_api_key", "whatsapp_cloud_token")


class Server:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self.router = Router()
        self.local: Listener | None = None
        self.phone: Listener | None = None
        _routes(self)

    # -- lifecycle -------------------------------------------------------------
    def token(self) -> str:
        """The dashboard session token, created on first use."""
        token = self.app.secrets.get("ui_token")
        if not token:
            token = pysecrets.token_urlsafe(32)
            self.app.secrets.set("ui_token", token)
        return token

    def start(self, port: int | None = None) -> None:
        config = self.app.config.get("server", {})
        host = config.get("host", "127.0.0.1")
        self.local = Listener(self.router, host, port if port is not None else int(config.get("port", 8765)),
                              "local", self._auth_local, extra_headers=SECURITY_HEADERS)
        self.local.start()
        if self.app.config.get("phone.enabled", False):
            self.start_phone_link()

    def start_phone_link(self, port: int | None = None) -> Listener:
        """Open the LAN listener the phone connects to (TLS unless disabled)."""
        if self.phone is not None:
            return self.phone
        config = self.app.config.get("phone", {})
        context = None
        if config.get("tls", True):
            try:
                cert, key, _ = ensure_certificate(self.app.paths.tls)
            except TlsUnavailable as exc:
                raise ToolError(str(exc)) from exc
            context = server_context(cert, key)
        self.phone = Listener(self.router, config.get("bind", "0.0.0.0"),
                              port if port is not None else int(config.get("port", 8766)),
                              "phone", self._auth_phone, tls=context)
        self.phone.start()
        return self.phone

    def stop(self) -> None:
        for listener in (self.local, self.phone):
            if listener is not None:
                listener.stop()
        self.local = self.phone = None

    def url(self, with_token: bool = False) -> str:
        assert self.local is not None
        base = f"http://127.0.0.1:{self.local.port}"
        return f"{base}/login?token={self.token()}" if with_token else base

    # -- who is calling ----------------------------------------------------------
    def _auth_local(self, request: Request) -> None:
        assert self.local is not None
        host = request.headers.get("host", "").split(":")[0].strip("[]").lower()
        allowed = {"127.0.0.1", "localhost", "::1", str(self.app.config.get("server.host", "127.0.0.1")).lower()}
        if host not in allowed:
            # A web page that points its own domain at 127.0.0.1 must not reach the API.
            raise HttpError(403, "forbidden_host")
        origin = request.headers.get("origin")
        if origin and request.method != "GET":
            expected = {f"http://{request.headers.get('host', '')}"}
            if origin not in expected:
                raise HttpError(403, "forbidden_origin")
        supplied = _bearer(request) or request.headers.get("x-felix-token") or _cookie(request, COOKIE)
        if supplied and hmac.compare_digest(supplied.encode(), self.token().encode()):
            request.who = OWNER

    def _auth_phone(self, request: Request) -> None:
        if not request.path.startswith(("/api/phone/", "/api/chat", "/api/approvals", "/api/brief/latest")):
            raise HttpError(404, "not_found")
        device = self.app.phone.authenticate(_bearer(request))
        if device is not None:
            request.who, request.device = DEVICE, device


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else None


def _cookie(request: Request, name: str) -> str | None:
    for part in request.headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _int(value: str | None, default: int, low: int = 1, high: int = 500) -> int:
    try:
        return max(low, min(high, int(value))) if value is not None else default
    except ValueError:
        return default


def _routes(server: Server) -> None:  # noqa: C901 - one flat table of small handlers
    app, router = server.app, server.router
    both = (OWNER, DEVICE)

    def owner_ctx(request: Request, thread: str = "main") -> ToolContext:
        """The owner acting directly (typing in the dashboard): their click is the approval."""
        return ToolContext(app, thread=thread, source="dashboard" if request.who == OWNER else "phone",
                           actor="owner", preapproved=True)

    def run_tool(request: Request, tool: str, **args: Any) -> dict[str, Any]:
        result = app.tools.execute(tool, {k: v for k, v in args.items() if v is not None}, owner_ctx(request))
        if not result.ok:
            raise HttpError(400, "failed", result.error or "")
        return result.data if isinstance(result.data, dict) else {"result": result.data}

    def source_of(request: Request) -> str:
        return "phone" if request.who == DEVICE else "dashboard"

    # -- pages and static files --------------------------------------------------
    @router.get("/login", allow=(ANYONE,))
    def login(request: Request) -> Response:
        supplied = request.query.get("token", "")
        if not hmac.compare_digest(supplied.encode(), server.token().encode()):
            raise HttpError(403, "forbidden")
        cookie = f"{COOKIE}={supplied}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
        return Response("", 302, {"Location": "/", "Set-Cookie": cookie}, "text/plain")

    def page(name: str):
        def handler(request: Request) -> Response:
            if request.who != OWNER:
                body = (STATIC / "locked.html").read_bytes()
                return Response(body, 401, {}, "text/html; charset=utf-8")
            return Response((STATIC / name).read_bytes(), 200, {"Cache-Control": "no-store"}, "text/html; charset=utf-8")
        return handler

    router.add("GET", "/", page("index.html"), (ANYONE,))
    router.add("GET", "/overlay", page("overlay.html"), (ANYONE,))

    @router.get("/static/([A-Za-z0-9_.-]+)", allow=(ANYONE,))
    def static(request: Request) -> Response:
        path = (STATIC / request.params[0]).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            raise HttpError(404, "not_found")
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
            kind += "; charset=utf-8"
        return Response(path.read_bytes(), 200, {"Cache-Control": "no-cache"}, kind)

    # -- status -----------------------------------------------------------------
    @router.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        briefs = app.scheduler.next_briefs()
        return {
            "version": app.version, "owner": app.owner_name(), "sandbox": app.sandbox,
            "theme": app.config.get("ui.theme", "auto"),
            "net": app.net.describe(), "brain": app.brain.describe(),
            "phone": app.phone.status(), "whatsapp": app.whatsapp.routes(), "bridge": app.whatsapp.bridge.describe(),
            "mail": {"accounts": [{"id": a.id, "email": a.email} for a in app.mail.accounts()],
                     "unread": app.mail.unread_count()},
            "counts": {"approvals": len(app.policy.pending()), "outbox": len(app.outbox.pending()),
                       "unread_chats": app.whatsapp.unread_total(), "tasks_open": len(app.tasks.list())},
            "next_briefs": briefs, "phone_link": {"enabled": server.phone is not None,
                                                 "port": server.phone.port if server.phone else None},
            "time": app.clock.now().isoformat(timespec="minutes"), "secrets": app.secrets.backend,
            "elevated": elevated(),
            # What a new owner still has to connect; the dashboard shows these as first steps.
            "setup": {"claude": app.brain.claude.available(), "mail": bool(app.mail.accounts()),
                      "phone": bool(app.phone.devices()), "whatsapp_number": bool(app.config.get("owner.phone")),
                      "dismissed": bool(app.config.get("ui.setup_dismissed", False))},
        }

    @router.post("/api/shutdown")
    def shutdown(request: Request) -> dict[str, Any]:
        """Stop Felix (`felix stop`). Only the owner, and only from this laptop."""
        if request.channel != "local":
            raise HttpError(403, "forbidden")
        app.store.audit("owner", "felix.stop")
        app.stopping.set()
        return {"ok": True}

    @router.post("/api/mode")
    def mode(request: Request) -> dict[str, Any]:
        try:
            app.set_mode(request.text("mode"))
        except ValueError as exc:
            raise HttpError(400, "bad_mode", str(exc)) from exc
        return app.net.describe()

    # -- chat --------------------------------------------------------------------
    def thread_of(request: Request) -> str:
        if request.who == DEVICE:
            return "phone"
        name = request.text("thread", "main") if isinstance(request.body, dict) else request.query.get("thread", "main")
        return name[:40] or "main"

    @router.post("/api/chat", allow=both)
    def chat(request: Request) -> dict[str, Any]:
        return app.brain.respond(request.text("text"), thread=thread_of(request), source=source_of(request))

    @router.post("/api/chat/stream", allow=both)
    def chat_stream(request: Request) -> Stream:
        text, thread, source = request.text("text"), thread_of(request), source_of(request)
        events: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                result = app.brain.respond(text, thread=thread, source=source, emit=events.put)
                events.put({"type": "done", **result})
            except Exception as exc:      # respond() should never raise; belt and braces
                events.put({"type": "error", "message": str(exc)})

        threading.Thread(target=work, name="felix-chat", daemon=True).start()

        def generate() -> Iterator[tuple[str | None, Any]]:
            while True:
                try:
                    event = events.get(timeout=15)
                except queue.Empty:
                    yield None, None
                    continue
                kind = event.pop("type")
                yield kind, event
                if kind in ("done", "error"):
                    return

        return Stream(generate())

    @router.get("/api/transcript")
    def transcript(request: Request) -> dict[str, Any]:
        return {"turns": app.brain.transcript(request.query.get("thread", "main"), _int(request.query.get("limit"), 80))}

    @router.post("/api/chat/reset")
    def chat_reset(request: Request) -> dict[str, Any]:
        app.brain.reset(thread_of(request))
        return {"ok": True}

    # -- today, pulse, briefs -------------------------------------------------------
    @router.get("/api/today")
    def today(request: Request) -> dict[str, Any]:
        data = app.briefing.gather("now")
        return {"facts": data, "text": app.briefing.render(data, style="plain"), "tomorrow": app.briefing.tomorrow()}

    @router.get("/api/pulse")
    def pulse(request: Request) -> dict[str, Any]:
        hours = float(request.query.get("hours") or app.config.get("pulse.window_hours", 24))
        return app.pulse.report(max(1.0, min(hours, 720.0)))

    @router.get("/api/pulse/explain")
    def pulse_explain(request: Request) -> dict[str, Any]:
        found = app.pulse.explain(request.query.get("kind", "email"), _int(request.query.get("id"), 0, 0, 10**9))
        if found is None:
            raise HttpError(404, "not_found")
        return found

    @router.post("/api/pulse/dismiss")
    def pulse_dismiss(request: Request) -> dict[str, Any]:
        for signal_id in request.json().get("signal_ids", []):
            app.pulse.dismiss(int(signal_id))
        return {"ok": True}

    @router.get("/api/brief/latest", allow=both)
    def brief_latest(request: Request) -> dict[str, Any]:
        row = app.briefing.latest()
        if row is None:
            brief = app.briefing.compose("now", polish=False)
            return {"kind": "now", "day": brief["day"], "text": brief["text"], "created_at": int(time.time() * 1000)}
        return {"kind": row["kind"], "day": row["day"], "text": row["text"], "created_at": int(row["created_at"] * 1000),
                "delivered_via": row["delivered_via"], "status": row["status"]}

    @router.post("/api/brief/send")
    def brief_send(request: Request) -> dict[str, Any]:
        kind = request.text("kind", "now")
        if kind not in ("morning", "evening", "now"):
            raise HttpError(400, "bad_kind")
        result = app.briefing.run(kind)
        return {"via": result["via"], "errors": result["errors"], "text": result["text"]}

    # -- calendar -------------------------------------------------------------------
    @router.get("/api/calendar")
    def calendar(request: Request) -> dict[str, Any]:
        now = app.clock.now()
        if request.query.get("start"):
            start = parse_datetime_loose(request.query["start"], now)
            end = parse_datetime_loose(request.query.get("end", ""), now)
            if start is None:
                raise HttpError(400, "bad_start")
            end = end or start + timedelta(days=7)
        else:
            found = parse_range(request.query.get("range", "this week"), now, app.config.get("owner.date_order", "dmy"))
            if found is None:
                raise HttpError(400, "bad_range")
            start, end, _ = found
        events = app.calendar.between(start, end)
        return {"start": start.isoformat(), "end": end.isoformat(), "events": [app.calendar.public(e) for e in events],
                "sources": [{"id": s["id"], "kind": s["kind"], "name": s.get("name", s["id"])} for s in app.calendar.sources()]}

    @router.post("/api/calendar")
    def calendar_create(request: Request) -> dict[str, Any]:
        body = request.json()
        return run_tool(request, "calendar_create", title=body.get("title"), start=body.get("start"), end=body.get("end"),
                        duration_minutes=body.get("duration_minutes"), location=body.get("location"),
                        attendees=body.get("attendees"), repeat=body.get("repeat"), calendar=body.get("calendar"))

    @router.post("/api/calendar/delete")
    def calendar_delete(request: Request) -> dict[str, Any]:
        return run_tool(request, "calendar_delete", event=request.text("event"),
                        whole_series=bool(request.json().get("whole_series")) or None)

    @router.post("/api/calendar/sources")
    def calendar_source_add(request: Request) -> dict[str, Any]:
        try:
            source = app.calendar.add_source(dict(request.json()))
        except ToolError as exc:
            raise HttpError(400, "bad_source", str(exc)) from exc
        return {"source": source, "sync": app.calendar.sync(source["id"])}

    @router.delete("/api/calendar/sources/([A-Za-z0-9_.:-]+)")
    def calendar_source_remove(request: Request) -> dict[str, Any]:
        if request.params[0] == "local":
            raise HttpError(400, "cannot_remove", "Felix's own calendar cannot be removed")
        app.calendar.remove_source(request.params[0])
        return {"ok": True}

    # -- tasks ---------------------------------------------------------------------
    @router.get("/api/tasks")
    def tasks(request: Request) -> dict[str, Any]:
        agenda = app.tasks.agenda()
        return {**{key: [app.tasks.public(t) for t in items] for key, items in agenda.items()},
                "suggested": [app.tasks.public(t) for t in app.tasks.suggestions()],
                "done": [app.tasks.public(t) for t in app.tasks.list("done", limit=15)][-15:]}

    @router.post("/api/tasks")
    def task_add(request: Request) -> dict[str, Any]:
        body = request.json()
        return run_tool(request, "task_add", title=body.get("title"), due=body.get("due"),
                        remind_at=body.get("remind_at"), priority=body.get("priority"), notes=body.get("notes"))

    @router.post(r"/api/tasks/(\d+)")
    def task_change(request: Request) -> dict[str, Any]:
        task_id, body = int(request.params[0]), request.json()
        if app.tasks.get(task_id) is None:
            raise HttpError(404, "not_found")
        action = body.get("action")
        if action == "done":
            app.tasks.complete(task_id)
        elif action == "reopen":
            app.tasks.update(task_id, status="open")
        elif action == "delete":
            app.tasks.drop(task_id)
        elif action == "accept":
            app.tasks.update(task_id, suggested=0)
        else:
            return run_tool(request, "task_update", task=str(task_id), title=body.get("title"), due=body.get("due"),
                            priority=body.get("priority"), notes=body.get("notes"))
        task = app.tasks.get(task_id)
        return {"task": app.tasks.public(task) if task else None}

    # -- email ---------------------------------------------------------------------
    @router.get("/api/inbox")
    def inbox(request: Request) -> dict[str, Any]:
        view = request.query.get("view", "inbox")
        rows = app.mail.list(unread_only=view == "unread", needs_reply=view == "waiting", direction="out" if view == "sent" else "in",
                             query=request.query.get("q") or None, limit=_int(request.query.get("limit"), 40, 1, 100))
        return {"emails": [app.mail.summary(r) for r in rows], "unread": app.mail.unread_count(),
                "accounts": len(app.mail.accounts())}

    @router.get(r"/api/emails/(\d+)")
    def email(request: Request) -> dict[str, Any]:
        return run_tool(request, "email_read", id=int(request.params[0]), mark_read=True)

    @router.post(r"/api/emails/(\d+)/reply")
    def email_reply(request: Request) -> dict[str, Any]:
        return run_tool(request, "email_reply", id=int(request.params[0]), body=request.text("body"),
                        reply_all=bool(request.json().get("reply_all")) or None)

    @router.post(r"/api/emails/(\d+)/(archive|trash|read|unread|flag|unflag)")
    def email_action(request: Request) -> dict[str, Any]:
        email_id, action = int(request.params[0]), request.params[1]
        if action in ("archive", "trash"):
            return run_tool(request, "email_archive", id=email_id, trash=True if action == "trash" else None)
        flags = {"read": {"read": True}, "unread": {"read": False}, "flag": {"flagged": True}, "unflag": {"flagged": False}}
        return run_tool(request, "email_mark", id=email_id, **flags[action])

    @router.post("/api/emails")
    def email_send(request: Request) -> dict[str, Any]:
        body = request.json()
        return run_tool(request, "email_send", to=body.get("to"), cc=body.get("cc"), subject=body.get("subject"),
                        body=body.get("body"))

    @router.post("/api/mail/accounts")
    def mail_account_add(request: Request) -> dict[str, Any]:
        body = dict(request.json())
        password = body.pop("password", None)
        try:
            account = app.mail.add_account(body, password)
        except (ToolError, TypeError) as exc:
            raise HttpError(400, "bad_account", str(exc)) from exc
        check = app.mail.check(account) if app.net.online else {"ok": None, "note": "offline: not tested"}
        return {"id": account.id, "email": account.email, "check": check}

    @router.delete("/api/mail/accounts/([A-Za-z0-9_.@-]+)")
    def mail_account_remove(request: Request) -> dict[str, Any]:
        app.mail.remove_account(request.params[0])
        return {"ok": True}

    # -- chats ---------------------------------------------------------------------
    @router.get("/api/chats")
    def chats(request: Request) -> dict[str, Any]:
        channel = request.query.get("channel", "whatsapp")
        rows = app.whatsapp.chats(None if channel == "all" else channel, limit=_int(request.query.get("limit"), 40, 1, 100))
        return {"chats": [app.whatsapp.public_chat(c) for c in rows],
                "unread": app.whatsapp.unread_total(None if channel == "all" else channel), "routes": app.whatsapp.routes()}

    @router.get(r"/api/chats/(\d+)")
    def chat_read(request: Request) -> dict[str, Any]:
        chat = app.store.one("SELECT * FROM chats WHERE id = ?", (int(request.params[0]),))
        if chat is None:
            raise HttpError(404, "not_found")
        app.whatsapp.mark_read(chat["id"])
        return {"chat": app.whatsapp.public_chat(chat), "messages": [{
            "from": "owner" if m["direction"] == "out" else m["sender"], "text": m["text"],
            "when": app.clock.from_ts(m["ts"]).isoformat(timespec="minutes"), "mine": m["direction"] == "out",
        } for m in app.whatsapp.history(chat["id"], 60)]}

    @router.post(r"/api/chats/(\d+)/send")
    def chat_send(request: Request) -> dict[str, Any]:
        chat = app.store.one("SELECT * FROM chats WHERE id = ?", (int(request.params[0]),))
        if chat is None:
            raise HttpError(404, "not_found")
        if chat["channel"] == "sms":
            return run_tool(request, "phone_sms", to=chat["address"] or chat["name"], text=request.text("text"))
        return run_tool(request, "whatsapp_send", to=str(chat["id"]), text=request.text("text"))

    # -- approvals, outbox, audit ------------------------------------------------------
    @router.get("/api/approvals", allow=both)
    def approvals(request: Request) -> dict[str, Any]:
        if request.query.get("status", "pending") == "pending":
            rows = app.policy.pending()
        else:
            rows = app.policy.recent(_int(request.query.get("limit"), 30))
        return {"approvals": [{**public_approval(r), "args": r["args"] if request.who == OWNER else None} for r in rows]}

    @router.post(r"/api/approvals/(\d+)/approve", allow=both)
    def approve(request: Request) -> dict[str, Any]:
        edited = request.body.get("args") if isinstance(request.body, dict) and request.who == OWNER else None
        result = app.policy.approve(int(request.params[0]), via=source_of(request),
                                    args=edited if isinstance(edited, dict) else None)
        if not result.ok:
            raise HttpError(409, "not_approved", result.error or "")
        return {"ok": True, "message": result.for_human()}

    @router.post(r"/api/approvals/(\d+)/deny", allow=both)
    def deny(request: Request) -> dict[str, Any]:
        result = app.policy.deny(int(request.params[0]), via=source_of(request))
        if not result.ok:
            raise HttpError(409, "not_denied", result.error or "")
        return {"ok": True}

    @router.get("/api/outbox")
    def outbox(request: Request) -> dict[str, Any]:
        return {"items": [{k: r[k] for k in ("id", "kind", "summary", "status", "attempts", "last_error", "created_at", "sent_at")}
                          for r in app.outbox.recent(40)]}

    @router.post(r"/api/outbox/(\d+)/(retry|cancel)")
    def outbox_action(request: Request) -> dict[str, Any]:
        item_id = int(request.params[0])
        if request.params[1] == "retry":
            app.outbox.retry(item_id)
        else:
            app.outbox.cancel(item_id)
        return {"item": app.outbox.get(item_id)}

    @router.get("/api/audit")
    def audit(request: Request) -> dict[str, Any]:
        rows = app.store.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (_int(request.query.get("limit"), 60),))
        return {"entries": rows}

    @router.post("/api/sync")
    def sync(request: Request) -> dict[str, Any]:
        what = request.text("what", "all")
        out: dict[str, Any] = {}
        if what in ("mail", "all"):
            out["mail"] = app.mail.sync()
        if what in ("calendar", "all"):
            out["calendar"] = app.calendar.sync()
        if what in ("feeds", "all"):
            out["feeds"] = app.feeds.sync()
        return out

    # -- settings -------------------------------------------------------------------
    @router.get("/api/settings")
    def settings(request: Request) -> dict[str, Any]:
        config = app.config.all()
        for account in config.get("mail", {}).get("accounts", []):
            account["has_password"] = app.secrets.has(f"mail:{account.get('id')}:password")
        return {"config": config, "secrets": {name: app.secrets.has(name) for name in SECRET_NAMES},
                "secret_store": app.secrets.backend, "data_folder": str(app.paths.home),
                "google_signed_in": [a.email for a in app.mail.accounts() if app.oauth.signed_in("google", a.email)]}

    @router.post("/api/settings")
    def settings_update(request: Request) -> dict[str, Any]:
        changes = request.json()
        for dotted in _leaves(changes):
            if not any(dotted == allowed or dotted.startswith(allowed + ".") for allowed in EDITABLE):
                raise HttpError(400, "not_editable", f"{dotted} cannot be changed here")
        try:
            app.config.update(changes)
        except ConfigError as exc:
            raise HttpError(400, "bad_value", str(exc)) from exc
        if "mode" in changes:
            app.net.set_mode(changes["mode"])
        app.store.audit("owner", "settings", {"changed": sorted(_leaves(changes))})
        if changes.get("phone", {}).get("enabled") and server.phone is None:
            server.start_phone_link()
        return {"ok": True}

    @router.post("/api/secrets")
    def secret_set(request: Request) -> dict[str, Any]:
        name, value = request.text("name"), request.text("value")
        if name not in SECRET_NAMES and not name.startswith("mail:"):
            raise HttpError(400, "unknown_secret")
        if value:
            app.secrets.set(name, value.strip())
        else:
            app.secrets.delete(name)
        app.store.audit("owner", "secret", {"name": name, "set": bool(value)})
        return {"ok": True, "set": bool(value)}

    # -- phone: owner side ----------------------------------------------------------
    @router.post("/api/pair/new")
    def pair_new(request: Request) -> dict[str, Any]:
        if app.sandbox:
            raise HttpError(400, "demo", "The demo runs on sample data and cannot pair a phone. "
                                         "Run 'felix start' to use Felix for real.")
        if not app.config.get("phone.enabled", False):
            app.config.set("phone.enabled", True)
        try:
            listener = server.start_phone_link()
            pairing = app.phone.new_pairing()
        except (ToolError, OSError) as exc:
            raise HttpError(500, "phone_link_failed", str(exc)) from exc
        if listener.port != pairing["port"]:
            pairing["link"] = pairing["link"].replace(f"p={pairing['port']}", f"p={listener.port}")
            pairing["port"] = listener.port
        pairing.pop("qr_svg", None)
        pairing["qr_data_uri"] = qr_data_uri(pairing["link"])
        return pairing

    @router.get("/api/devices")
    def devices(request: Request) -> dict[str, Any]:
        return {"devices": app.phone.devices()}

    @router.post("/api/devices/([A-Za-z0-9_.:-]+)/revoke")
    def device_revoke(request: Request) -> dict[str, Any]:
        return {"ok": app.phone.revoke(request.params[0])}

    @router.post("/api/whatsapp/bridge")
    def bridge(request: Request) -> dict[str, Any]:
        action = request.text("action")
        link = app.whatsapp.bridge
        try:
            if action == "start":
                app.config.set("whatsapp.bridge.enabled", True)
                link.start()
            elif action == "stop":
                app.config.set("whatsapp.bridge.enabled", False)
                link.stop()
            elif action == "logout":
                link.logout()
            elif action == "pairing_code":
                phone = request.text("phone") or str(app.config.get("owner.phone", ""))
                return {"code": link.request_pairing_code(phone)}
            else:
                raise HttpError(400, "bad_action")
        except BridgeError as exc:
            raise HttpError(409, "bridge", str(exc)) from exc
        return link.describe()

    @router.get("/api/whatsapp/qr")
    def bridge_qr(request: Request) -> dict[str, Any]:
        """The linked-device QR as an image, while the bridge is waiting to be linked."""
        link = app.whatsapp.bridge
        return {"state": link.state, "data_uri": qr_data_uri(link.qr) if link.qr else None,
                "pairing_code": link.pairing_code}

    # -- live updates ----------------------------------------------------------------
    @router.get("/api/events")
    def events(request: Request) -> Stream:
        stream = app.bus.open_stream()

        def generate() -> Iterator[tuple[str | None, Any]]:
            try:
                yield "hello", {"time": time.time()}
                while True:
                    try:
                        event = stream.get(timeout=15)
                    except queue.Empty:
                        yield None, None
                        continue
                    yield "update", {"topic": event["topic"], "data": _safe(event["data"])}
            finally:
                app.bus.close_stream(stream)

        return Stream(generate())

    # -- phone: device side -----------------------------------------------------------
    @router.post("/api/phone/pair", allow=(ANYONE,))
    def phone_pair(request: Request) -> Response | dict[str, Any]:
        try:
            answer = app.phone.pair(request.json())
        except PermissionError:
            raise HttpError(429, "too_many_attempts") from None
        if answer is None:
            raise HttpError(403, "invalid_code")
        return answer

    @router.post("/api/phone/events", allow=(DEVICE,))
    def phone_events(request: Request) -> dict[str, Any]:
        assert request.device is not None
        return {"accepted": app.phone.ingest(request.device, request.json().get("events") or [])}

    @router.get("/api/phone/poll", allow=(DEVICE,))
    def phone_poll(request: Request) -> dict[str, Any]:
        assert request.device is not None
        try:
            wait = float(request.query.get("wait", POLL_WAIT))
        except ValueError:
            wait = POLL_WAIT
        return {"commands": app.phone.poll(request.device, wait), "server_ts": int(time.time() * 1000)}

    @router.post("/api/phone/results", allow=(DEVICE,))
    def phone_results(request: Request) -> dict[str, Any]:
        assert request.device is not None
        app.phone.results(request.device, request.json().get("results") or [])
        return {"ok": True}

    @router.get("/api/phone/hello", allow=(DEVICE,))
    def phone_hello(request: Request) -> dict[str, Any]:
        state = app.net.describe()
        return {"core": {"name": app.owner_name() or "Felix", "version": app.version}, "online": state["online"],
                "mode": state["mode"], "time": int(time.time() * 1000)}


def _leaves(values: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, value in values.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out += _leaves(value, dotted + ".")
        else:
            out.append(dotted)
    return out


def _safe(data: dict[str, Any]) -> dict[str, Any]:
    """Bus events are small already; make sure nothing unserialisable leaks into the stream."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, dict)):
            out[key] = value
    return out
