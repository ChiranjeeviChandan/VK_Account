"""Morning and evening briefs, their delivery fallbacks, and the scheduler."""

import http.server
import threading
from datetime import datetime

import pytest

from felix.connectors.feeds import parse_feed
from felix.tools import ToolContext

from .conftest import TZ
from .fakes.simphone import PhoneFailure, SimPhone
from .test_pulse import busy_day


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def fill(app):
    """A realistic Wednesday: meetings, to-dos, an outage, a VIP complaint."""
    ctx = ToolContext(app, source="test")
    app.tools.execute("calendar_create", {"title": "Board review", "start": "2026-11-11T15:00:00+05:30",
                                          "duration_minutes": 60, "location": "HQ"}, ctx)
    app.tools.execute("calendar_create", {"title": "Vendor demo", "start": "2026-11-11T15:30:00+05:30"}, ctx)
    app.tools.execute("calendar_create", {"title": "Diwali", "start": "2026-11-11"}, ctx)
    app.tools.execute("calendar_create", {"title": "Investor breakfast", "start": "2026-11-12T08:30:00+05:30",
                                          "duration_minutes": 60}, ctx)
    app.tasks.add("Send Q3 deck to Meera", due=at(10, 18), priority=1)
    app.tasks.add("Call the bank", due=at(11, 17))
    app.tasks.add("Sign the lease", due=at(12, 12))
    done = app.tasks.add("Approve payroll")
    app.tasks.complete(done["id"])
    busy_day(app)
    app.whatsapp.receive("whatsapp", "919812345678@s.whatsapp.net", "Ravi Kumar", False, "Ravi Kumar",
                         "Call me when free", app.clock.ts() - 600, "phone", can_reply=True)


def test_morning_brief_reads_like_a_briefing(app):
    fill(app)
    text = app.briefing.render(app.briefing.gather("morning"))
    lines = text.split("\n")
    assert lines[0] == "*Good morning, VK* - Wed 11 Nov"
    assert "*Today* - 2 meetings, first at 3 PM" in text
    assert "• All day: Diwali" in text and "• 3 PM–4 PM  Board review (HQ)" in text
    assert "Clash: Board review / Vendor demo overlap." in text
    assert "*To do* - 1 overdue, 1 due today" in text
    assert "• Overdue: Send Q3 deck to Meera (was due Tue 10 Nov) [high]" in text and "• Call the bank (5 PM)" in text
    assert "*What is broken* (3)" in text and "from Anita Rao (VIP)" in text
    assert "*Waiting on you*" in text and "• Meera Iyer: Q4 budget" in text
    assert "WhatsApp: 1 unread in 1 chat." in text
    assert "*Good news*" in text and "Acme" in text
    assert "1 suggested to-do from your messages." in text
    assert "Sign the lease" not in text                         # due tomorrow: not in the morning brief
    plain = app.briefing.render(app.briefing.gather("morning"), style="plain")
    assert "*" not in plain and "GOOD MORNING, VK - Wed 11 Nov" in plain


def test_evening_brief_reviews_today_and_previews_tomorrow(app):
    fill(app)
    app.clock.set(at(11, 21))
    text = app.briefing.render(app.briefing.gather("evening"))
    assert text.startswith("*Evening wrap-up, VK* - Wed 11 Nov")
    assert "*Done today* (1)" in text and "• Approve payroll" in text
    assert "*Still open* (2)" in text and "• Overdue: Send Q3 deck to Meera (was due Tue 10 Nov) [high]" in text
    assert "• Call the bank (5 PM)" in text
    assert "*Tomorrow* - 1 meeting, first at 8:30 AM" in text and "Investor breakfast" in text
    assert "*Due tomorrow*" in text and "• Sign the lease (12 PM)" in text
    assert text.index("*Still open*") < text.index("*Tomorrow*")


def test_quiet_day_brief_is_short_and_calm(app):
    text = app.briefing.render(app.briefing.gather("morning"))
    assert text == "*Good morning, VK* - Wed 11 Nov\n\n*Today* - no meetings.\n\n*To do* - nothing due today.\n\nNothing looks broken."


def test_long_briefs_are_cut_at_a_line_boundary(app):
    fill(app)
    app.config.set("briefs.max_chars", 400)
    text = app.briefing.render(app.briefing.gather("morning"))
    assert len(text) <= 400 and text.endswith("...more in the Felix dashboard.")
    assert all(not line.endswith(("Boa", "revi")) for line in text.split("\n"))


# -- delivery ------------------------------------------------------------------------

@pytest.fixture
def phone(app, net):
    net.up = True
    sim = SimPhone(app).start()
    yield sim
    sim.stop()


def test_delivery_falls_back_from_whatsapp_to_phone_notification(app, phone):
    phone.failures["wa.send"] = PhoneFailure("screen_locked")      # 7:30 am, phone on the nightstand
    fill(app)
    result = app.briefing.run("morning")
    assert result["via"] == "phone notification"
    assert "phone: The phone could not do that: the phone is locked." in " ".join(result["errors"]) or \
        "whatsapp" in result["errors"][0]
    notify = phone.commands("notify")[-1]
    assert notify["title"] == "Felix - morning brief" and notify["tag"] == "brief-morning"
    assert notify["big_text"].startswith("Good morning, VK - Wed 11 Nov") and "*" not in notify["big_text"]
    row = app.briefing.latest("morning")
    assert row["status"] == "delivered" and row["delivered_via"] == "phone notification"


def test_whatsapp_is_used_when_the_phone_can_send(app, phone):
    result = app.briefing.run("morning")
    assert result["via"] == "whatsapp (phone)"
    sent = phone.commands("wa.send")[-1]
    assert sent["number"] == "+919800000001" and sent["text"].startswith("*Good morning, VK*")
    assert phone.commands("notify") == []


def test_undeliverable_brief_waits_and_goes_out_when_a_route_appears(app, net, monkeypatch):
    monkeypatch.setattr(type(app.laptop), "notify", lambda self, title, text: False)
    result = app.briefing.run("morning")                       # offline, no phone, no desktop notifier
    assert result["via"] is None and app.briefing.latest()["status"] == "pending"
    assert [e.split(":")[0] for e in result["errors"]] == ["whatsapp", "phone", "email", "desktop"]
    net.up = True
    sim = SimPhone(app).start()                                 # the phone reconnects: the brief is retried
    try:
        deadline = datetime.now().timestamp() + 3
        while app.briefing.latest()["status"] != "delivered" and datetime.now().timestamp() < deadline:
            threading.Event().wait(0.02)
        assert app.briefing.latest()["delivered_via"] == "whatsapp (phone)"
    finally:
        sim.stop()


def test_stale_pending_brief_is_dropped_not_sent_hours_late(app, net, monkeypatch):
    monkeypatch.setattr(type(app.laptop), "notify", lambda self, title, text: False)
    app.briefing.run("morning")
    app.clock.advance(hours=5)
    monkeypatch.setattr(type(app.laptop), "notify", lambda self, title, text: True)
    assert app.briefing.retry_pending() == 0 and app.briefing.latest()["status"] == "missed"


def test_claude_polish_is_used_when_available_and_never_blocks_the_brief(app, net):
    class Claude:
        calls = []
        mode = "ok"

        def available(self):
            return True

        def complete(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if self.mode == "fail":
                raise RuntimeError("529 overloaded")
            return "*Good morning, VK* - Wed 11 Nov\n• Nothing needs you before noon.\n• Polished."

    class Brain:
        claude = Claude()

    app.brain = Brain()
    assert app.briefing.compose("morning")["engine"] == "template"       # offline: no cloud call
    net.up = True
    brief = app.briefing.compose("morning")
    assert brief["engine"] == "claude" and brief["text"].endswith("• Polished.")
    assert "Use only facts present in the JSON" in Claude.calls[0][0] and '"kind": "morning"' in Claude.calls[0][1]
    Claude.mode = "fail"
    brief = app.briefing.compose("morning")
    assert brief["engine"] == "template" and brief["text"].startswith("*Good morning, VK* - Wed 11 Nov")


def test_brief_tools(app):
    fill(app)
    ctx = ToolContext(app, source="test")
    got = app.tools.execute("brief_get", {"kind": "morning"}, ctx).data
    assert got["brief_text"].startswith("*Good morning, VK*") and got["facts"]["day"]["count"] == 2
    sent = app.tools.execute("brief_send", {"kind": "now"}, ctx)
    assert sent.ok and not sent.pending          # a message to yourself needs no approval


# -- scheduler -------------------------------------------------------------------------

def test_briefs_fire_once_at_their_time(app, monkeypatch):
    runs = []
    monkeypatch.setattr(type(app.briefing), "deliver", lambda self, text, kind: runs.append(kind) or {"via": "test", "errors": []})
    app.clock.set(at(11, 7, 29))
    assert "brief.morning" not in app.scheduler.tick()
    app.clock.set(at(11, 7, 30))
    assert "brief.morning" in app.scheduler.tick()
    app.clock.set(at(11, 7, 31))
    assert "brief.morning" not in app.scheduler.tick()        # already sent today
    app.clock.set(at(11, 21, 0))
    assert "brief.evening" in app.scheduler.tick()
    app.clock.set(at(12, 7, 45))
    assert "brief.morning" in app.scheduler.tick()             # a new day
    assert runs == ["morning", "evening", "morning"]


def test_brief_catches_up_after_sleep_but_not_too_late(app, monkeypatch):
    monkeypatch.setattr(type(app.briefing), "deliver", lambda self, text, kind: {"via": "test", "errors": []})
    app.clock.set(at(11, 9, 40))                               # lid opened two hours late
    assert "brief.morning" in app.scheduler.tick()
    app.clock.set(at(12, 12, 15))                              # next day: nearly five hours late
    assert "brief.morning" not in app.scheduler.tick()


def test_brief_settings_are_respected(app, monkeypatch):
    monkeypatch.setattr(type(app.briefing), "deliver", lambda self, text, kind: {"via": "test", "errors": []})
    app.config.set("briefs.morning.time", "06:15")
    app.config.set("briefs.evening.enabled", False)
    app.config.set("briefs.weekends", False)
    app.clock.set(at(11, 6, 20))
    assert "brief.morning" in app.scheduler.tick()
    app.clock.set(at(11, 21, 30))
    assert "brief.evening" not in app.scheduler.tick()
    app.clock.set(at(14, 6, 30))                               # Saturday
    assert "brief.morning" not in app.scheduler.tick()
    upcoming = app.scheduler.next_briefs()
    assert upcoming == {"morning": "2026-11-16T06:15+05:30"}   # Monday; no evening brief


def test_reminders_and_meeting_alerts(app, monkeypatch):
    nudges = []
    monkeypatch.setattr(type(app.scheduler), "_nudge", lambda self, title, text: nudges.append((title, text)))
    app.tasks.add("Call the landlord", remind=at(11, 10, 20))
    app.tools.execute("calendar_create", {"title": "Board review", "start": "2026-11-11T11:00:00+05:30",
                                          "location": "HQ"}, ToolContext(app))
    app.scheduler.tick()
    assert nudges == []
    app.clock.set(at(11, 10, 21))
    app.scheduler.tick()
    assert nudges == [("Reminder", "Call the landlord")]
    app.clock.set(at(11, 10, 52))
    app.scheduler.tick()
    assert nudges[-1] == ("Board review in 8 min", "today 11 AM–11:30 AM - HQ")
    app.clock.set(at(11, 10, 55))
    app.scheduler.tick()
    assert len(nudges) == 2                                    # told once per meeting, reminded once per task


def test_network_jobs_only_run_online_and_report_errors(app, net):
    app.config.set("mail.accounts", [{"id": "w", "email": "vk@example.com", "imap_host": "127.0.0.1",
                                      "imap_port": 9, "imap_security": "none", "smtp_host": "127.0.0.1", "smtp_port": 9}])
    assert "mail.sync" not in app.scheduler.tick()
    net.up = True
    app.clock.advance(seconds=1)
    assert "mail.sync" in app.scheduler.tick()
    deadline = datetime.now().timestamp() + 3
    while not app.store.one("SELECT last_error FROM jobs WHERE name = 'mail.sync'")["last_error"] \
            and datetime.now().timestamp() < deadline:
        threading.Event().wait(0.02)
    assert "No password saved" in app.store.one("SELECT last_error FROM jobs WHERE name = 'mail.sync'")["last_error"]
    assert "mail.sync" not in app.scheduler.tick()             # not again until the interval passes
    app.clock.advance(minutes=4)
    assert "mail.sync" in app.scheduler.tick()


# -- news feeds --------------------------------------------------------------------------

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Trade Wire</title>
<item><title>Acme Corp hit by data breach, customers furious</title><link>https://news.example/1</link>
<guid>n1</guid><pubDate>Wed, 11 Nov 2026 03:00:00 GMT</pubDate><description>&lt;p&gt;A security incident exposed records.&lt;/p&gt;</description></item>
<item><title>Weather stays mild</title><link>https://news.example/2</link><guid>n2</guid></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Acme wins record order</title><link href="https://blog.example/a"/><id>a1</id>
<updated>2026-11-11T02:00:00Z</updated><summary>Great quarter</summary></entry></feed>"""


def test_parse_feed_rss_and_atom():
    title, items = parse_feed(RSS)
    assert title == "Trade Wire" and [i["guid"] for i in items] == ["n1", "n2"]
    assert items[0]["summary"] == "A security incident exposed records." and items[0]["ts"] > 0
    title, items = parse_feed(ATOM)
    assert title == "Blog" and items[0]["link"] == "https://blog.example/a" and items[0]["title"] == "Acme wins record order"
    assert parse_feed("<not xml") == ("", [])


def test_watched_news_reaches_the_brief(app, net):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = RSS.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        net.up = True
        app.config.set("pulse.feeds", [f"http://127.0.0.1:{server.server_address[1]}/rss"])
        app.config.set("pulse.watch", ["acme"])
        assert app.feeds.sync() == {"new": 1, "errors": []}     # only the watched company's story is kept
        assert app.feeds.sync()["new"] == 0
        text = app.briefing.render(app.briefing.gather("morning"))
        assert "*In the news*" in text and "Acme Corp hit by data breach" in text
    finally:
        server.shutdown()
        server.server_close()
