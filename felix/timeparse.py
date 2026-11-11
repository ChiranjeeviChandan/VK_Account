"""Natural-language dates and times, with no network and no dependencies.

Used by the offline command engine ("meeting with Ravi tomorrow 3-4pm",
"remind me in 20 minutes", "what's on next Friday") and by tools that accept
loose time strings from a language model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60, "ninety": 90, "couple": 2, "few": 3,
}
DAYPARTS = {
    "morning": (9, 0), "noon": (12, 0), "midday": (12, 0), "lunch": (13, 0), "lunchtime": (13, 0),
    "afternoon": (15, 0), "evening": (18, 0), "tonight": (20, 0), "night": (20, 0),
    "midnight": (0, 0), "eod": (18, 0),
}

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_WDAY = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_NUM = r"\d+(?:\.\d+)?|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_UNIT = (
    r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|years?|yrs?"
)
_AMPM = r"(?:a\.?m\.?|p\.?m\.?)"


@dataclass
class When:
    start: datetime
    end: datetime | None = None
    has_date: bool = False
    has_time: bool = False
    duration: timedelta | None = None
    rest: str = ""          # the input with the time phrases removed

    @property
    def all_day(self) -> bool:
        return self.has_date and not self.has_time


def _number(token: str) -> float:
    token = token.strip().lower()
    if token in NUMBER_WORDS:
        return float(NUMBER_WORDS[token])
    return float(token)


def _unit_delta(amount: float, unit: str) -> timedelta:
    unit = unit.lower()
    if unit.startswith("sec"):
        return timedelta(seconds=amount)
    if unit.startswith("min"):
        return timedelta(minutes=amount)
    if unit.startswith(("hour", "hr")):
        return timedelta(hours=amount)
    if unit.startswith("day"):
        return timedelta(days=amount)
    if unit.startswith(("week", "wk")):
        return timedelta(weeks=amount)
    if unit.startswith("month"):
        return timedelta(days=30 * amount)
    return timedelta(days=365 * amount)


def parse_duration(text: str) -> timedelta | None:
    """'30 min', '1.5 hours', 'an hour and a half', '1h30m', 'half an hour'."""
    low = (text or "").lower().strip()
    if not low:
        return None
    match = re.fullmatch(r"(\d+)\s*h(?:ours?|rs?)?\s*(\d+)\s*m(?:in(?:ute)?s?)?", low)
    if match:
        return timedelta(hours=int(match.group(1)), minutes=int(match.group(2)))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hm])", low)
    if match:
        amount = float(match.group(1))
        return timedelta(hours=amount) if match.group(2) == "h" else timedelta(minutes=amount)
    if re.fullmatch(r"half (?:an?|of an) hour", low):
        return timedelta(minutes=30)
    if re.fullmatch(r"(?:an?|one) hour and a half", low):
        return timedelta(minutes=90)
    match = re.fullmatch(rf"({_NUM})\s*(?:and a half\s+)?({_UNIT})(\s+and a half)?", low)
    if match:
        amount = _number(match.group(1))
        if " and a half" in low:
            amount += 0.5
        return _unit_delta(amount, match.group(2))
    return None


class _Scanner:
    """Finds phrases, remembers their spans, and blanks them so later
    patterns cannot re-read the same characters."""

    def __init__(self, text: str) -> None:
        self.original = text
        self.work = text.lower()
        self.spans: list[tuple[int, int]] = []

    def take(self, pattern: str) -> re.Match[str] | None:
        match = re.search(pattern, self.work)
        if match:
            self.blank(match.start(), match.end())
        return match

    def blank(self, start: int, end: int) -> None:
        self.spans.append((start, end))
        self.work = self.work[:start] + " " * (end - start) + self.work[end:]

    def rest(self) -> str:
        chars = list(self.original)
        for start, end in self.spans:
            for i in range(start, min(end, len(chars))):
                chars[i] = " "
        text = re.sub(r"\s+", " ", "".join(chars)).strip()
        # Drop connective words left dangling at either edge ("... on", "at ...").
        edge = r"(?:on|at|by|from|for|in|the|this|next|until|till|to|due|before|@)"
        previous = None
        while previous != text:
            previous = text
            text = re.sub(rf"^(?:{edge})\b[\s,.:;-]*", "", text, flags=re.I)
            text = re.sub(rf"[\s,.:;-]*\b(?:{edge})$", "", text, flags=re.I)
        return text.strip(" ,.;:-")


def _hour24(hour: int, minute: int, meridiem: str | None, bare: bool = False) -> tuple[int, int] | None:
    if minute > 59 or hour > 24:
        return None
    if meridiem:
        letter = meridiem.replace(".", "")[0]
        if hour < 1 or hour > 12:
            return None
        if letter == "p" and hour != 12:
            hour += 12
        if letter == "a" and hour == 12:
            hour = 0
    elif bare and 1 <= hour <= 6:
        hour += 12  # "at 3" in a working day means 15:00
    if hour == 24:
        hour = 0
    return hour, minute


def _next_weekday(today: date, weekday: int, modifier: str | None, later_today_ok: bool) -> date:
    ahead = (weekday - today.weekday()) % 7
    if modifier == "next":
        # the named day in the following Monday-based week
        monday_next = today + timedelta(days=7 - today.weekday())
        return monday_next + timedelta(days=weekday)
    if ahead == 0 and not later_today_ok:
        ahead = 7
    return today + timedelta(days=ahead)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _roll_year(candidate: date | None, today: date, explicit_year: bool) -> date | None:
    """A date without a year means its next occurrence."""
    if candidate is None or explicit_year:
        return candidate
    if candidate < today - timedelta(days=1):
        return _safe_date(candidate.year + 1, candidate.month, candidate.day)
    return candidate


def _year(token: str | None, today: date) -> tuple[int, bool]:
    if not token:
        return today.year, False
    value = int(token)
    if value < 100:
        value += 2000
    return value, True


def parse_when(text: str, now: datetime, date_order: str = "dmy", prefer_future: bool = True) -> When | None:
    """Find a date, time and/or duration anywhere in ``text``.

    Returns None when the text contains no time expression at all.
    """
    if not text or not text.strip():
        return None
    scan = _Scanner(text)
    today = now.date()
    found_date: date | None = None
    start_time: tuple[int, int] | None = None
    end_time: tuple[int, int] | None = None
    duration: timedelta | None = None
    relative: datetime | None = None
    relative_has_time = False

    # --- duration: "for 45 minutes", "for an hour and a half" -------------
    match = scan.take(
        rf"\bfor\s+((?:{_NUM})\s*(?:{_UNIT})(?:\s+and\s+a\s+half)?|half\s+an?\s+hour|"
        rf"(?:an?|one)\s+hour\s+and\s+a\s+half|\d+\s*h(?:ours?|rs?)?\s*\d+\s*m(?:ins?)?|\d+(?:\.\d+)?\s*[hm])\b"
    )
    if match:
        duration = parse_duration(match.group(1))

    # --- relative offsets: "in 20 minutes", "in a couple of hours" --------
    match = scan.take(rf"\b(?:in|after)\s+(?:about\s+)?(half\s+an?|(?:{_NUM}))(?:\s+of)?\s*({_UNIT})\b")
    if match:
        head = match.group(1)
        amount = 0.5 if head.startswith("half") else _number(head)
        unit = match.group(2)
        relative = now + _unit_delta(amount, unit)
        relative_has_time = unit.startswith(("sec", "min", "hour", "hr"))
    else:
        match = scan.take(r"\bin\s+half\s+an?\s+hour\b")
        if match:
            relative = now + timedelta(minutes=30)
            relative_has_time = True

    # --- clock ranges: "3-4pm", "from 10:30 to 11", "between 2 and 3 pm" --
    range_pattern = (
        rf"\b(?:from\s+|between\s+)?(\d{{1,2}})(?:[:.](\d{{2}}))?\s*({_AMPM})?\s*"
        rf"(?:-|–|—|to|until|till|and)\s*(\d{{1,2}})(?:[:.](\d{{2}}))?\s*({_AMPM})?(?=\W|$)"
    )
    for match in re.finditer(range_pattern, scan.work):
        h1, m1, ap1, h2, m2, ap2 = match.groups()
        phrase = match.group(0)
        anchored = bool(ap1 or ap2 or m1 or m2) or phrase.startswith(("from", "between"))
        if not anchored:
            continue
        ap1 = ap1 or ap2
        first = _hour24(int(h1), int(m1 or 0), ap1, bare=not ap1)
        second = _hour24(int(h2), int(m2 or 0), ap2 or ap1, bare=not (ap2 or ap1))
        if first is None or second is None:
            continue
        if ap2 and not match.group(3) and first > second:
            # "11-1pm": the first hour belongs to the morning
            first = _hour24(int(h1), int(m1 or 0), "am")
        if first is None:
            continue
        start_time, end_time = first, second
        scan.blank(match.start(), match.end())
        break

    # --- single clock times ------------------------------------------------
    explicit_meridiem = start_time is not None
    if start_time is None:
        match = scan.take(rf"\b(?:at\s+|@\s*)?(\d{{1,2}})[:.](\d{{2}})\s*({_AMPM})(?=\W|$)")
        if match:
            start_time = _hour24(int(match.group(1)), int(match.group(2)), match.group(3))
            explicit_meridiem = True
    if start_time is None:
        match = scan.take(rf"\b(?:at\s+|@\s*)?(\d{{1,2}})\s*({_AMPM})(?=\W|$)")
        if match:
            start_time = _hour24(int(match.group(1)), 0, match.group(2))
            explicit_meridiem = True
    if start_time is None:
        match = scan.take(r"\b(?:at\s+|@\s*)?([01]?\d|2[0-3]):([0-5]\d)\b")
        if match:
            start_time = _hour24(int(match.group(1)), int(match.group(2)), None)
            # 13:00-23:59 and a leading zero are unambiguous 24-hour forms
            explicit_meridiem = int(match.group(1)) >= 13 or match.group(1).startswith("0")
    if start_time is None:
        match = scan.take(r"\b(?:at|@)\s*(\d{1,2})[.]([0-5]\d)\b")
        if match:
            start_time = _hour24(int(match.group(1)), int(match.group(2)), None, bare=True)
    if start_time is None:
        match = scan.take(r"\b(\d{1,2})\s*o'?\s?clock\b")
        if match:
            start_time = _hour24(int(match.group(1)), 0, None, bare=True)
    if start_time is None:
        match = scan.take(r"\b(?:at|@)\s*(\d{1,2})(?!\s*(?:/|-\d|\.\d|\s*(?:" + _MONTH + r")\b))\b")
        if match:
            start_time = _hour24(int(match.group(1)), 0, None, bare=True)

    # A bare hour ("at 7") takes its half of the day from the words around it.
    if start_time is not None and not explicit_meridiem and end_time is None and 1 <= start_time[0] <= 11:
        if re.search(r"\b(?:evening|tonight|night|dinner|supper|drinks|party|movie)\b", scan.work):
            start_time = (start_time[0] + 12, start_time[1])
        elif re.search(r"\bafternoon\b", scan.work) and start_time[0] <= 6:
            start_time = (start_time[0] + 12, start_time[1])
        elif re.search(r"\b(?:morning|breakfast)\b", scan.work) and start_time[0] >= 13:
            start_time = (start_time[0] - 12, start_time[1])
    elif start_time is not None and not explicit_meridiem and end_time is None and 13 <= start_time[0] <= 18:
        if re.search(r"\b(?:morning|breakfast)\b", scan.work):
            start_time = (start_time[0] - 12, start_time[1])

    # --- calendar dates ------------------------------------------------------
    match = scan.take(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
    if match:
        found_date = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if found_date is None:
        match = scan.take(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?(?:,?\s+(\d{{4}}))?")
        if match:
            year, explicit = _year(match.group(3), today)
            found_date = _roll_year(_safe_date(year, MONTHS[match.group(2)], int(match.group(1))), today, explicit)
    if found_date is None:
        match = scan.take(rf"\b({_MONTH})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
        if match:
            year, explicit = _year(match.group(3), today)
            found_date = _roll_year(_safe_date(year, MONTHS[match.group(1)], int(match.group(2))), today, explicit)
    if found_date is None:
        # "14/11" needs no year; dashes and dots do, so that "3-4" (a range)
        # and "2.5" (a decimal) are never mistaken for dates.
        match = (
            scan.take(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")
            or scan.take(r"\b(\d{1,2})-(\d{1,2})-(\d{2}|\d{4})\b")
            or scan.take(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\b")
        )
        if match:
            first, second = int(match.group(1)), int(match.group(2))
            day, month = (first, second) if date_order == "dmy" else (second, first)
            if month > 12 >= day:
                day, month = month, day
            year, explicit = _year(match.group(3), today)
            found_date = _roll_year(_safe_date(year, month, day), today, explicit)

    if found_date is None:
        if scan.take(r"\bday\s+after\s+tomorrow\b"):
            found_date = today + timedelta(days=2)
        elif scan.take(r"\b(?:tomorrow|tmrw|tmr|tomo|2moro)\b"):
            found_date = today + timedelta(days=1)
        elif scan.take(r"\byesterday\b"):
            found_date = today - timedelta(days=1)
        elif scan.take(r"\btoday\b"):
            found_date = today
        elif re.search(r"\btonight\b", scan.work):
            found_date = today  # the word itself is consumed below as a day part

    if found_date is None:
        match = scan.take(rf"\b(?:(next|this|coming|on)\s+)?({_WDAY})\b")
        if match:
            modifier = match.group(1)
            later_ok = start_time is not None and (start_time > (now.hour, now.minute))
            found_date = _next_weekday(today, WEEKDAYS[match.group(2)], modifier, later_ok)

    if found_date is None:
        if scan.take(r"\bnext\s+week\b"):
            found_date = today + timedelta(days=7 - today.weekday())
        elif scan.take(r"\b(?:this\s+)?weekend\b"):
            found_date = _next_weekday(today, 5, None, True)
        elif scan.take(r"\bnext\s+month\b"):
            year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            found_date = date(year, month, 1)
        elif scan.take(r"\b(?:end\s+of\s+(?:the\s+)?week|eow)\b"):
            found_date = _next_weekday(today, 4, None, True)
            start_time = start_time or (18, 0)
        elif scan.take(r"\bend\s+of\s+(?:the\s+)?month\b"):
            year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            found_date = date(year, month, 1) - timedelta(days=1)
        elif scan.take(r"\bend\s+of\s+(?:the\s+)?day\b"):
            found_date = today
            start_time = start_time or (18, 0)

    if found_date is None:
        match = scan.take(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b")
        if match:
            day = int(match.group(1))
            candidate = _safe_date(today.year, today.month, day)
            if candidate is None or candidate < today:
                year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                candidate = _safe_date(year, month, day)
            found_date = candidate

    # --- day parts: "tomorrow morning", "tonight", "noon" -----------------
    if start_time is None:
        match = scan.take(r"\b(?:this\s+|in\s+the\s+|at\s+)?(" + "|".join(DAYPARTS) + r")\b")
        if match:
            start_time = DAYPARTS[match.group(1)]
            if match.group(1) == "midnight" and found_date is None:
                found_date = today + timedelta(days=1)
    else:
        scan.take(r"\b(?:this\s+|in\s+the\s+)?(?:morning|afternoon|evening|tonight|night)\b")

    if relative is None and found_date is None and start_time is None:
        return None  # a bare duration ("for 2 weeks") says nothing about when

    tz = now.tzinfo
    has_date = found_date is not None or relative is not None
    has_time = start_time is not None or relative_has_time

    if relative is not None and found_date is None and start_time is None:
        start = relative if relative_has_time else relative.replace(hour=9, minute=0, second=0, microsecond=0)
        if relative_has_time:
            start = start.replace(second=0, microsecond=0)
    else:
        base_date = found_date or (relative.date() if relative else today)
        if start_time is not None:
            start = datetime.combine(base_date, time(*start_time), tzinfo=tz)
            if found_date is None and relative is None and prefer_future and start <= now:
                start += timedelta(days=1)
        else:
            start = datetime.combine(base_date, time(0, 0), tzinfo=tz)

    end: datetime | None = None
    if end_time is not None:
        end = datetime.combine(start.date(), time(*end_time), tzinfo=tz)
        if end <= start:
            end += timedelta(hours=12) if end + timedelta(hours=12) > start else timedelta(days=1)
    elif duration is not None:
        end = start + duration

    return When(start=start, end=end, has_date=has_date, has_time=has_time, duration=duration, rest=scan.rest())


def parse_range(text: str, now: datetime, date_order: str = "dmy") -> tuple[datetime, datetime, str] | None:
    """Turn 'today', 'this week', 'next 3 days', 'Friday', '12 Nov' into a
    half-open [start, end) window plus a label for display."""
    low = (text or "").lower()
    tz = now.tzinfo
    today = now.date()

    def day_start(d: date) -> datetime:
        return datetime.combine(d, time(0, 0), tzinfo=tz)

    match = re.search(rf"\b(?:next|coming|upcoming)\s+({_NUM})\s+(days?|weeks?|hours?)\b", low)
    if match:
        delta = _unit_delta(_number(match.group(1)), match.group(2))
        if match.group(2).startswith("hour"):
            return now, now + delta, f"next {match.group(1)} {match.group(2)}"
        return day_start(today), day_start(today) + delta + timedelta(days=0), f"next {match.group(1)} {match.group(2)}"
    match = re.search(rf"\b(?:last|past|previous)\s+({_NUM})\s+(days?|weeks?|hours?)\b", low)
    if match:
        delta = _unit_delta(_number(match.group(1)), match.group(2))
        if match.group(2).startswith("hour"):
            return now - delta, now, f"last {match.group(1)} {match.group(2)}"
        return day_start(today) - delta + timedelta(days=1), day_start(today) + timedelta(days=1), f"last {match.group(1)} {match.group(2)}"
    if re.search(r"\bthis\s+week\b", low):
        monday = today - timedelta(days=today.weekday())
        return day_start(monday), day_start(monday + timedelta(days=7)), "this week"
    if re.search(r"\bnext\s+week\b", low):
        monday = today + timedelta(days=7 - today.weekday())
        return day_start(monday), day_start(monday + timedelta(days=7)), "next week"
    if re.search(r"\blast\s+week\b", low):
        monday = today - timedelta(days=today.weekday() + 7)
        return day_start(monday), day_start(monday + timedelta(days=7)), "last week"
    if re.search(r"\bthis\s+month\b", low):
        first = today.replace(day=1)
        nxt = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
        return day_start(first), day_start(nxt), "this month"
    if re.search(r"\b(?:this\s+)?weekend\b", low):
        saturday = _next_weekday(today, 5, None, True)
        if today.weekday() == 6:
            saturday = today - timedelta(days=1)
        return day_start(saturday), day_start(saturday + timedelta(days=2)), "the weekend"
    when = parse_when(text, now, date_order=date_order, prefer_future=False)
    if when is None or not when.has_date:
        return None
    start = day_start(when.start.date())
    label = _day_label(when.start.date(), today)
    return start, start + timedelta(days=1), label


def _day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return day.strftime("%A %d %b")


def parse_datetime_loose(value: str, now: datetime, date_order: str = "dmy") -> datetime | None:
    """Accept ISO 8601 first (what a language model sends), then free text."""
    if not value:
        return None
    text = value.strip()
    try:
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed
    except ValueError:
        pass
    when = parse_when(text, now, date_order=date_order)
    return when.start if when else None


def format_when(start: datetime, end: datetime | None, now: datetime, all_day: bool = False) -> str:
    """Compact human form: 'today 3:00-3:45 PM', 'Fri 14 Nov, all day'."""
    day = _day_label(start.date(), now.date())
    if day not in ("today", "tomorrow", "yesterday"):
        day = start.strftime("%a %d %b")
        if start.year != now.year:
            day += f" {start.year}"
    if all_day:
        return f"{day}, all day"
    clock = _clock(start)
    if end and end.date() == start.date():
        return f"{day} {clock}–{_clock(end)}"
    if end:
        return f"{day} {clock} – {end.strftime('%a %d %b')} {_clock(end)}"
    return f"{day} {clock}"


def _clock(moment: datetime) -> str:
    text = moment.strftime("%I:%M %p").lstrip("0")
    return text.replace(":00 ", " ")
