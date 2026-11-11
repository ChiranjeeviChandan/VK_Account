"""Phone hub and WhatsApp, driven by a simulated phone and a stand-in bridge."""

import http.server
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from felix.phone import hub as hub_module
from felix.tls import fingerprint
from felix.tools import ToolContext
from felix.whatsapp.bridge import BridgeError

from .fakes.simphone import PhoneFailure, SimPhone

FAKE_BRIDGE = Path(__file__).parent / "fakes" / "fake_bridge.py"


@pytest.fixture
def phone(app, net):
    net.up = True
    sim = SimPhone(app).start()
    yield sim
    sim.stop()


def run(felix, tool, **args):
    result = felix.tools.execute(tool, args, ToolContext(felix, thread="t", source="test"))
    assert result.ok, result.error
    return result


def approve(app, result):
    assert result.pending, "expected an approval request"
    done = app.policy.approve(result.pending["id"], via="test")
    assert done.ok, done.error
    return done.data


def wait_for(condition, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_finding_the_laptop_address_does_not_wait_for_a_slow_name_lookup(monkeypatch):
    from felix import tls

    monkeypatch.setattr(tls, "_ADDRESSES", None)
    monkeypatch.setattr(tls.socket, "getaddrinfo", lambda *args, **kwargs: time.sleep(3) or [])   # macOS can take seconds
    started = time.time()
    found = tls.local_addresses()
    assert found and time.time() - started < 4
    started = time.time()
    assert tls.local_addresses() == found and time.time() - started < 0.2             # remembered for a while


# -- pairing ----------------------------------------------------------------------

def test_pairing_link_carries_everything_the_phone_needs(app):
    pairing = app.phone.new_pairing()
    link = urllib.parse.urlparse(pairing["link"])
    query = {k: v[0] for k, v in urllib.parse.parse_qs(link.query).items()}
    assert (link.scheme, link.netloc) == ("felix", "pair")
    assert query["v"] == "1" and query["s"] == "https" and query["p"] == "8766" and query["c"] == pairing["code"]
    assert query["n"] == "VK's Felix" and query["h"]
    assert query["fp"] == fingerprint(app.paths.tls / "phone-link.crt") and len(query["fp"]) == 64
    assert pairing["qr_svg"].startswith("<svg") and len(pairing["code"]) == 6
    # the same certificate is reused, so paired phones keep working after a restart
    assert app.phone.new_pairing()["fingerprint"] == query["fp"]


def test_pairing_code_is_single_use_and_tokens_authenticate(app):
    pairing = app.phone.new_pairing()
    body = {"code": pairing["code"], "device_id": "dev-1", "name": "Pixel 8"}
    answer = app.phone.pair(body)
    assert answer["device_id"] == "dev-1" and answer["poll_wait"] == 25 and len(answer["token"]) > 30
    assert app.phone.pair(body) is None                                   # the code was consumed
    assert app.phone.pair({"code": "000000", "device_id": "dev-2"}) is None
    device = app.phone.authenticate(answer["token"])
    assert device["name"] == "Pixel 8"
    assert app.phone.authenticate("wrong") is None and app.phone.authenticate(None) is None
    assert answer["token"] not in json.dumps(app.store.query("SELECT * FROM devices"))   # only a hash is stored
    assert app.phone.revoke("dev-1") and app.phone.authenticate(answer["token"]) is None


def test_pairing_locks_out_after_repeated_wrong_codes(app):
    good = app.phone.new_pairing()["code"]
    for _ in range(hub_module.MAX_PAIR_FAILURES):
        assert app.phone.pair({"code": "999999", "device_id": "attacker"}) is None
    with pytest.raises(PermissionError):
        app.phone.pair({"code": good, "device_id": "dev-1"})


def test_expired_pairing_code_is_refused(app, monkeypatch):
    code = app.phone.new_pairing()["code"]
    real = time.time
    monkeypatch.setattr(hub_module.time, "time", lambda: real() + hub_module.PAIRING_TTL + 5)
    assert app.phone.pair({"code": code, "device_id": "dev-1"}) is None


# -- events ------------------------------------------------------------------------

def test_messages_become_chats_signals_and_contacts(app, phone):
    phone.message("Ravi Kumar", "The payment gateway is down again, customers are calling non stop!",
                  key="com.whatsapp|919812345678@s.whatsapp.net", minutes_ago=5)
    phone.message("Ravi Kumar", "Can you call me?", key="com.whatsapp|919812345678@s.whatsapp.net")
    chats = run(app, "whatsapp_chats").data
    assert chats["unread_total"] == 2
    chat = chats["chats"][0]
    assert chat["chat"] == "Ravi Kumar" and chat["unread"] == 2 and chat["needs_reply"] and chat["looks_urgent_or_unhappy"]
    assert chat["last_message"] == "Can you call me?"
    # the number embedded in the notification key became a contact
    assert app.contacts.address_of("ravi", "phone") == ("+919812345678", "Ravi Kumar")
    assert [b["title"] for b in app.pulse.report()["broken"]][0].startswith("The payment gateway is down")
    read = run(app, "whatsapp_read", chat="ravi", mark_read=True).data
    assert [m["text"] for m in read["messages"]][-1] == "Can you call me?" and read["messages"][0]["from"] == "Ravi Kumar"
    assert run(app, "whatsapp_chats", unread_only=True).data["chats"] == []


def test_duplicates_are_ignored(app, phone):
    data = {"channel": "whatsapp", "chat_key": "com.whatsapp|Meera", "chat_name": "Meera", "sender": "Meera",
            "text": "Lunch?", "msg_ts": int(app.clock.ts() * 1000), "can_reply": True}
    assert phone.emit("message", data, event_id="evt-1") == ["evt-1"]
    assert phone.emit("message", data, event_id="evt-1") == ["evt-1"]      # the phone retried: acknowledged, not stored
    assert phone.emit("message", data, event_id="evt-2") == ["evt-2"]      # Android re-posted the same notification
    assert app.store.scalar("SELECT COUNT(*) FROM messages") == 1


def test_group_chatter_does_not_demand_a_reply_unless_owner_is_named(app, phone):
    phone.message("Leadership", "Can someone share the deck?", sender="Meera", group=True,
                  key="com.whatsapp|120363041234567890@g.us")
    assert app.pulse.waiting_on_owner() == []
    phone.message("Leadership", "VK can you approve the budget today?", sender="Ravi", group=True,
                  key="com.whatsapp|120363041234567890@g.us")
    waiting = app.pulse.waiting_on_owner()
    assert [(w["who"], w["title"]) for w in waiting] == [("Ravi", "Leadership")]
    assert run(app, "whatsapp_chats").data["chats"][0]["group"] is True


def test_notifications_calls_and_status(app, phone):
    phone.emit("notification", {"package": "com.bank.app", "app": "MyBank", "key": "k1", "title": "Payment failed",
                                "text": "Your payment of Rs 4,00,000 to Acme failed. Insufficient funds.",
                                "post_ts": int(app.clock.ts() * 1000)})
    phone.emit("notification", {"package": "com.music", "app": "Music", "key": "k2", "title": "Now playing",
                                "text": "Song", "ongoing": True})
    phone.emit("call", {"state": "missed", "number": "+919812345678", "name": "Ravi Kumar"})
    phone.emit("status", {"battery": 42, "charging": True, "network": "cellular", "ringer": "silent", "dnd": True})
    listing = run(app, "phone_notifications").data
    assert [n["title"] for n in listing["notifications"]] == ["Missed call from Ravi Kumar", "Payment failed"]
    assert listing["missed_calls"] == 1
    assert [n["app"] for n in run(app, "phone_notifications", app="bank").data["notifications"]] == ["MyBank"]
    status = run(app, "phone_status").data
    assert status["connected"] and status["battery"] == 81          # asked the phone live
    phone.stop()
    cached = run(app, "phone_status").data
    assert not cached["connected"] and "last_seen" in cached and cached["battery"] == 81
    phone.emit("notification_removed", {"key": "k1"})
    assert app.store.one("SELECT removed FROM notifications WHERE key = 'k1'")["removed"] == 1


def test_sms_in_and_out(app, phone):
    app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    phone.emit("sms", {"from": "+91 98000 11122", "text": "Running 10 min late", "msg_ts": int(app.clock.ts() * 1000)})
    chats = run(app, "whatsapp_chats", channel="sms").data["chats"]
    assert chats[0]["chat"] == "Meera Iyer" and chats[0]["channel"] == "sms"
    asked = run(app, "phone_sms", to="Meera", text="No problem")
    assert asked.pending["preview"] == "Send SMS\nTo: Meera Iyer (+919800011122)\n---\nNo problem"
    assert approve(app, asked)["message"] == "Text sent to Meera Iyer."
    assert phone.commands("sms.send") == [{"to": "+919800011122", "text": "No problem"}]
    assert [m["from"] for m in run(app, "whatsapp_read", chat="Meera", channel="sms").data["messages"]] == ["Meera Iyer", "owner"]


# -- commands ----------------------------------------------------------------------

def test_request_round_trip_errors_and_timeouts(app, phone):
    assert app.phone.request("ping") == {"pong": True}
    phone.failures["torch"] = PhoneFailure("permission_denied")
    failed = app.tools.execute("phone_control", {"action": "torch_on"}, ToolContext(app))
    assert not failed.ok and "does not have permission for that yet" in failed.error
    phone.handlers["location"] = lambda a: time.sleep(0.6)
    with pytest.raises(Exception, match="did not answer in time"):
        app.phone.request("location", timeout=0.2)
    phone.stop()
    offline = app.tools.execute("phone_control", {"action": "torch_on"}, ToolContext(app))
    assert not offline.ok and "not reachable" in offline.error


def test_no_phone_paired_is_explained(app):
    result = app.tools.execute("phone_control", {"action": "torch_on"}, ToolContext(app))
    assert not result.ok and "No phone is paired" in result.error
    assert app.tools.execute("phone_status", {}, ToolContext(app)).data["paired"] is False


def test_unanswered_commands_are_offered_again(app, monkeypatch):
    monkeypatch.setattr(hub_module, "RESEND_AFTER", 0.05)
    sim = SimPhone(app).pair()
    command_id = app.phone.send("notify", {"title": "Felix", "text": "hello"}, expires_in=30)
    first = app.phone.poll(sim.device, wait=0.05)
    assert [c["id"] for c in first] == [command_id] and first[0]["args"]["text"] == "hello"
    assert app.phone.poll(sim.device, wait=0.01) == []           # just handed out: not repeated yet
    time.sleep(0.08)
    assert [c["id"] for c in app.phone.poll(sim.device, wait=0.05)] == [command_id]
    app.phone.results(sim.device, [{"id": command_id, "ok": True}])
    time.sleep(0.08)
    assert app.phone.poll(sim.device, wait=0.01) == []
    # an expired command is never delivered
    expired = app.phone.send("notify", {"text": "late"}, expires_in=-1)
    assert app.phone.poll(sim.device, wait=0.01) == []
    assert app.store.one("SELECT status FROM phone_commands WHERE id = ?", (expired,))["status"] == "expired"


def test_phone_control_maps_actions_to_commands(app, phone):
    run(app, "phone_control", action="volume", level=30, stream="ring")
    run(app, "phone_control", action="alarm", hour=6, minute=15, label="Flight")
    run(app, "phone_control", action="timer", seconds=600)
    run(app, "phone_control", action="open_app", name="Maps")
    run(app, "phone_control", action="ring")
    run(app, "phone_control", action="dnd_on")
    phone.handlers["location"] = lambda a: {"lat": 12.97, "lon": 77.59, "accuracy": 18}
    assert "12.97, 77.59" in run(app, "phone_control", action="location").data["message"]
    assert phone.calls[-7:-1] == [
        ("volume", {"stream": "ring", "level": 30}), ("alarm.set", {"hour": 6, "minute": 15, "label": "Flight"}),
        ("timer.set", {"seconds": 600, "label": ""}), ("app.open", {"name": "Maps"}), ("locate", {"seconds": 30}),
        ("dnd", {"on": True})]
    missing = app.tools.execute("phone_control", {"action": "volume"}, ToolContext(app))
    assert not missing.ok and "needs `level`" in missing.error


def test_calls_and_screen_control_need_approval(app, phone):
    app.contacts.upsert("Ravi Kumar", phones=["+919812345678"])
    call = run(app, "phone_call", to="Ravi")
    assert call.pending["preview"] == "Call\nRavi Kumar - +919812345678" and phone.commands("call.dial") == []
    assert approve(app, call)["message"] == "Calling Ravi Kumar (+919812345678)."
    phone.handlers["ui.dump"] = lambda a: {"package": "com.whatsapp", "nodes": [{"text": "Chats"}, {"desc": "New chat"}]}
    seen = run(app, "phone_ui", action="read_screen")
    assert not seen.pending and seen.data == {"app": "com.whatsapp", "visible_text": ["Chats", "New chat"]}
    tap = run(app, "phone_ui", action="click", text="Pay now")
    assert tap.pending["risk"] == "danger" and phone.commands("ui.click") == []
    approve(app, tap)
    assert phone.commands("ui.click") == [{"text": "Pay now"}]


def test_contacts_sync_from_phone(app, phone):
    phone.handlers["contacts.dump"] = lambda a: {"contacts": [
        {"name": "Anita Rao", "phones": ["+91 98111 22233"], "emails": ["anita@board.org"]},
        {"name": "Plumber", "phones": ["080-1234567"], "emails": []}]}
    assert app.phone.sync_contacts() == 2
    assert app.contacts.address_of("anita", "email") == ("anita@board.org", "Anita Rao")


# -- sending WhatsApp ---------------------------------------------------------------

def test_reply_goes_through_the_notification_then_falls_back(app, phone):
    phone.message("Ravi Kumar", "Are we on for 4pm?", key="com.whatsapp|919812345678@s.whatsapp.net")
    asked = run(app, "whatsapp_send", to="Ravi", text="Yes, see you at 4.")
    assert asked.pending["preview"] == "Send WhatsApp\nTo: Ravi Kumar (+919812345678)\n---\nYes, see you at 4."
    assert phone.calls == []                                             # nothing left the laptop yet
    assert approve(app, asked) == {"message": "Sent to Ravi Kumar on WhatsApp.", "status": "sent"}
    assert phone.commands("wa.reply") == [{"chat_key": "919812345678@s.whatsapp.net", "text": "Yes, see you at 4."}]
    assert app.pulse.waiting_on_owner() == [] and run(app, "whatsapp_chats").data["unread_total"] == 0

    # the notification has since been swiped away: open the chat through accessibility instead
    phone.message("Ravi Kumar", "One more thing?", key="com.whatsapp|919812345678@s.whatsapp.net")
    phone.failures["wa.reply"] = PhoneFailure("no_reply_action")
    approve(app, run(app, "whatsapp_send", to="Ravi", text="Go on."))
    assert phone.commands("wa.send") == [{"number": "+919812345678", "text": "Go on."}]
    history = [(m["from"], m["text"]) for m in run(app, "whatsapp_read", chat="Ravi").data["messages"]]
    assert history[-1] == ("owner", "Go on.") and len(history) == 4


def test_new_chat_to_a_contact_and_ambiguity(app, phone):
    app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    approve(app, run(app, "whatsapp_send", to="Meera", text="Sending the deck shortly."))
    assert phone.commands("wa.send") == [{"number": "+919800011122", "text": "Sending the deck shortly."}]
    phone.message("Ravi Kumar", "hi", key="com.whatsapp|Ravi Kumar")
    phone.message("Ravi Shankar", "hello", key="com.whatsapp|Ravi Shankar")
    ambiguous = app.tools.execute("whatsapp_send", {"to": "Ravi", "text": "x"}, ToolContext(app))
    assert not ambiguous.ok and "matches several chats" in ambiguous.error
    unknown = app.tools.execute("whatsapp_send", {"to": "Zoltan", "text": "x"}, ToolContext(app))
    assert not unknown.ok and "don't know a contact" in unknown.error


def test_message_waits_in_the_outbox_until_the_phone_returns(app, phone):
    app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    phone.stop()
    data = approve(app, run(app, "whatsapp_send", to="Meera", text="Landing at 9."))
    assert data["status"] == "queued" and "phone is not connected" in data["message"]
    assert len(app.outbox.pending()) == 1
    phone.start()                                                         # reconnecting flushes the queue
    assert wait_for(lambda: phone.commands("wa.send") == [{"number": "+919800011122", "text": "Landing at 9."}])
    assert wait_for(lambda: app.outbox.pending() == [])


def test_chat_seen_by_title_then_by_id_is_one_chat(app, phone):
    phone.message("Karan Shah", "First", key="com.whatsapp|Karan Shah")
    app.whatsapp.receive("whatsapp", "919877766655@s.whatsapp.net", "Karan Shah", False, "Karan Shah", "Second",
                         app.clock.ts(), "bridge", can_reply=True)
    chats = app.whatsapp.chats()
    assert len(chats) == 1 and chats[0]["key"] == "919877766655@s.whatsapp.net" and chats[0]["address"] == "+919877766655"
    assert len(app.whatsapp.history(chats[0]["id"])) == 2


# -- linked-device bridge ------------------------------------------------------------

@pytest.fixture
def bridge(app, net):
    net.up = True
    app.config.set("whatsapp.bridge.enabled", True)
    link = app.whatsapp.bridge
    link.command = lambda: [sys.executable, str(FAKE_BRIDGE)]
    link.start()
    assert wait_for(link.ready), link.describe()
    yield link
    link.stop()


def emit(link, **payload):
    link._write(link._process, {"cmd": "emit", "payload": {"event": "message", "ts": time.time(), **payload}})


def test_bridge_receives_sends_and_learns_contacts(app, bridge):
    assert bridge.me["jid"] == "919800000001@s.whatsapp.net"
    assert wait_for(lambda: app.contacts.by_address("+919812345678") is not None)
    emit(bridge, id="IN1", chat="919812345678@s.whatsapp.net", chat_name="Ravi Kumar", sender_name="Ravi Kumar",
         text="Need the contract signed today please", from_me=False, ts=app.clock.ts() - 60)
    assert wait_for(lambda: app.whatsapp.unread_total() == 1)
    assert app.pulse.waiting_on_owner()[0]["who"] == "Ravi Kumar"

    data = approve(app, run(app, "whatsapp_send", to="Ravi", text="Signing it now."))
    assert data["status"] == "sent"
    history = app.whatsapp.history(app.whatsapp.chats()[0]["id"])
    assert [(m["direction"], m["text"], m["transport"]) for m in history] == [
        ("in", "Need the contract signed today please", "bridge"), ("out", "Signing it now.", "bridge")]
    assert app.pulse.waiting_on_owner() == []


def test_owner_replying_from_their_own_phone_clears_needs_reply(app, bridge):
    emit(bridge, id="IN1", chat="919812345678@s.whatsapp.net", chat_name="Ravi Kumar", sender_name="Ravi Kumar",
         text="Can you confirm the venue?", from_me=False)
    assert wait_for(lambda: len(app.pulse.waiting_on_owner()) == 1)
    emit(bridge, id="OUT9", chat="919812345678@s.whatsapp.net", chat_name="Ravi Kumar", text="Confirmed", from_me=True)
    assert wait_for(lambda: app.pulse.waiting_on_owner() == [])
    assert app.whatsapp.unread_total() == 0


def test_self_chat_is_a_command_line_to_felix(app, bridge):
    class Brain:
        asked = []

        def respond(self, text, thread, source):
            self.asked.append((text, thread, source))
            return {"reply": "You have 2 meetings today."}

    app.brain = Brain()
    emit(bridge, id="CMD1", chat="919800000001@s.whatsapp.net", text="what's on today", from_me=True)
    assert wait_for(lambda: Brain.asked == [("what's on today", "whatsapp", "whatsapp")])
    # Felix's own answer comes back as an echo and must not be treated as a new command
    time.sleep(0.2)
    assert len(Brain.asked) == 1
    assert app.whatsapp.chats() == []          # the command chat is not stored as a conversation


def test_bridge_is_preferred_and_failures_fall_back_to_the_phone(app, bridge):
    phone = SimPhone(app).start()
    try:
        app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
        approve(app, run(app, "whatsapp_send", to="Meera", text="Via the bridge"))
        assert phone.commands("wa.send") == []
        app.contacts.upsert("Old Number", phones=["+0001112223"])
        approve(app, run(app, "whatsapp_send", to="Old Number", text="Try anyway"))     # bridge says "not on WhatsApp"
        assert phone.commands("wa.send") == [{"number": "+0001112223", "text": "Try anyway"}]
    finally:
        phone.stop()


def test_offline_mode_takes_the_whatsapp_link_down_and_auto_brings_it_back(app, net):
    net.up = True
    app.config.set("whatsapp.bridge.enabled", True)
    link = app.whatsapp.bridge
    link.command = lambda: [sys.executable, str(FAKE_BRIDGE)]
    app.start(port=0, background_jobs=True)
    try:
        assert wait_for(link.ready), link.describe()
        app.set_mode("offline")                      # "never touch the network" includes WhatsApp
        assert link.state == "stopped" and link._process is None
        app.set_mode("auto")
        assert wait_for(link.ready), link.describe()
    finally:
        app.close()
    # starting while offline does not open the link at all
    app.config.set("mode", "offline")
    app.net.set_mode("offline")
    link.state = "stopped"
    app._started = False
    app.start(port=0, background_jobs=True)
    try:
        time.sleep(0.2)
        assert link.state == "stopped" and link._process is None
    finally:
        app.close()


def test_bridge_restarts_after_a_crash_and_reports_missing_node(app, bridge, monkeypatch):
    bridge._write(bridge._process, {"cmd": "crash"})
    assert wait_for(lambda: bridge.state == "closed")
    assert wait_for(bridge.ready, seconds=6)                 # supervised: comes back by itself
    bridge.stop()
    monkeypatch.setattr("shutil.which", lambda name: None)
    fresh = type(bridge)(app, app.whatsapp)
    fresh.start()
    assert fresh.state == "error" and "Node.js is not installed" in fresh.last_error


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a stand-in for npm")
def test_bridge_installs_into_the_data_folder(app, tmp_path, monkeypatch):
    link = app.whatsapp.bridge
    runtime = app.paths.bridge_runtime
    assert not link.installed()
    assert link.prepare() == runtime
    assert sorted(p.name for p in runtime.iterdir()) == ["bridge.js", "index.js", "package.json"]
    assert json.loads((runtime / "package.json").read_text())["dependencies"] == {"baileys": "7.0.0-rc14"}

    tools = tmp_path / "bin"
    tools.mkdir()
    for name, body in (("node", "exit 0"), ("npm", 'echo "$@" > npm-args.txt; mkdir -p node_modules/baileys')):
        script = tools / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(BridgeError, match="felix connect whatsapp"):
        link.command()                                     # not installed yet: says how to install
    assert link.install() == (True, "installed")
    assert (runtime / "npm-args.txt").read_text().split() == ["install", "--omit=dev", "--no-audit", "--no-fund"]
    assert link.installed() and link.command() == [str(tools / "node"), str(runtime / "index.js")]

    # a Felix update that ships a different package list asks for a re-install instead of running stale code
    (runtime / ".installed").write_text("an older package list")
    assert not link.installed()
    # an edited copy in the data folder is put back to what this version ships
    (runtime / "bridge.js").write_text("// tampered")
    link.prepare()
    assert (runtime / "bridge.js").read_text().startswith("// Felix <-> WhatsApp linked-device bridge")

    (tools / "npm").write_text("#!/bin/sh\necho 'network unreachable' >&2\nexit 1\n")
    assert link.install() == (False, "network unreachable")
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "npm" else real_which(name))
    assert link.install() == (False, "npm was not found; it normally comes with Node.js.")


# -- the real bridge code, with a scripted WhatsApp connection -------------------------------

BRIDGE_HARNESS = Path(__file__).parent / "fakes" / "bridge_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_core_and_the_real_bridge_code_speak_the_same_protocol(app, net, tmp_path):
    """Python Core <-> felix/whatsapp/sidecar/bridge.js over stdin/stdout; only WhatsApp itself is scripted."""
    net.up = True
    app.config.set("whatsapp.bridge.enabled", True)
    log = tmp_path / "sent.jsonl"
    link = app.whatsapp.bridge
    link.command = lambda: [shutil.which("node"), str(BRIDGE_HARNESS)]
    os.environ.update(HARNESS_LOG=str(log), HARNESS_NOW=str(int(app.clock.ts()) - 60))
    states = []
    app.bus.on("whatsapp.bridge", lambda topic, data: states.append((data["state"], data["qr"])))
    try:
        link.start()
        assert wait_for(link.ready, seconds=10), link.describe()
        # the state is set a moment before it is announced, so wait for the announcement itself
        assert wait_for(lambda: len(states) >= 3), states
        assert states[:3] == [("starting", None), ("qr", "2@harness-qr"), ("open", None)]
        assert link.me == {"jid": "919800000001@s.whatsapp.net", "name": "VK"}

        # incoming: a contact learned, a 1:1 message, a group message; the status update is dropped
        assert wait_for(lambda: app.whatsapp.unread_total() == 2), app.whatsapp.chats()
        assert app.contacts.by_address("+919812345678")["name"] == "Ravi Kumar"
        chats = {c["name"]: c for c in app.whatsapp.chats()}
        assert set(chats) == {"Ravi Kumar", "Leadership"} and chats["Leadership"]["is_group"]
        assert app.whatsapp.history(chats["Leadership"]["id"])[0]["sender"] == "Meera"
        assert app.pulse.waiting_on_owner()[0]["who"] == "Ravi Kumar"

        # the owner's command typed into "Message yourself" is answered there, marked as Felix's
        assert wait_for(lambda: log.exists() and "Felix: Added to your list" in log.read_text(), seconds=10)
        assert [t["title"] for t in app.tasks.list()] == ["Call the bank"]
        command_reply = json.loads(log.read_text().splitlines()[0])
        assert command_reply["to"] == "919800000001@s.whatsapp.net"
        assert not app.whatsapp.find_chat("919800000001"), "the command chat is not a conversation to track"

        # outgoing: sent once, recorded once (WhatsApp's echo of our own message is recognised)
        data = approve(app, run(app, "whatsapp_send", to="Ravi", text="Signing it now."))
        assert data["status"] == "sent"
        time.sleep(0.2)
        history = app.whatsapp.history(chats["Ravi Kumar"]["id"])
        assert [(m["direction"], m["text"]) for m in history] == [
            ("in", "Need the contract signed today please"), ("out", "Signing it now.")]
        assert json.loads(log.read_text().splitlines()[-1]) == {"to": "919812345678@s.whatsapp.net", "text": "Signing it now."}
        assert app.pulse.waiting_on_owner() == []

        # a number that is not on WhatsApp is refused by the bridge, and the message stays queued
        app.contacts.upsert("Old Number", phones=["+0001112223"])
        queued = approve(app, run(app, "whatsapp_send", to="Old Number", text="Hello?"))
        assert queued["status"] == "queued" and len(log.read_text().splitlines()) == 2

        # logging out ends the bridge for good: no restart, and Felix shows it
        link.logout()
        assert wait_for(lambda: link.state == "logged_out", seconds=5)
        assert wait_for(lambda: link._process is None, seconds=5) and not link.ready()
    finally:
        os.environ.pop("HARNESS_LOG", None)
        os.environ.pop("HARNESS_NOW", None)
        link.stop()


# -- official Cloud API ----------------------------------------------------------------

@pytest.fixture
def graph(app, net):
    net.up = True
    state = {"requests": [], "window_closed": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, self.headers.get("Authorization"), body))
            if state["window_closed"] and body["type"] == "text":
                status, answer = 400, {"error": {"message": "Re-engagement message", "code": 131047}}
            elif self.headers.get("Authorization") != "Bearer cloud-token":
                status, answer = 401, {"error": {"message": "Invalid OAuth access token", "code": 190}}
            else:
                status, answer = 200, {"messages": [{"id": "wamid.1"}]}
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    app.whatsapp.cloud.base = f"http://127.0.0.1:{server.server_address[1]}"
    app.config.update({"whatsapp": {"cloud": {"enabled": True, "phone_number_id": "1055", "template": "felix_brief"}}})
    app.secrets.set("whatsapp_cloud_token", "cloud-token")
    yield state
    server.shutdown()
    server.server_close()


def test_brief_to_owner_over_the_cloud_api_with_template_fallback(app, graph):
    assert app.whatsapp.send_to_owner("Good morning.\n- 2 meetings") == "whatsapp (business api)"
    path, auth, body = graph["requests"][-1]
    assert path == "/v21.0/1055/messages" and auth == "Bearer cloud-token"
    assert body["to"] == "919800000001" and body["text"]["body"] == "Good morning.\n- 2 meetings"

    graph["window_closed"] = True                      # more than 24h since the owner last wrote
    app.whatsapp.send_to_owner("Good morning.\n- 2 meetings")
    body = graph["requests"][-1][2]
    assert body["type"] == "template" and body["template"]["name"] == "felix_brief"
    assert body["template"]["components"][0]["parameters"][0]["text"] == "Good morning. | - 2 meetings"

    app.secrets.set("whatsapp_cloud_token", "expired")
    graph["window_closed"] = False
    with pytest.raises(Exception, match="Invalid OAuth access token"):
        app.whatsapp.send_to_owner("x")


def test_cloud_api_never_messages_other_people(app, graph):
    app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    data = approve(app, run(app, "whatsapp_send", to="Meera", text="hello"))
    assert data["status"] == "queued" and graph["requests"] == []
