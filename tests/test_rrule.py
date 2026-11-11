"""Recurrence expansion, cross-checked against python-dateutil."""

import random
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from felix.connectors.rrule import RuleError, describe, expand, parse_rrule, repeat_to_rrule

dateutil_rrule = pytest.importorskip("dateutil.rrule")

IST = ZoneInfo("Asia/Kolkata")
NYC = ZoneInfo("America/New_York")

# (DTSTART, RRULE) pairs whose first occurrence satisfies the rule, as real calendars produce.
SERIES = [
    (datetime(2026, 1, 5, 9, 30, tzinfo=IST), "FREQ=DAILY"),
    (datetime(2026, 1, 5, 9, 30, tzinfo=IST), "FREQ=DAILY;INTERVAL=3;COUNT=40"),
    (datetime(2026, 1, 5, 9, 30, tzinfo=IST), "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"),
    (datetime(2026, 1, 5, 10, 0, tzinfo=IST), "FREQ=WEEKLY"),
    (datetime(2026, 1, 5, 10, 0, tzinfo=IST), "FREQ=WEEKLY;BYDAY=MO,WE,FR"),
    (datetime(2026, 1, 7, 10, 0, tzinfo=IST), "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR"),
    (datetime(2026, 1, 4, 10, 0, tzinfo=IST), "FREQ=WEEKLY;INTERVAL=2;BYDAY=SU,TU;WKST=SU"),
    (datetime(2026, 1, 5, 10, 0, tzinfo=IST), "FREQ=WEEKLY;BYDAY=MO;UNTIL=20260630T183000Z"),
    (datetime(2026, 1, 5, 10, 0, tzinfo=IST), "FREQ=WEEKLY;BYDAY=MO,TH;COUNT=17"),
    (datetime(2026, 1, 15, 14, 0, tzinfo=IST), "FREQ=MONTHLY"),
    (datetime(2026, 1, 31, 14, 0, tzinfo=IST), "FREQ=MONTHLY"),                       # skips short months
    (datetime(2026, 1, 13, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYDAY=2TU"),             # second Tuesday
    (datetime(2026, 1, 30, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYDAY=-1FR"),            # last Friday
    (datetime(2026, 1, 5, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYDAY=1MO;INTERVAL=3"),   # quarterly, first Monday
    (datetime(2026, 1, 31, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYMONTHDAY=-1"),         # last day of month
    (datetime(2026, 1, 1, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYMONTHDAY=1,15"),
    (datetime(2026, 1, 30, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"),  # last weekday
    (datetime(2026, 1, 1, 14, 0, tzinfo=IST), "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1"),    # first weekday
    (datetime(2026, 3, 14, 8, 0, tzinfo=IST), "FREQ=YEARLY"),
    (datetime(2024, 2, 29, 8, 0, tzinfo=IST), "FREQ=YEARLY"),                         # leap day
    (datetime(2026, 4, 1, 8, 0, tzinfo=IST), "FREQ=YEARLY;BYMONTH=4,10"),             # half-yearly
    (datetime(2026, 11, 26, 8, 0, tzinfo=IST), "FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"),   # fourth Thursday of November
    (datetime(2026, 1, 5, 9, 0, tzinfo=NYC), "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),     # across DST changes
    (datetime(2026, 3, 1, 2, 30, tzinfo=NYC), "FREQ=DAILY;COUNT=20"),                 # through the spring-forward gap
    (datetime(2026, 10, 25, 1, 30, tzinfo=NYC), "FREQ=DAILY;COUNT=14"),               # through the fall-back overlap
]


def reference(start, rule_text, window_start, window_end):
    rule = dateutil_rrule.rrulestr(rule_text, dtstart=start)
    return [d for d in rule.between(window_start - timedelta(seconds=1), window_end, inc=False)
            if window_start <= d < window_end]


@pytest.mark.parametrize("start,rule_text", SERIES)
def test_matches_dateutil(start, rule_text):
    rng = random.Random(zlib.crc32(rule_text.encode()))
    windows = [(start - timedelta(days=3), start + timedelta(days=400))]
    for _ in range(6):
        begin = start + timedelta(days=rng.randint(-30, 700), hours=rng.randint(0, 23))
        windows.append((begin, begin + timedelta(days=rng.choice([1, 7, 31, 120]))))
    for window_start, window_end in windows:
        ours = expand(start, parse_rrule(rule_text, start.tzinfo), window_start, window_end)
        theirs = reference(start, rule_text, window_start, window_end)
        # the spring-forward gap makes "02:30" ambiguous in representation; compare instants
        assert [d.timestamp() for d in ours] == [d.timestamp() for d in theirs], (rule_text, window_start)


def test_wall_clock_time_survives_dst():
    start = datetime(2026, 3, 2, 9, 0, tzinfo=NYC)      # Monday before the US clocks change
    days = expand(start, parse_rrule("FREQ=WEEKLY"), start, start + timedelta(days=30))
    assert [d.hour for d in days] == [9, 9, 9, 9, 9]
    offsets = {d.utcoffset() for d in days}
    assert offsets == {timedelta(hours=-5), timedelta(hours=-4)}


def test_count_is_counted_from_the_series_start():
    start = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    rule = parse_rrule("FREQ=WEEKLY;COUNT=4")
    late_window = expand(rule=rule, start=start, window_start=start + timedelta(days=20), window_end=start + timedelta(days=90))
    assert [d.day for d in late_window] == [26]           # only the 4th occurrence remains
    assert expand(start, rule, start + timedelta(days=40), start + timedelta(days=90)) == []


def test_exdates_remove_single_occurrences():
    start = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    skipped = datetime(2026, 1, 12, 4, 30, tzinfo=timezone.utc)   # the same instant, expressed in UTC
    days = expand(start, parse_rrule("FREQ=WEEKLY;COUNT=4"), start, start + timedelta(days=60), exdates={skipped})
    assert [d.day for d in days] == [5, 19, 26]


def test_first_occurrence_is_kept_even_when_off_rule():
    start = datetime(2026, 1, 6, 10, 0, tzinfo=IST)                 # a Tuesday
    days = expand(start, parse_rrule("FREQ=WEEKLY;BYDAY=MO;COUNT=3"), start, start + timedelta(days=30))
    assert [(d.day, d.strftime("%a")) for d in days] == [(6, "Tue"), (12, "Mon"), (19, "Mon")]


def test_until_date_only_is_inclusive():
    start = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    days = expand(start, parse_rrule("FREQ=DAILY;UNTIL=20260107", IST), start, start + timedelta(days=30))
    assert [d.day for d in days] == [5, 6, 7]


def test_parse_errors_and_round_trip():
    with pytest.raises(RuleError):
        parse_rrule("FREQ=HOURLY")
    with pytest.raises(RuleError):
        parse_rrule("FREQ=WEEKLY;BYDAY=XX")
    text = "FREQ=MONTHLY;INTERVAL=2;COUNT=5;BYDAY=2TU,-1FR"
    assert parse_rrule("RRULE:" + text).to_text() == text


def test_plain_word_repeats():
    assert repeat_to_rrule("weekly") == "FREQ=WEEKLY"
    assert repeat_to_rrule("Weekdays") == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    assert repeat_to_rrule("every other week") == "FREQ=WEEKLY;INTERVAL=2"
    assert repeat_to_rrule("every 3 months") == "FREQ=MONTHLY;INTERVAL=3"
    assert repeat_to_rrule("none") is None and repeat_to_rrule("") is None
    with pytest.raises(RuleError):
        repeat_to_rrule("whenever")


def test_describe():
    assert describe("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "every weekday"
    assert describe("FREQ=MONTHLY;BYDAY=2TU") == "every month on the second Tue"
    assert describe("FREQ=DAILY;INTERVAL=2;COUNT=5") == "every 2 days, 5 times"
