"""iCalendar (RFC 5545) reading and writing, without dependencies.

Reads the calendar feeds that Google ("secret address"), Outlook and others
publish, and writes events for export. Recurrence is kept as a rule and
expanded on demand by ``rrule.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

# Windows time-zone names that Outlook and Exchange put into TZID.
WINDOWS_ZONES = {
    "India Standard Time": "Asia/Kolkata", "Sri Lanka Standard Time": "Asia/Colombo",
    "Arabian Standard Time": "Asia/Dubai", "Arab Standard Time": "Asia/Riyadh",
    "Singapore Standard Time": "Asia/Singapore", "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo", "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney", "New Zealand Standard Time": "Pacific/Auckland",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev",
    "Russian Standard Time": "Europe/Moscow", "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem", "South Africa Standard Time": "Africa/Johannesburg",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Atlantic Standard Time": "America/Halifax",
    "E. South America Standard Time": "America/Sao_Paulo", "Argentina Standard Time": "America/Buenos_Aires",
    "SE Asia Standard Time": "Asia/Bangkok", "Pakistan Standard Time": "Asia/Karachi",
    "Bangladesh Standard Time": "Asia/Dhaka", "Nepal Standard Time": "Asia/Kathmandu",
    "UTC": "UTC", "Coordinated Universal Time": "UTC",
}


@dataclass
class IcsEvent:
    uid: str
    title: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    description: str = ""
    location: str = ""
    rrule: str = ""
    exdates: list[datetime] = field(default_factory=list)
    recurrence_id: datetime | None = None
    status: str = "confirmed"
    organizer: str = ""
    attendees: list[dict[str, str]] = field(default_factory=list)
    reminders: list[int] = field(default_factory=list)
    url: str = ""
    tz: str = ""
    transparent: bool = False


def zone_for(tzid: str | None, default: tzinfo) -> tzinfo:
    """Map a TZID (IANA, Windows or path-style) to a tzinfo; fall back to ``default``."""
    if not tzid:
        return default
    tzid = tzid.strip().strip('"')
    candidates = [tzid, WINDOWS_ZONES.get(tzid, "")]
    parts = tzid.split("/")
    if len(parts) > 2:
        candidates.append("/".join(parts[-2:]))        # "/vendor/Tzfile/Asia/Kolkata"
        candidates.append("/".join(parts[-3:]))        # "America/Argentina/Buenos_Aires"
    match = re.search(r"\(?(?:UTC|GMT)\s*([+-]\d{1,2}):?(\d{2})\)?", tzid)
    from zoneinfo import ZoneInfo

    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    if match:
        sign = -1 if match.group(1).startswith("-") else 1
        return timezone(sign * timedelta(hours=abs(int(match.group(1))), minutes=int(match.group(2))))
    return default


def parse_ics(text: str, default_tz: tzinfo) -> list[IcsEvent]:
    """Parse every VEVENT in an iCalendar document. Malformed events are skipped."""
    events: list[IcsEvent] = []
    stack: list[str] = []
    current: list[tuple[str, dict[str, str], str]] | None = None
    alarms: list[int] = []
    in_alarm = False
    for name, params, value in _lines(text):
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VEVENT":
                current, alarms = [], []
            elif value.upper() == "VALARM":
                in_alarm = True
            continue
        if name == "END":
            ended = value.upper()
            if stack and stack[-1] == ended:
                stack.pop()
            if ended == "VALARM":
                in_alarm = False
            elif ended == "VEVENT" and current is not None:
                try:
                    event = _build(current, default_tz)
                except (ValueError, KeyError, IndexError):
                    event = None
                if event is not None and event.start is not None:
                    event.reminders = sorted(set(alarms))
                    events.append(event)
                current = None
            continue
        if current is None:
            continue
        if in_alarm:
            if name == "TRIGGER":
                delta = parse_duration(value)
                if delta is not None and delta <= timedelta(0):
                    alarms.append(int(-delta.total_seconds() // 60))
            continue
        if stack and stack[-1] == "VEVENT":
            current.append((name, params, value))
    return events


def _lines(text: str):
    """Unfold and split content lines into (NAME, params, value)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    for line in text.split("\n"):
        if not line.strip() or ":" not in line:
            continue
        head, value = _split_head(line)
        parts = _split_params(head)
        name = parts[0].upper()
        params: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                key, val = part.split("=", 1)
                params[key.upper()] = val.strip('"')
        yield name, params, value


def _split_head(line: str) -> tuple[str, str]:
    """Split at the first colon that is not inside a quoted parameter value."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            return line[:index], line[index + 1:]
    return line, ""


def _split_params(head: str) -> list[str]:
    parts, buffer, quoted = [], "", False
    for char in head:
        if char == '"':
            quoted = not quoted
            buffer += char
        elif char == ";" and not quoted:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    parts.append(buffer)
    return parts


def _build(props: list[tuple[str, dict[str, str], str]], default_tz: tzinfo) -> IcsEvent | None:
    event = IcsEvent(uid="")
    duration: timedelta | None = None
    for name, params, value in props:
        if name == "UID":
            event.uid = value.strip()
        elif name == "SUMMARY":
            event.title = unescape(value)
        elif name == "DESCRIPTION":
            event.description = unescape(value)
        elif name == "LOCATION":
            event.location = unescape(value)
        elif name == "URL":
            event.url = value.strip()
        elif name == "DTSTART":
            event.start, event.all_day = parse_time(value, params, default_tz)
            event.tz = params.get("TZID", "")
        elif name == "DTEND":
            event.end, _ = parse_time(value, params, default_tz)
        elif name == "DURATION":
            duration = parse_duration(value)
        elif name == "RRULE":
            event.rrule = value.strip()
        elif name == "EXDATE":
            for item in value.split(","):
                if item.strip():
                    event.exdates.append(parse_time(item, params, default_tz)[0])
        elif name == "RECURRENCE-ID":
            event.recurrence_id = parse_time(value, params, default_tz)[0]
        elif name == "STATUS":
            event.status = value.strip().lower()
        elif name == "TRANSP":
            event.transparent = value.strip().upper() == "TRANSPARENT"
        elif name == "ORGANIZER":
            event.organizer = _mailto(value) or params.get("CN", "")
        elif name == "ATTENDEE":
            address = _mailto(value)
            if address or params.get("CN"):
                event.attendees.append({
                    "email": address, "name": params.get("CN", ""),
                    "status": params.get("PARTSTAT", "NEEDS-ACTION").lower().replace("needs-action", "pending"),
                })
    if event.start is None:
        return None
    if event.end is None:
        if duration is not None:
            event.end = event.start + duration
        elif event.all_day:
            event.end = event.start + timedelta(days=1)
        else:
            event.end = event.start
    if not event.uid:
        event.uid = f"{int(event.start.timestamp())}-{abs(hash(event.title)) % 10**8}@felix"
    return event


def parse_time(value: str, params: dict[str, str], default_tz: tzinfo) -> tuple[datetime, bool]:
    """Return (aware datetime, is_all_day) for a DATE or DATE-TIME value."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time(0, 0), tzinfo=default_tz), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    moment = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    return moment.replace(tzinfo=zone_for(params.get("TZID"), default_tz)), False


def parse_duration(value: str) -> timedelta | None:
    match = re.fullmatch(
        r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip().upper())
    if not match or not any(match.groups()[1:]):
        return None
    sign = -1 if match.group(1) == "-" else 1
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups()[1:])
    return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def unescape(value: str) -> str:
    out, index = [], 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append("\n" if nxt in "nN" else nxt)
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out).strip()


def escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _mailto(value: str) -> str:
    value = value.strip()
    return value[7:].strip().lower() if value.lower().startswith("mailto:") else ""


def _fold(line: str) -> str:
    """Content lines are limited to 75 octets; continuation lines start with a space."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks, start, limit = [], 0, 75
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:   # never split a UTF-8 character
            end -= 1
        chunks.append(data[start:end].decode("utf-8"))
        start, limit = end, 74
    return "\r\n ".join(chunks)


def to_ics(events: list[dict[str, Any]], product: str = "-//Felix//Assistant//EN", method: str | None = None) -> str:
    """Serialise events (dicts with start/end datetimes) as a VCALENDAR."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{product}", "CALSCALE:GREGORIAN"]
    if method:
        lines.append(f"METHOD:{method}")
    for event in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{event['uid']}")
        lines.append(f"DTSTAMP:{stamp}")
        start, end = event["start"], event["end"]
        if event.get("all_day"):
            lines.append(f"DTSTART;VALUE=DATE:{_as_date(start)}")
            lines.append(f"DTEND;VALUE=DATE:{_as_date(end)}")
        elif event.get("rrule") and getattr(start.tzinfo, "key", None):
            # A repeating event must keep its zone, or it would drift across DST.
            lines.append(f"DTSTART;TZID={start.tzinfo.key}:{start.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND;TZID={start.tzinfo.key}:{end.astimezone(start.tzinfo).strftime('%Y%m%dT%H%M%S')}")
        else:
            lines.append("DTSTART:" + start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            lines.append("DTEND:" + end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        lines.append("SUMMARY:" + escape(event.get("title", "")))
        if event.get("description"):
            lines.append("DESCRIPTION:" + escape(event["description"]))
        if event.get("location"):
            lines.append("LOCATION:" + escape(event["location"]))
        if event.get("rrule"):
            lines.append("RRULE:" + event["rrule"])
        for moment in event.get("exdates") or []:
            lines.append("EXDATE:" + moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        if event.get("organizer"):
            lines.append(f"ORGANIZER:mailto:{event['organizer']}")
        for attendee in event.get("attendees") or []:
            name = f";CN={attendee['name']}" if attendee.get("name") else ""
            lines.append(f"ATTENDEE{name};RSVP=TRUE:mailto:{attendee['email']}")
        if event.get("status") == "cancelled":
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _as_date(moment: datetime | date) -> str:
    return moment.strftime("%Y%m%d")
