from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from felix.timeparse import format_when, parse_datetime_loose, parse_duration, parse_range, parse_when

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)  # a Wednesday


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


@pytest.mark.parametrize(
    "text,start,end,has_time",
    [
        ("tomorrow at 3pm", at(2026, 11, 12, 15), None, True),
        ("tomorrow 3-4pm", at(2026, 11, 12, 15), at(2026, 11, 12, 16), True),
        ("tomorrow from 10:30 to 11", at(2026, 11, 12, 10, 30), at(2026, 11, 12, 11), True),
        ("friday 11-1pm", at(2026, 11, 13, 11), at(2026, 11, 13, 13), True),
        ("today at 5", at(2026, 11, 11, 17), None, True),
        ("at 9", at(2026, 11, 12, 9), None, True),            # 9 already passed today -> tomorrow
        ("at 14:30", at(2026, 11, 11, 14, 30), None, True),
        ("3.30pm", at(2026, 11, 11, 15, 30), None, True),
        ("in 20 minutes", at(2026, 11, 11, 10, 20), None, True),
        ("in an hour", at(2026, 11, 11, 11), None, True),
        ("in half an hour", at(2026, 11, 11, 10, 30), None, True),
        ("in 3 days", at(2026, 11, 14, 9), None, False),
        ("day after tomorrow", at(2026, 11, 13), None, False),
        ("12 nov", at(2026, 11, 12), None, False),
        ("nov 12th at noon", at(2026, 11, 12, 12), None, True),
        ("5th January", at(2027, 1, 5), None, False),          # next occurrence
        ("2026-12-01 09:15", at(2026, 12, 1, 9, 15), None, True),
        ("14/11", at(2026, 11, 14), None, False),
        ("14/11/2026 6pm", at(2026, 11, 14, 18), None, True),
        ("monday", at(2026, 11, 16), None, False),
        ("next friday", at(2026, 11, 20), None, False),
        ("this friday at 4pm", at(2026, 11, 13, 16), None, True),
        ("wednesday at 5pm", at(2026, 11, 11, 17), None, True),  # later today
        ("wednesday at 8am", at(2026, 11, 18, 8), None, True),   # already passed -> next week
        ("tomorrow morning", at(2026, 11, 12, 9), None, True),
        ("tonight", at(2026, 11, 11, 20), None, True),
        ("next week", at(2026, 11, 16), None, False),
        ("end of day", at(2026, 11, 11, 18), None, True),
        ("the 15th", at(2026, 11, 15), None, False),
        ("the 3rd", at(2026, 12, 3), None, False),
        ("10 o'clock", at(2026, 11, 12, 10), None, True),
    ],
)
def test_parse_when(text, start, end, has_time):
    when = parse_when(text, NOW)
    assert when is not None, text
    assert when.start == start, f"{text}: {when.start}"
    assert when.end == end, f"{text}: {when.end}"
    assert when.has_time is has_time, text


def test_duration_sets_end():
    when = parse_when("call with Priya tomorrow at 4pm for 45 minutes", NOW)
    assert when.start == at(2026, 11, 12, 16)
    assert when.end == at(2026, 11, 12, 16, 45)
    assert when.rest == "call with Priya"


@pytest.mark.parametrize(
    "text,rest",
    [
        ("Board review on Friday at 10am", "Board review"),
        ("lunch with investors 14 Nov 1pm for an hour", "lunch with investors"),
        ("remind me to call the bank in 20 minutes", "remind me to call the bank"),
        ("send the deck to Meera by tomorrow", "send the deck to Meera"),
        ("dentist at 5", "dentist"),
    ],
)
def test_rest_is_the_title(text, rest):
    assert parse_when(text, NOW).rest == rest


@pytest.mark.parametrize("text", ["hello there", "what is 2 plus 2", "", "send 3 copies to 4 people"])
def test_no_time_expression(text):
    assert parse_when(text, NOW) is None


def test_mdy_order():
    assert parse_when("03/04", NOW, date_order="mdy").start == at(2027, 3, 4)
    assert parse_when("03/04", NOW, date_order="dmy").start == at(2027, 4, 3)
    # an impossible month flips the fields rather than failing
    assert parse_when("25/12", NOW, date_order="mdy").start == at(2026, 12, 25)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30 min", timedelta(minutes=30)),
        ("1.5 hours", timedelta(minutes=90)),
        ("an hour", timedelta(hours=1)),
        ("half an hour", timedelta(minutes=30)),
        ("an hour and a half", timedelta(minutes=90)),
        ("1h30m", timedelta(minutes=90)),
        ("2 days", timedelta(days=2)),
        ("45m", timedelta(minutes=45)),
        ("soon", None),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text,start,end,label",
    [
        ("what's on today", at(2026, 11, 11), at(2026, 11, 12), "today"),
        ("tomorrow", at(2026, 11, 12), at(2026, 11, 13), "tomorrow"),
        ("this week", at(2026, 11, 9), at(2026, 11, 16), "this week"),
        ("next week", at(2026, 11, 16), at(2026, 11, 23), "next week"),
        ("next 3 days", at(2026, 11, 11), at(2026, 11, 14), "next 3 days"),
        ("on friday", at(2026, 11, 13), at(2026, 11, 14), "Friday 13 Nov"),
        ("this month", at(2026, 11, 1), at(2026, 12, 1), "this month"),
        ("weekend", at(2026, 11, 14), at(2026, 11, 16), "the weekend"),
        # a part of the day narrows the window, and "this ..." means today
        ("this afternoon", at(2026, 11, 11, 12), at(2026, 11, 11, 18), "this afternoon"),
        ("tonight", at(2026, 11, 11, 18), at(2026, 11, 12), "tonight"),
        ("tomorrow morning", at(2026, 11, 12, 5), at(2026, 11, 12, 12), "tomorrow morning"),
        ("friday evening", at(2026, 11, 13, 17), at(2026, 11, 13, 22), "Friday 13 Nov evening"),
    ],
)
def test_parse_range(text, start, end, label):
    assert parse_range(text, NOW) == (start, end, label)


def test_parse_range_none():
    assert parse_range("anything interesting", NOW) is None


def test_parse_datetime_loose_accepts_iso_and_text():
    assert parse_datetime_loose("2026-11-12T15:00:00+05:30", NOW) == at(2026, 11, 12, 15)
    assert parse_datetime_loose("2026-11-12T09:30:00Z", NOW) == at(2026, 11, 12, 15)
    assert parse_datetime_loose("2026-11-12T15:00", NOW) == at(2026, 11, 12, 15)
    assert parse_datetime_loose("tomorrow 3pm", NOW) == at(2026, 11, 12, 15)
    assert parse_datetime_loose("gibberish", NOW) is None


def test_format_when():
    assert format_when(at(2026, 11, 11, 15), at(2026, 11, 11, 15, 45), NOW) == "today 3 PM–3:45 PM"
    assert format_when(at(2026, 11, 14), None, NOW, all_day=True) == "Sat 14 Nov, all day"
    assert format_when(at(2026, 11, 12, 9, 30), None, NOW) == "tomorrow 9:30 AM"
