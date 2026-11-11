"""The HTTP API over real sockets: who may call what, streaming, and the TLS phone link."""

import hashlib
import http.client
import json
import ssl
import threading
import time
import urllib.parse

import pytest

from felix.tools import OUTBOUND, Tool, ToolContext

from .fakes.simphone import SimPhone


@pytest.fixture
def live(app):
    app.start(port=0, background_jobs=False)
    yield app
    app.close()


class Client:
    """A minimal HTTP client that can lie about Host and Origin, as an attacker's page could."""

    def __init__(self, port, token=None, tls_pin=None):
        self.port, self.token, self.tls_pin = port, token, tls_pin
        self.seen_pin = None

    def _connection(self, timeout=10):
        if self.tls_pin is None:
            return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE            # the pin below is the trust decision
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=timeout, context=context)
        connection.connect()
        self.seen_pin = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        assert self.seen_pin == self.tls_pin, "certificate does not match the pairing fingerprint"
        return connection

    def call(self, method, path, body=None, headers=None, timeout=10):
        connection = self._connection(timeout)
        send = dict(headers or {})
        if self.token and "Authorization" not in send and "Cookie" not in send:
            send["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            send["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=send)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        kind = response.getheader("Content-Type", "")
        payload = json.loads(raw) if raw and kind.startswith("application/json") else raw
        return response.status, payload, response

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body=body if body is not None else {}, **kw)

    def events(self, path, body):
        """Read a server-sent-event response to its end."""
        status, raw, _ = self.call("POST", path, body=body)
        assert status == 200
        out = []
        for block in raw.decode().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":"))
            if "event" in fields:
                out.append((fields["event"], json.loads(fields["data"])))
        return out


@pytest.fixture
def owner(live):
    return Client(live.server.local.port, live.server.token())


# -- who may call -------------------------------------------------------------------

def test_api_needs_the_session_token(live, owner):
    anonymous = Client(live.server.local.port)
    assert anonymous.get("/api/status")[:2] == (401, {"error": "unauthorized"})
    assert Client(live.server.local.port, "wrong-token").get("/api/status")[0] == 401
    status, body, _ = owner.get("/api/status")
    assert status == 200 and body["owner"] == "VK" and body["net"]["online"] is False
    assert body["brain"]["engine"] == "rules" and body["counts"]["approvals"] == 0


def test_login_link_sets_a_strict_cookie(live):
    anonymous = Client(live.server.local.port)
    status, page, _ = anonymous.get("/")
    assert status == 401 and b"felix open" in page and live.server.token().encode() not in page
    assert anonymous.get("/login?token=nope")[0] == 403
    status, _, response = anonymous.get(f"/login?token={live.server.token()}")
    cookie = response.getheader("Set-Cookie")
    assert status == 302 and response.getheader("Location") == "/"
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    session = cookie.split(";")[0]
    status, page, response = anonymous.get("/", headers={"Cookie": session})
    assert status == 200 and "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
    assert anonymous.get("/api/status", headers={"Cookie": session})[0] == 200


def test_other_websites_cannot_drive_the_api(live, owner):
    # DNS rebinding: an attacker's domain resolving to 127.0.0.1 still sends its own Host header
    assert owner.get("/api/status", headers={"Host": "evil.example"})[:2] == (403, {"error": "forbidden_host"})
    # a cross-site form or fetch carries a foreign Origin
    status, body, _ = owner.post("/api/mode", {"mode": "offline"}, headers={"Origin": "https://evil.example"})
    assert (status, body) == (403, {"error": "forbidden_origin"}) and live.net.mode == "auto"
    same_origin = {"Origin": f"http://127.0.0.1:{live.server.local.port}", "Host": f"127.0.0.1:{live.server.local.port}"}
    assert owner.post("/api/mode", {"mode": "offline"}, headers=same_origin)[0] == 200 and live.net.mode == "offline"
    assert owner.call("OPTIONS", "/api/status")[0] == 403           # no CORS preflight is ever granted


def test_static_files_and_traversal(live, owner):
    assert owner.get("/static/locked.html")[0] == 200
    assert owner.get("/static/..%2f..%2fconfig.py")[0] == 404
    assert owner.get("/static/%2e%2e/api.py")[0] == 404
    assert owner.get("/nope")[0] == 404 and owner.call("DELETE", "/api/status")[0] == 405
    connection = http.client.HTTPConnection("127.0.0.1", live.server.local.port, timeout=5)
    connection.request("POST", "/api/mode", body=b"{not json", headers={
        "Authorization": f"Bearer {owner.token}", "Content-Type": "application/json"})
    response = connection.getresponse()
    assert (response.status, json.loads(response.read())) == (400, {"error": "bad_json"})
    connection.close()


# -- the dashboard's working surface ---------------------------------------------------

def test_chat_and_streaming(live, owner):
    status, body, _ = owner.post("/api/chat", {"text": "add task call the bank tomorrow"})
    assert status == 200 and body["engine"] == "rules" and body["reply"].startswith("Added to your list: #1 Call the bank")
    events = owner.events("/api/chat/stream", {"text": "my tasks"})
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "delta" and kinds[-1] == "done"
    assert "Call the bank" in events[0][1]["text"] and events[-1][1]["reply"] == events[0][1]["text"]
    transcript = owner.get("/api/transcript")[1]["turns"]
    assert [t["text"] for t in transcript][0] == "add task call the bank tomorrow" and len(transcript) == 4
    assert owner.post("/api/chat/reset", {"thread": "main"})[0] == 200


def test_tasks_calendar_and_today(live, owner):
    assert owner.post("/api/tasks", {"title": "Sign the lease", "due": "tomorrow 5pm", "priority": "high"})[0] == 200
    assert owner.post("/api/tasks", {"title": ""})[0] == 400
    tasks = owner.get("/api/tasks")[1]
    assert tasks["upcoming"][0]["title"] == "Sign the lease" and tasks["upcoming"][0]["priority"] == "high"
    task_id = tasks["upcoming"][0]["id"]
    assert owner.post(f"/api/tasks/{task_id}", {"action": "done"})[1]["task"]["status"] == "done"
    assert owner.post("/api/tasks/999", {"action": "done"})[0] == 404

    made = owner.post("/api/calendar", {"title": "Board review", "start": "tomorrow 3pm", "duration_minutes": 60})[1]
    assert made["message"] == "Added: Board review, tomorrow 3 PM–4 PM."
    week = owner.get("/api/calendar?range=this%20week")[1]
    assert [e["title"] for e in week["events"]] == ["Board review"] and week["sources"][0]["kind"] == "local"
    assert owner.get("/api/calendar?range=gibberish")[0] == 400
    today = owner.get("/api/today")[1]
    assert today["text"].startswith("GOOD MORNING, VK") and today["facts"]["tasks"]["done_today"][0]["title"] == "Sign the lease"
    assert owner.post("/api/calendar/delete", {"event": "board review"})[1]["message"].startswith("Removed: Board review")


def test_approvals_can_be_reviewed_edited_and_decided(live, owner):
    sent = []
    live.tools.add(Tool(
        name="fake_send", description="send", risk=OUTBOUND, channel="email",
        schema={"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "body"]},
        handler=lambda args, ctx: sent.append(args) or {"message": f"Sent to {args['to']}."},
        preview=lambda args, ctx: f"Send email\nTo: {args['to']}\n---\n{args['body']}"))
    ctx = ToolContext(live, thread="main", source="test")
    first = live.tools.execute("fake_send", {"to": "ravi@corp.com", "body": "draft"}, ctx).pending["id"]
    second = live.tools.execute("fake_send", {"to": "meera@corp.com", "body": "hello"}, ctx).pending["id"]
    listed = owner.get("/api/approvals")[1]["approvals"]
    assert [a["id"] for a in listed] == [first, second] and listed[0]["args"]["body"] == "draft"
    status, body, _ = owner.post(f"/api/approvals/{first}/approve", {"args": {"to": "ravi@corp.com", "body": "final wording"}})
    assert (status, body["message"]) == (200, "Sent to ravi@corp.com.") and sent == [{"to": "ravi@corp.com", "body": "final wording"}]
    assert owner.post(f"/api/approvals/{first}/approve")[0] == 409              # cannot be approved twice
    assert owner.post(f"/api/approvals/{second}/deny")[0] == 200 and len(sent) == 1
    assert owner.get("/api/approvals")[1]["approvals"] == []
    assert [e["action"] for e in owner.get("/api/audit")[1]["entries"]][:2] == ["approval.denied", "fake_send"]


def test_settings_are_whitelisted_and_secrets_are_write_only(live, owner):
    status, _, _ = owner.post("/api/settings", {"briefs": {"morning": {"time": "06:45"}}, "owner": {"name": "V. Kumar"},
                                                "policy": {"autonomy": {"whatsapp": "auto"}}})
    assert status == 200 and live.config.get("briefs.morning.time") == "06:45" and live.owner_name() == "V. Kumar"
    assert owner.post("/api/settings", {"briefs": {"morning": {"time": "25:99"}}})[:2] == (
        400, {"error": "bad_value", "message": "briefs.morning.time must be a time like 07:30"})
    assert owner.post("/api/settings", {"server": {"host": "0.0.0.0"}})[1]["error"] == "not_editable"
    assert owner.post("/api/settings", {"mail": {"accounts": []}})[1]["error"] == "not_editable"

    assert owner.post("/api/secrets", {"name": "anthropic_api_key", "value": "sk-live-123"})[1] == {"ok": True, "set": True}
    assert owner.post("/api/secrets", {"name": "ui_token", "value": "x"})[1]["error"] == "unknown_secret"
    settings = owner.get("/api/settings")[1]
    assert settings["secrets"] == {"anthropic_api_key": True, "whatsapp_cloud_token": False}
    assert "sk-live-123" not in json.dumps(settings) and live.server.token() not in json.dumps(settings)
    assert "sk-live-123" not in json.dumps(owner.get("/api/audit")[1])


def test_live_updates_reach_the_dashboard(live, owner):
    received = []

    def listen():
        connection = http.client.HTTPConnection("127.0.0.1", live.server.local.port, timeout=5)
        connection.request("GET", "/api/events", headers={"Authorization": f"Bearer {owner.token}"})
        response = connection.getresponse()
        buffer = b""
        while b"tasks.changed" not in buffer:
            chunk = response.read1(4096)
            if not chunk:
                break
            buffer += chunk
        received.append(buffer.decode())
        connection.close()

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(0.2)
    live.tasks.add("Trigger an update")
    thread.join(timeout=5)
    assert received and "event: hello" in received[0] and '"topic": "tasks.changed"' in received[0]


# -- the phone link -------------------------------------------------------------------

@pytest.fixture
def link(live, owner):
    """Pair a phone over the real TLS listener, the way the Android app does."""
    live.server.start_phone_link(port=0)                     # any free port
    status, pairing, _ = owner.post("/api/pair/new")
    assert status == 200, pairing
    query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(pairing["link"]).query).items()}
    phone = Client(int(query["p"]), tls_pin=query["fp"])
    status, answer, _ = phone.post("/api/phone/pair", {"code": query["c"], "device_id": "dev-1", "name": "Pixel 8"})
    assert status == 200, answer
    phone.token = answer["token"]
    phone.pairing = pairing
    return phone


def test_pairing_over_pinned_tls(live, owner, link):
    assert link.pairing["scheme"] == "https" and link.pairing["qr_data_uri"].startswith("data:image/svg+xml;base64,")
    assert link.seen_pin == link.pairing["fingerprint"]
    # a phone that pinned a different certificate refuses to talk
    with pytest.raises(AssertionError, match="does not match"):
        Client(link.port, tls_pin="00" * 32).get("/api/phone/hello")
    assert link.get("/api/phone/hello")[1]["core"] == {"name": "VK", "version": live.version}
    assert owner.get("/api/devices")[1]["devices"][0]["name"] == "Pixel 8"
    # plain HTTP to the TLS port gets nothing
    with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
        Client(link.port).get("/api/phone/hello")


def test_phone_link_exposes_only_what_a_phone_needs(live, link):
    stranger = Client(link.port, tls_pin=link.tls_pin)
    assert stranger.get("/api/phone/hello")[:2] == (401, {"error": "unauthorized"})
    assert stranger.post("/api/phone/pair", {"code": "000000", "device_id": "x"})[:2] == (403, {"error": "invalid_code"})
    # even a paired phone cannot read settings, secrets, the audit log or the dashboard
    for path in ("/api/settings", "/api/audit", "/api/status", "/api/inbox", "/", "/login?token=x"):
        assert link.get(path)[0] == 404, path
    assert link.post("/api/secrets", {"name": "anthropic_api_key", "value": "x"})[0] == 404
    # the dashboard token is not a phone credential
    assert Client(link.port, live.server.token(), tls_pin=link.tls_pin).get("/api/phone/hello")[0] == 401


def test_events_commands_and_results_over_http(live, link):
    event = {"id": "e-1", "type": "message", "ts": int(live.clock.ts() * 1000), "data": {
        "channel": "whatsapp", "package": "com.whatsapp", "chat_key": "com.whatsapp|919812345678@s.whatsapp.net",
        "chat_name": "Ravi Kumar", "sender": "Ravi Kumar", "text": "Are we on for 4pm?", "can_reply": True,
        "msg_ts": int(live.clock.ts() * 1000)}}
    assert link.post("/api/phone/events", {"events": [event, event]})[1] == {"accepted": ["e-1", "e-1"]}
    assert live.whatsapp.unread_total() == 1

    assert link.get("/api/phone/poll?wait=0.1")[1]["commands"] == []
    command_id = live.phone.send("torch", {"on": True})
    started = time.time()
    polled = link.get("/api/phone/poll?wait=5")[1]
    assert time.time() - started < 2 and polled["commands"][0]["id"] == command_id
    assert polled["commands"][0]["type"] == "torch" and polled["commands"][0]["args"] == {"on": True}
    assert link.post("/api/phone/results", {"results": [{"id": command_id, "ok": True, "data": {}}]})[1] == {"ok": True}
    assert live.store.one("SELECT status FROM phone_commands WHERE id = ?", (command_id,))["status"] == "done"


def test_long_poll_wakes_the_moment_a_command_arrives(live, link):
    answers = []
    poller = threading.Thread(target=lambda: answers.append(link.get("/api/phone/poll?wait=20", timeout=30)[1]), daemon=True)
    poller.start()
    time.sleep(0.3)
    started = time.time()
    live.phone.send("notify", {"title": "Felix", "text": "hello"})
    poller.join(timeout=5)
    assert answers and answers[0]["commands"][0]["type"] == "notify" and time.time() - started < 2


def test_overlay_chat_and_approvals_from_the_phone(live, link):
    live.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    events = link.events("/api/chat/stream", {"text": "whatsapp Meera saying Landing at 9"})
    done = events[-1][1]
    assert events[-1][0] == "done" and done["engine"] == "rules" and len(done["approvals"]) == 1
    waiting = link.get("/api/approvals")[1]["approvals"]
    assert waiting[0]["tool"] == "whatsapp_send" and waiting[0]["args"] is None      # previews only on the phone
    assert link.post(f"/api/approvals/{waiting[0]['id']}/approve")[0] in (200, 409)
    assert live.policy.get(waiting[0]["id"])["status"] != "pending"
    assert [t["text"] for t in live.brain.transcript("phone")][0] == "whatsapp Meera saying Landing at 9"
    assert link.get("/api/brief/latest")[1]["text"].startswith("*Good morning, VK*")


def test_revoked_phone_is_cut_off(live, owner, link):
    assert owner.post("/api/devices/dev-1/revoke")[1] == {"ok": True}
    assert link.get("/api/phone/hello")[0] == 401 and link.get("/api/phone/poll?wait=0.1")[0] == 401


def test_owner_typing_in_the_dashboard_sends_without_a_second_approval(live, owner, net):
    net.up = True
    sim = SimPhone(live).start()
    try:
        sim.message("Ravi Kumar", "Are we on?", key="com.whatsapp|919812345678@s.whatsapp.net")
        chat_id = owner.get("/api/chats")[1]["chats"][0]["id"]
        status, body, _ = owner.post(f"/api/chats/{chat_id}/send", {"text": "Yes, 4pm."})
        assert (status, body["status"]) == (200, "sent") and live.policy.pending() == []
        assert sim.commands("wa.reply") == [{"chat_key": "919812345678@s.whatsapp.net", "text": "Yes, 4pm."}]
        thread = owner.get(f"/api/chats/{chat_id}")[1]["messages"]
        assert [(m["mine"], m["text"]) for m in thread] == [(False, "Are we on?"), (True, "Yes, 4pm.")]
    finally:
        sim.stop()
