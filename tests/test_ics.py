from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from felix.connectors.ics import parse_duration, parse_ics, to_ics, zone_for

IST = ZoneInfo("Asia/Kolkata")

FEED = "\r\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
    "BEGIN:VTIMEZONE",
    "TZID:Asia/Kolkata",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0530",
    "TZOFFSETTO:+0530",
    "END:STANDARD",
    "END:VTIMEZONE",
    "BEGIN:VEVENT",
    "UID:standup@google.com",
    "DTSTART;TZID=Asia/Kolkata:20261102T093000",
    "DTEND;TZID=Asia/Kolkata:20261102T094500",
    "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "EXDATE;TZID=Asia/Kolkata:20261111T093000,20261112T093000",
    "SUMMARY:Daily standup",
    "LOCATION:Board room\\, 4th floor",
    "DESCRIPTION:Agenda:\\n1. Blockers\\n2. Plan; priorities",
    "ORGANIZER;CN=VK:mailto:vk@example.com",
    "ATTENDEE;CN=\"Kumar, Ravi\";PARTSTAT=ACCEPTED:mailto:Ravi@Corp.com",
    "ATTENDEE;CN=Meera;PARTSTAT=NEEDS-ACTION:mailto:meera@corp.com",
    "BEGIN:VALARM",
    "ACTION:DISPLAY",
    "TRIGGER:-PT10M",
    "END:VALARM",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:standup@google.com",
    "RECURRENCE-ID;TZID=Asia/Kolkata:20261113T093000",
    "DTSTART;TZID=Asia/Kolkata:20261113T110000",
    "DTEND;TZID=Asia/Kolkata:20261113T111500",
    "SUMMARY:Daily standup (moved)",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:offsite@google.com",
    "DTSTART;VALUE=DATE:20261120",
    "DTEND;VALUE=DATE:20261122",
    "SUMMARY:Leadership offsite",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:outlook-1",
    "DTSTART;TZID=India Standard Time:20261116T150000",
    "DURATION:PT1H30M",
    "SUMMARY:Quarterly business review with a very long title that goes on and on so that the line ha",
    " s to be folded across two physical lines",
    "STATUS:CANCELLED",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:utc-1",
    "DTSTART:20261118T043000Z",
    "DTEND:20261118T053000Z",
    "SUMMARY:Investor call",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "SUMMARY:No start time - must be skipped",
    "END:VEVENT",
    "END:VCALENDAR",
])


def by_title(events):
    return {e.title: e for e in events}


def test_parse_feed():
    events = parse_ics(FEED, IST)
    assert len(events) == 5
    found = by_title(events)

    standup = found["Daily standup"]
    assert standup.start == datetime(2026, 11, 2, 9, 30, tzinfo=IST) and standup.end - standup.start == timedelta(minutes=15)
    assert standup.rrule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    assert standup.exdates == [datetime(2026, 11, 11, 9, 30, tzinfo=IST), datetime(2026, 11, 12, 9, 30, tzinfo=IST)]
    assert standup.location == "Board room, 4th floor"
    assert standup.description == "Agenda:\n1. Blockers\n2. Plan; priorities"
    assert standup.organizer == "vk@example.com" and standup.reminders == [10]
    assert standup.attendees == [
        {"email": "ravi@corp.com", "name": "Kumar, Ravi", "status": "accepted"},
        {"email": "meera@corp.com", "name": "Meera", "status": "pending"},
    ]

    moved = found["Daily standup (moved)"]
    assert moved.uid == "standup@google.com" and moved.recurrence_id == datetime(2026, 11, 13, 9, 30, tzinfo=IST)
    assert moved.start.hour == 11

    offsite = found["Leadership offsite"]
    assert offsite.all_day and offsite.start == datetime(2026, 11, 20, tzinfo=IST)
    assert offsite.end == datetime(2026, 11, 22, tzinfo=IST)            # DTEND is exclusive

    review = next(e for e in events if e.uid == "outlook-1")
    assert review.title.endswith("the line has to be folded across two physical lines")
    assert review.start == datetime(2026, 11, 16, 15, 0, tzinfo=IST)     # Windows zone name understood
    assert review.end - review.start == timedelta(minutes=90) and review.status == "cancelled"

    call = found["Investor call"]
    assert call.start == datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc)
    assert call.start.astimezone(IST).hour == 10


def test_garbage_is_tolerated():
    assert parse_ics("", IST) == []
    assert parse_ics("this is not a calendar", IST) == []
    broken = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nDTSTART:not-a-date\nSUMMARY:bad\nEND:VEVENT\n" \
             "BEGIN:VEVENT\nUID:y\nDTSTART:20261118T043000Z\nSUMMARY:good\nEND:VEVENT\nEND:VCALENDAR"
    assert [e.title for e in parse_ics(broken, IST)] == ["good"]


def test_zone_for():
    assert zone_for("Asia/Kolkata", timezone.utc) == IST
    assert zone_for("India Standard Time", timezone.utc) == IST
    assert zone_for("/freeassociation.sourceforge.net/Tzfile/Asia/Kolkata", timezone.utc) == IST
    assert zone_for("(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi", timezone.utc).utcoffset(None) == timedelta(hours=5, minutes=30)
    assert zone_for("Nonsense/Zone", IST) == IST and zone_for(None, IST) == IST


def test_parse_duration():
    assert parse_duration("PT1H30M") == timedelta(minutes=90)
    assert parse_duration("P1W") == timedelta(weeks=1)
    assert parse_duration("-PT15M") == timedelta(minutes=-15)
    assert parse_duration("P1DT2H") == timedelta(days=1, hours=2)
    assert parse_duration("soon") is None and parse_duration("P") is None


def test_round_trip_through_to_ics():
    events = [
        {"uid": "a@felix", "title": "Review; pricing, margins", "start": datetime(2026, 11, 12, 15, 0, tzinfo=IST),
         "end": datetime(2026, 11, 12, 16, 0, tzinfo=IST), "location": "HQ", "description": "Line one\nLine two " + "x" * 120,
         "attendees": [{"email": "ravi@corp.com", "name": "Ravi"}], "organizer": "vk@example.com"},
        {"uid": "b@felix", "title": "Standup", "start": datetime(2026, 11, 2, 9, 30, tzinfo=IST),
         "end": datetime(2026, 11, 2, 9, 45, tzinfo=IST), "rrule": "FREQ=WEEKLY;BYDAY=MO",
         "exdates": [datetime(2026, 11, 9, 9, 30, tzinfo=IST)]},
        {"uid": "c@felix", "title": "Offsite", "start": datetime(2026, 11, 20, tzinfo=IST),
         "end": datetime(2026, 11, 22, tzinfo=IST), "all_day": True},
    ]
    text = to_ics(events)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "DTSTART;TZID=Asia/Kolkata:20261102T093000" in text            # repeating events keep their zone
    parsed = by_title(parse_ics(text, IST))
    review = parsed["Review; pricing, margins"]
    assert review.start == events[0]["start"] and review.description == events[0]["description"]
    assert review.attendees[0]["email"] == "ravi@corp.com" and review.organizer == "vk@example.com"
    assert parsed["Standup"].rrule == "FREQ=WEEKLY;BYDAY=MO" and parsed["Standup"].exdates == events[1]["exdates"]
    assert parsed["Offsite"].all_day and parsed["Offsite"].end == events[2]["end"]
