"""Recurrence rules (RFC 5545 RRULE) - parse and expand, offline.

Covers what business calendars actually use: DAILY / WEEKLY / MONTHLY /
YEARLY with INTERVAL, COUNT, UNTIL, BYDAY (with ordinals such as 2TU or
-1FR), BYMONTHDAY, BYMONTH and BYSETPOS. Expansion happens in the event's own
time zone, so a 9:00 meeting stays at 9:00 across daylight-saving changes.
"""

from __future__ import annotations

import calendar as pycalendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
CODES = {v: k for k, v in WEEKDAY_CODES.items()}
MAX_STEPS = 50000


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    freq: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    byday: list[tuple[int | None, int]] = field(default_factory=list)   # (ordinal, weekday)
    bymonthday: list[int] = field(default_factory=list)
    bymonth: list[int] = field(default_factory=list)
    bysetpos: list[int] = field(default_factory=list)
    wkst: int = 0

    def to_text(self) -> str:
        parts = [f"FREQ={self.freq}"]
        if self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.count is not None:
            parts.append(f"COUNT={self.count}")
        if self.until is not None:
            parts.append("UNTIL=" + self.until.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        if self.byday:
            parts.append("BYDAY=" + ",".join((str(n) if n else "") + CODES[d] for n, d in self.byday))
        if self.bymonthday:
            parts.append("BYMONTHDAY=" + ",".join(map(str, self.bymonthday)))
        if self.bymonth:
            parts.append("BYMONTH=" + ",".join(map(str, self.bymonth)))
        if self.bysetpos:
            parts.append("BYSETPOS=" + ",".join(map(str, self.bysetpos)))
        if self.wkst:
            parts.append(f"WKST={CODES[self.wkst]}")
        return ";".join(parts)


def parse_rrule(text: str, tz=None) -> Rule:
    text = (text or "").strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    fields: dict[str, str] = {}
    for part in text.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip().upper()] = value.strip()
    freq = fields.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise RuleError(f"unsupported recurrence frequency {freq or '(none)'}")
    rule = Rule(freq=freq)
    try:
        rule.interval = max(1, int(fields.get("INTERVAL", "1")))
        if "COUNT" in fields:
            rule.count = max(0, int(fields["COUNT"]))
        if "UNTIL" in fields:
            rule.until = _parse_until(fields["UNTIL"], tz)
        for item in filter(None, fields.get("BYDAY", "").split(",")):
            match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item.strip().upper())
            if not match:
                raise RuleError(f"bad BYDAY value {item!r}")
            rule.byday.append((int(match.group(1)) if match.group(1) else None, WEEKDAY_CODES[match.group(2)]))
        rule.bymonthday = [int(v) for v in filter(None, fields.get("BYMONTHDAY", "").split(","))]
        rule.bymonth = [int(v) for v in filter(None, fields.get("BYMONTH", "").split(","))]
        rule.bysetpos = [int(v) for v in filter(None, fields.get("BYSETPOS", "").split(","))]
        if "WKST" in fields:
            rule.wkst = WEEKDAY_CODES[fields["WKST"].upper()]
    except (ValueError, KeyError) as exc:
        raise RuleError(f"malformed recurrence rule: {text}") from exc
    return rule


def _parse_until(value: str, tz) -> datetime:
    value = value.strip().upper()
    if re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value, "%Y%m%d")
        return day.replace(hour=23, minute=59, second=59, tzinfo=tz or timezone.utc)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz or timezone.utc)


def describe(rule_text: str) -> str:
    """'every 2 weeks on Mon, Wed' - for showing a repeat to a person."""
    try:
        rule = parse_rrule(rule_text)
    except RuleError:
        return "repeats"
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}[rule.freq]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = [d for _, d in rule.byday]
    if rule.freq == "WEEKLY" and sorted(days) == [0, 1, 2, 3, 4] and rule.interval == 1:
        text = "every weekday"
    else:
        text = f"every {unit}" if rule.interval == 1 else f"every {rule.interval} {unit}s"
        if rule.byday:
            ordinal = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}
            text += " on " + ", ".join(
                (f"the {ordinal.get(n, str(n))} " if n else "") + names[d] for n, d in rule.byday)
    if rule.count:
        text += f", {rule.count} times"
    elif rule.until:
        text += f" until {rule.until.strftime('%d %b %Y')}"
    return text


def repeat_to_rrule(word: str) -> str | None:
    """Accept plain words from a person or model: 'weekly', 'weekdays', 'every 2 weeks'."""
    text = (word or "").strip()
    if not text or text.lower() in ("none", "no", "never", "once"):
        return None
    if "FREQ=" in text.upper():
        parse_rrule(text)
        return text[6:] if text.upper().startswith("RRULE:") else text
    low = text.lower()
    simple = {"daily": "FREQ=DAILY", "every day": "FREQ=DAILY", "weekly": "FREQ=WEEKLY", "every week": "FREQ=WEEKLY",
              "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "every weekday": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
              "monthly": "FREQ=MONTHLY", "every month": "FREQ=MONTHLY", "yearly": "FREQ=YEARLY",
              "annually": "FREQ=YEARLY", "every year": "FREQ=YEARLY", "fortnightly": "FREQ=WEEKLY;INTERVAL=2",
              "biweekly": "FREQ=WEEKLY;INTERVAL=2", "quarterly": "FREQ=MONTHLY;INTERVAL=3"}
    if low in simple:
        return simple[low]
    match = re.fullmatch(r"every\s+(\d+|other)\s+(day|week|month|year)s?", low)
    if match:
        interval = 2 if match.group(1) == "other" else int(match.group(1))
        freq = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}[match.group(2)]
        return f"FREQ={freq};INTERVAL={interval}"
    raise RuleError(f"I don't understand the repeat {word!r}. Try daily, weekly, weekdays, monthly or yearly.")


# -- expansion -----------------------------------------------------------------

def expand(start: datetime, rule: Rule, window_start: datetime, window_end: datetime,
           exdates: set[datetime] | None = None, limit: int = 2000) -> list[datetime]:
    """Occurrences of a series that begin in [window_start, window_end).

    ``start`` is the first occurrence (DTSTART) and should be timezone-aware.
    COUNT is honoured from the beginning of the series, not from the window.
    """
    tz = start.tzinfo
    exdates = {_key(d) for d in (exdates or set())}
    clock = start.timetz().replace(tzinfo=None)
    results: list[datetime] = []
    produced = 0
    steps = 0
    for day in _with_first(start.date(), _days(start.date(), rule)):
        steps += 1
        if steps > MAX_STEPS:
            break
        occurrence = datetime.combine(day, clock, tzinfo=tz)
        if occurrence < start:
            continue
        if rule.until is not None and occurrence > rule.until:
            break
        if rule.count is not None and produced >= rule.count:
            break
        produced += 1
        if occurrence >= window_end:
            break
        if occurrence >= window_start and _key(occurrence) not in exdates:
            results.append(occurrence)
            if len(results) >= limit:
                break
    return results


def _with_first(first: date, days):
    """DTSTART is always the first occurrence, even when the rule itself would
    not land on it (Google and Outlook show it too)."""
    iterator = iter(days)
    head = next(iterator, None)
    if head != first:
        yield first
    if head is not None:
        yield head
    yield from iterator


def _key(moment: datetime) -> float:
    return moment.timestamp()


def _days(first: date, rule: Rule):
    """Yield the rule's dates in order, from ``first`` onwards."""
    if rule.freq == "DAILY":
        day = first
        while True:
            if _passes_filters(day, rule):
                yield day
            day += timedelta(days=rule.interval)
    elif rule.freq == "WEEKLY":
        weekdays = sorted({d for _, d in rule.byday}) or [first.weekday()]
        week_start = first - timedelta(days=(first.weekday() - rule.wkst) % 7)
        while True:
            days = sorted(week_start + timedelta(days=(d - rule.wkst) % 7) for d in weekdays)
            for day in days:
                if day >= first and (not rule.bymonth or day.month in rule.bymonth):
                    yield day
            week_start += timedelta(weeks=rule.interval)
    elif rule.freq == "MONTHLY":
        year, month = first.year, first.month
        while True:
            for day in _month_days(year, month, rule, first.day):
                if day >= first:
                    yield day
            month += rule.interval
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
            if year > 9998:
                return
    else:  # YEARLY
        year = first.year
        while True:
            months = sorted(rule.bymonth) or [first.month]
            if rule.byday and not rule.bymonth and any(n for n, _ in rule.byday):
                days = _year_ordinals(year, rule)
            else:
                days = []
                for month in months:
                    days += _month_days(year, month, rule, first.day)
            for day in sorted(set(days)):
                if day >= first:
                    yield day
            year += rule.interval
            if year > 9998:
                return


def _passes_filters(day: date, rule: Rule) -> bool:
    if rule.bymonth and day.month not in rule.bymonth:
        return False
    if rule.byday and day.weekday() not in {d for _, d in rule.byday}:
        return False
    if rule.bymonthday:
        last = pycalendar.monthrange(day.year, day.month)[1]
        allowed = {d if d > 0 else last + 1 + d for d in rule.bymonthday}
        if day.day not in allowed:
            return False
    return True


def _month_days(year: int, month: int, rule: Rule, default_day: int) -> list[date]:
    last = pycalendar.monthrange(year, month)[1]
    days: list[date] = []
    if rule.byday:
        for ordinal, weekday in rule.byday:
            matching = [date(year, month, d) for d in range(1, last + 1) if date(year, month, d).weekday() == weekday]
            if ordinal is None:
                days += matching
            elif 1 <= ordinal <= len(matching):
                days.append(matching[ordinal - 1])
            elif -len(matching) <= ordinal <= -1:
                days.append(matching[ordinal])
        if rule.bymonthday:
            allowed = {d if d > 0 else last + 1 + d for d in rule.bymonthday}
            days = [d for d in days if d.day in allowed]
    elif rule.bymonthday:
        for value in rule.bymonthday:
            day = value if value > 0 else last + 1 + value
            if 1 <= day <= last:
                days.append(date(year, month, day))
    elif default_day <= last:
        days.append(date(year, month, default_day))
    days = sorted(set(days))
    if rule.bysetpos:
        picked = []
        for position in rule.bysetpos:
            if 1 <= position <= len(days):
                picked.append(days[position - 1])
            elif -len(days) <= position <= -1:
                picked.append(days[position])
        days = sorted(set(picked))
    return days


def _year_ordinals(year: int, rule: Rule) -> list[date]:
    """YEARLY with BYDAY ordinals and no BYMONTH: the nth weekday of the year."""
    out: list[date] = []
    first, last = date(year, 1, 1), date(year, 12, 31)
    for ordinal, weekday in rule.byday:
        matching = []
        day = first + timedelta(days=(weekday - first.weekday()) % 7)
        while day <= last:
            matching.append(day)
            day += timedelta(days=7)
        if ordinal is None:
            out += matching
        elif 1 <= ordinal <= len(matching):
            out.append(matching[ordinal - 1])
        elif -len(matching) <= ordinal <= -1:
            out.append(matching[ordinal])
    return out

