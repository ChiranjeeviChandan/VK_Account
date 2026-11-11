"""Calendars: local events, recurrence, feeds, Google, phone, availability."""

import http.server
import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

import pytest

from felix.tools import ToolContext

from .conftest import NOW, TZ
from .test_ics import FEED


def at(day, hour=0, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


def run(app, tool, **args):
    result = app.tools.execute(tool, args, ToolContext(app, thread="t", source="test"))
    assert result.ok, result.error
    return result


def titles(events):
    return [e["title"] for e in events]


# -- local calendar ----------------------------------------------------------------

def test_create_list_update_delete(app):
    made = run(app, "calendar_create", title="Board review", start="tomorrow 3pm", duration_minutes=60,
               location="HQ").data
    assert made["message"] == "Added: Board review, tomorrow 3 PM–4 PM."
    run(app, "calendar_create", title="Dentist", start="2026-11-12T17:30:00+05:30")
    listing = run(app, "calendar_list", range="tomorrow").data
    assert listing["period"] == "tomorrow" and titles(listing["events"]) == ["Board review", "Dentist"]
    assert listing["events"][0]["location"] == "HQ" and listing["events"][1]["when"] == "tomorrow 5:30 PM–6 PM"
    assert run(app, "calendar_list", range="today").data["events"] == []

    moved = run(app, "calendar_update", event="board review", start="friday 10am").data
    assert moved["message"] == "Updated: Board review, Fri 13 Nov 10 AM–11 AM."      # keeps its length
    assert titles(run(app, "calendar_list", range="friday").data["events"]) == ["Board review"]

    assert run(app, "calendar_delete", event="dentist").data["message"] == "Removed: Dentist, tomorrow 5:30 PM–6 PM."
    assert titles(app.calendar.between(NOW, NOW + timedelta(days=7))) == ["Board review"]
    assert app.store.search("dentist", kinds=["event"]) == [] and app.store.search("board", kinds=["event"])


def test_natural_language_span_and_all_day(app):
    span = run(app, "calendar_create", title="Strategy offsite", start="14 Nov").data["event"]
    assert span["all_day"] and span["when"] == "Sat 14 Nov, all day"
    ranged = run(app, "calendar_create", title="Lunch", start="tomorrow 1-2pm").data["event"]
    assert ranged["when"] == "tomorrow 1 PM–2 PM"
    bad = app.tools.execute("calendar_create", {"title": "x", "start": "whenever"}, ToolContext(app))
    assert not bad.ok and "could not understand the start time" in bad.error
    backwards = app.tools.execute("calendar_create", {"title": "x", "start": "tomorrow 3pm", "end": "tomorrow 2pm"},
                                  ToolContext(app))
    assert not backwards.ok and "after the start" in backwards.error


def test_conflict_warning(app):
    run(app, "calendar_create", title="Investor call", start="tomorrow 3pm", duration_minutes=60)
    clash = run(app, "calendar_create", title="Vendor demo", start="tomorrow 3:30pm").data
    assert "Note: it overlaps Investor call (tomorrow 3 PM–4 PM)" in clash["message"]
    assert run(app, "calendar_list", range="tomorrow").data["overlapping"] == ["Investor call", "Vendor demo"]


def test_recurring_series_with_exception_and_skip(app):
    run(app, "calendar_create", title="Standup", start="2026-11-09T09:30:00+05:30", duration_minutes=15, repeat="weekdays")
    week = app.calendar.between(at(9), at(16))
    assert [e["start"].day for e in week] == [9, 10, 11, 12, 13] and week[0]["repeat"] == "every weekday"

    # move only Thursday's occurrence
    thursday = next(e for e in week if e["start"].day == 12)
    run(app, "calendar_update", event=thursday["id"], start="2026-11-12T11:00:00+05:30")
    # skip Friday's
    friday = next(e for e in week if e["start"].day == 13)
    run(app, "calendar_delete", event=friday["id"])
    after = app.calendar.between(at(9), at(16))
    assert [(e["start"].day, e["start"].hour) for e in after] == [(9, 9), (10, 9), (11, 9), (12, 11)]
    # the following week is untouched
    assert len(app.calendar.between(at(16), at(23))) == 5

    # renaming the whole series leaves the moved Thursday as its own exception
    run(app, "calendar_update", event="standup monday", title="Daily sync", whole_series=True)
    assert set(titles(app.calendar.between(at(16), at(23)))) == {"Daily sync"}
    run(app, "calendar_delete", event=str(week[0]["row_id"]), whole_series=True)
    assert app.calendar.between(at(1), at(30)) == []


def test_resolve_by_words_time_and_ambiguity(app):
    run(app, "calendar_create", title="Review with Ravi", start="tomorrow 10am")
    run(app, "calendar_create", title="Review with Meera", start="tomorrow 2pm")
    assert app.calendar.resolve("review meera")["title"] == "Review with Meera"
    assert app.calendar.resolve("tomorrow 10am")["title"] == "Review with Ravi"
    assert app.calendar.resolve("review tomorrow 2pm")["title"] == "Review with Meera"
    ambiguous = app.tools.execute("calendar_delete", {"event": "review"}, ToolContext(app))
    assert not ambiguous.ok and "More than one event matches" in ambiguous.error
    missing = app.tools.execute("calendar_delete", {"event": "yoga"}, ToolContext(app))
    assert not missing.ok and "can't find an event" in missing.error


def test_free_slots_respect_working_hours_and_meetings(app):
    run(app, "calendar_create", title="A", start="tomorrow 10am", duration_minutes=60)
    run(app, "calendar_create", title="B", start="tomorrow 1pm", duration_minutes=120)
    run(app, "calendar_create", title="Holiday", start="2026-11-12", all_day=True)       # all-day does not block
    free = run(app, "calendar_free", range="tomorrow", duration_minutes=60).data["free"]
    assert [f["text"] for f in free] == ["tomorrow 9 AM–10 AM", "tomorrow 11 AM–1 PM", "tomorrow 3 PM–6 PM"]
    assert run(app, "calendar_free", range="tomorrow", duration_minutes=240).data["note"].startswith("No gap")
    weekend = run(app, "calendar_free", range="weekend", duration_minutes=30).data
    assert weekend["free"] == []
    # "today" starts from now, not from 9am
    today = run(app, "calendar_free", range="today", duration_minutes=30).data["free"]
    assert today[0]["from"].startswith("2026-11-11T10:00")


def test_next_event_and_day_view(app):
    run(app, "calendar_create", title="Earlier", start="2026-11-11T08:00:00+05:30")
    run(app, "calendar_create", title="Next up", start="today 4pm")
    assert app.calendar.next_event()["title"] == "Next up"
    assert titles(app.calendar.day(NOW)) == ["Earlier", "Next up"]


def test_owner_can_require_approval_for_calendar_changes(app):
    app.config.set("policy.autonomy.calendar", "ask")
    result = run(app, "calendar_create", title="Private", start="tomorrow 9am")
    assert result.pending and app.calendar.between(NOW, NOW + timedelta(days=3)) == []
    app.policy.approve(result.pending["id"], via="test")
    assert titles(app.calendar.between(NOW, NOW + timedelta(days=3))) == ["Private"]


# -- ICS feed ------------------------------------------------------------------------

@pytest.fixture
def feed_server():
    state = {"hits": 0, "not_modified": 0, "body": FEED}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"] += 1
            if self.headers.get("If-None-Match") == '"v1"' and state["body"] is FEED:
                state["not_modified"] += 1
                self.send_response(304)
                self.end_headers()
                return
            data = state["body"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar")
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/basic.ics"
    yield state
    server.shutdown()
    server.server_close()


def test_ics_feed_sync_expands_and_is_read_only(app, net, feed_server):
    net.up = True
    app.calendar.add_source({"id": "work", "kind": "ics", "name": "Work feed", "url": feed_server["url"]})
    assert app.calendar.sync()["events"] == 5
    week = app.calendar.between(at(9), at(14))
    # 11th and 12th are EXDATEs; the 13th was moved from 09:30 to 11:00 by a RECURRENCE-ID override
    assert [(e["start"].day, e["start"].hour, e["title"]) for e in week] == [
        (9, 9, "Daily standup"), (10, 9, "Daily standup"), (13, 11, "Daily standup (moved)")]
    assert titles(app.calendar.between(at(21), at(22))) == ["Leadership offsite"]     # Saturday: no standup
    assert app.calendar.between(at(16), at(17), query="quarterly") == []            # cancelled events are hidden

    refused = app.tools.execute("calendar_delete", {"event": "leadership offsite"}, ToolContext(app))
    assert not refused.ok and "read-only" in refused.error

    assert app.calendar.sync()["events"] == 0 and feed_server["not_modified"] == 1    # ETag honoured
    feed_server["body"] = "<html>login required</html>"
    assert "did not return a calendar" in app.calendar.sync()["errors"][0]
    assert len(app.calendar.between(at(9), at(14))) == 3                             # a bad fetch keeps the old copy


def test_sync_offline_skips_network_sources(app, feed_server):
    app.calendar.add_source({"id": "work", "kind": "ics", "url": feed_server["url"]})
    assert app.calendar.sync() == {"events": 0, "errors": []} and feed_server["hits"] == 0


# -- Google Calendar -------------------------------------------------------------------

class FakeGoogle:
    def __init__(self):
        self.events = {}
        self.requests = []
        self.next_id = 1
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                parsed = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append((method, parsed.path, query, body))
                if self.headers.get("Authorization") != "Bearer good-token":
                    return self._send(401, {"error": {"message": "Invalid Credentials"}})
                parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
                if parts == ["users", "me", "calendarList"]:
                    return self._send(200, {"items": [
                        {"id": "vk@example.com", "summary": "VK", "primary": True, "selected": True, "accessRole": "owner"},
                        {"id": "holidays", "summary": "Holidays", "selected": False, "accessRole": "reader"}]})
                if parts[0] == "calendars" and parts[2] == "events":
                    if method == "GET":
                        lo, hi = query["timeMin"], query["timeMax"]
                        items = [e for e in fake.events.values()
                                 if "recurrence" not in e and lo <= e["start"].get("dateTime", e["start"].get("date")) < hi]
                        return self._send(200, {"items": sorted(items, key=lambda e: str(e["start"]))})
                    if method == "POST":
                        return self._send(200, fake.insert(body))
                    event = fake.events.get(parts[3])
                    if event is None:
                        return self._send(404, {"error": {"message": "Not Found"}})
                    if method == "PATCH":
                        event.update(body)
                        return self._send(200, event)
                    if method == "DELETE":
                        for key in [k for k, e in fake.events.items() if k == parts[3] or e.get("recurringEventId") == parts[3]]:
                            del fake.events[key]
                        return self._send(204)
                self._send(404, {"error": {"message": "Not Found"}})

            do_GET = lambda self: self._handle("GET")          # noqa: E731
            do_POST = lambda self: self._handle("POST")        # noqa: E731
            do_PATCH = lambda self: self._handle("PATCH")      # noqa: E731
            do_DELETE = lambda self: self._handle("DELETE")    # noqa: E731

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def insert(self, body, **extra):
        event = {"id": f"ev{self.next_id}", "status": "confirmed", "organizer": {"email": "vk@example.com", "self": True},
                 **body, **extra}
        self.next_id += 1
        self.events[event["id"]] = event
        if "recurrence" in body:                       # the API hands back the series; instances appear on list
            first = datetime.fromisoformat(body["start"]["dateTime"])
            length = datetime.fromisoformat(body["end"]["dateTime"]) - first
            for week in range(3):
                moment = first + timedelta(weeks=week)
                key = f"{event['id']}_{moment:%Y%m%d}"
                self.events[key] = {**{k: v for k, v in event.items() if k != "recurrence"}, "id": key,
                                    "recurringEventId": event["id"],
                                    "start": {"dateTime": moment.isoformat()},
                                    "end": {"dateTime": (moment + length).isoformat()}}
        return event

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def google(app, net):
    fake = FakeGoogle()
    net.up = True
    app.calendar.google.base = fake.base
    app.secrets.set("oauth:google:vk@example.com", json.dumps(
        {"access_token": "good-token", "refresh_token": "r", "expires_at": time.time() + 3600}))
    app.calendar.add_source({"id": "g", "kind": "google", "name": "Google", "account": "vk@example.com"})
    app.config.set("calendar.default_source", "g")
    yield fake
    fake.stop()


def test_google_sync_maps_fields_and_prunes(app, google):
    google.insert({"summary": "Investor call", "location": "Zoom",
                   "start": {"dateTime": "2026-11-12T15:00:00+05:30", "timeZone": "Asia/Kolkata"},
                   "end": {"dateTime": "2026-11-12T16:00:00+05:30"},
                   "attendees": [{"email": "VK@example.com", "self": True, "responseStatus": "needsAction"},
                                 {"email": "ravi@corp.com", "displayName": "Ravi", "responseStatus": "accepted"}]},
                  organizer={"email": "ravi@corp.com"})
    google.insert({"summary": "Diwali", "start": {"date": "2026-11-13"}, "end": {"date": "2026-11-14"}})
    declined = google.insert({"summary": "Optional sync", "start": {"dateTime": "2026-11-12T11:00:00+05:30"},
                              "end": {"dateTime": "2026-11-12T11:30:00+05:30"},
                              "attendees": [{"email": "vk@example.com", "self": True, "responseStatus": "declined"}]})
    assert app.calendar.sync()["events"] == 3
    events = app.calendar.between(at(12), at(14))
    assert titles(events) == ["Investor call", "Diwali"]                    # declined meeting hidden
    call = events[0]
    assert call["my_response"] == "pending" and call["organizer"] == "ravi@corp.com" and call["location"] == "Zoom"
    assert app.calendar.public(call)["awaiting_your_rsvp"] and app.calendar.public(call)["attendees"] == ["vk@example.com", "Ravi"]
    assert events[1]["all_day"]
    assert "Optional sync" in titles(app.calendar.between(at(12), at(14), include_declined=True))

    del google.events[declined["id"]]                                        # removed in Google
    del google.events["ev1"]
    app.calendar.sync()
    assert titles(app.calendar.between(at(12), at(14), include_declined=True)) == ["Diwali"]


def test_google_create_without_guests_is_immediate(app, google):
    made = run(app, "calendar_create", title="Focus time", start="tomorrow 9am", duration_minutes=90)
    assert not made.pending and made.data["message"].startswith("Added: Focus time, tomorrow 9 AM")
    method, path, query, body = google.requests[-1]
    assert (method, query["sendUpdates"]) == ("POST", "none")
    assert body["summary"] == "Focus time" and body["start"]["timeZone"] == "Asia/Kolkata"
    assert body["start"]["dateTime"] == "2026-11-12T09:00:00+05:30"


def test_google_invite_needs_approval_and_notifies_guests(app, google):
    app.contacts.upsert("Ravi Kumar", emails=["ravi@corp.com"])
    asked = run(app, "calendar_create", title="Pricing review", start="tomorrow 4pm", attendees=["Ravi Kumar"],
                location="HQ")
    assert asked.pending["channel"] == "calendar_invite" and asked.pending["risk"] == "outbound"
    assert asked.pending["preview"] == (
        "Create event and invite guests\nPricing review - tomorrow 4 PM–4:30 PM\nWhere: HQ\nGuests: ravi@corp.com")
    assert app.policy.get(asked.pending["id"])["args"]["attendees"] == ["ravi@corp.com"]      # pinned to the address shown
    # the notes are part of what guests receive, so they are part of what the owner approves
    noted = run(app, "calendar_create", title="Offsite", start="friday 10am", attendees=["Ravi Kumar"],
                description="Dial-in: 555 0100. Bring the forecast.")
    assert noted.pending["preview"].endswith("Guests: ravi@corp.com\nNotes guests will see: Dial-in: 555 0100. Bring the forecast.")
    app.policy.deny(noted.pending["id"], via="test")
    assert not [r for r in google.requests if r[0] == "POST"]
    app.policy.approve(asked.pending["id"], via="test")
    method, path, query, body = [r for r in google.requests if r[0] == "POST"][-1]
    assert query["sendUpdates"] == "all" and body["attendees"] == [{"email": "ravi@corp.com", "displayName": "Ravi Kumar"}]

    # moving it tells the guests, so that waits too; cancelling likewise
    move = run(app, "calendar_update", event="pricing review", start="tomorrow 5pm")
    assert move.pending and "New time: tomorrow 5 PM–5:30 PM" in move.pending["preview"]
    # the approval is for that event, even if another one with a similar name shows up meanwhile
    pinned = app.policy.get(move.pending["id"])["args"]["event"]
    assert pinned == str(app.calendar.resolve("pricing review")["id"])
    run(app, "calendar_create", title="Pricing review - prep", start="tomorrow 9am")
    app.policy.approve(move.pending["id"], via="test")
    assert app.calendar.resolve(pinned)["start"].hour == 17
    assert app.calendar.resolve("pricing review prep")["start"].hour == 9          # the newcomer was not touched
    app.calendar.delete(app.calendar.resolve("pricing review prep"))
    assert [r for r in google.requests if r[0] == "PATCH"][-1][2]["sendUpdates"] == "all"
    cancel = run(app, "calendar_delete", event="pricing review")
    assert cancel.pending["preview"].startswith("Cancel event\nPricing review - tomorrow 5 PM")
    app.policy.approve(cancel.pending["id"], via="test")
    assert google.events == {} and app.calendar.between(at(12), at(13)) == []


def test_google_recurring_create_and_series_delete(app, google):
    run(app, "calendar_create", title="1:1 with Meera", start="2026-11-12T10:00:00+05:30", repeat="weekly")
    assert google.requests[1][3]["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    month = app.calendar.between(at(11), at(30))
    assert [e["start"].day for e in month] == [12, 19, 26] and all(e["recurring"] for e in month)
    run(app, "calendar_delete", event=month[1]["id"])                       # one occurrence only
    assert [e["start"].day for e in app.calendar.between(at(11), at(30))] == [12, 26]
    run(app, "calendar_delete", event="1:1 with meera", whole_series=True)
    assert app.calendar.between(at(11), at(30)) == [] and google.events == {}


def test_google_errors_are_explained(app, google, net):
    app.secrets.set("oauth:google:vk@example.com", json.dumps(
        {"access_token": "bad", "refresh_token": "r", "expires_at": time.time() + 3600}))
    assert "Google Calendar refused (401): Invalid Credentials" in app.calendar.sync()["errors"][0]
    net.up = False
    offline = app.tools.execute("calendar_create", {"title": "x", "start": "tomorrow 9am"}, ToolContext(app))
    assert not offline.ok and "offline" in offline.error


# -- phone calendar ----------------------------------------------------------------------

class FakePhone:
    def __init__(self):
        self.online = True
        self.calls = []
        self.events = [
            {"id": 41, "instance_start_ms": int(at(12, 15).timestamp() * 1000), "title": "Investor call",
             "start_ms": int(at(12, 15).timestamp() * 1000), "end_ms": int(at(12, 16).timestamp() * 1000),
             "all_day": False, "location": "Zoom", "calendar": "vk@example.com", "calendar_id": 3,
             "attendees": [{"name": "Ravi", "email": "ravi@corp.com", "status": "accepted"}], "tz": "Asia/Kolkata"},
        ]

    def connected(self):
        return self.online

    def request(self, kind, args, timeout=30):
        self.calls.append((kind, args))
        if kind == "calendar.list":
            return {"events": list(self.events)}
        if kind == "calendar.create":
            return {"id": 99}
        return {}


def test_phone_calendar_sync_write_and_dedupe(app, google):
    phone = app.phone = FakePhone()
    app.calendar.add_source({"id": "phone", "kind": "phone", "name": "Phone"})
    google.insert({"summary": "Investor call", "start": {"dateTime": "2026-11-12T15:00:00+05:30"},
                   "end": {"dateTime": "2026-11-12T16:00:00+05:30"}})
    assert app.calendar.sync()["events"] == 2
    # the same meeting seen through Google and through the phone is listed once, preferring Google
    day = app.calendar.between(at(12), at(13))
    assert [(e["title"], e["source_kind"]) for e in day] == [("Investor call", "google")]

    app.config.set("calendar.default_source", "phone")
    run(app, "calendar_create", title="Gym", start="tomorrow 7am", duration_minutes=45)
    kind, args = phone.calls[-1]
    assert kind == "calendar.create" and args["title"] == "Gym" and args["end_ms"] - args["start_ms"] == 45 * 60000
    assert "Gym" in titles(app.calendar.between(at(12), at(13)))

    phone.online = False
    refused = app.tools.execute("calendar_create", {"title": "x", "start": "tomorrow 8am"}, ToolContext(app))
    assert not refused.ok and "phone is not connected" in refused.error
    assert app.calendar.sync()["errors"] == []          # an absent phone is not an error, just skipped
