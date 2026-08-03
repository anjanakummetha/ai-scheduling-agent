"""Windows stated as calendar dates, not relative phrases.

Regression cover for the LT-B1 defect: a sender wrote "the week of the 10th"
and every offer landed three weeks out, because infer_scheduling_window only
understood "this week"/"next week" and returned None for anything else — which
the slot engine reads as "no constraint, use the full 60-120 day horizon".
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.scheduling_window import infer_scheduling_window

MT = ZoneInfo("America/Denver")
# A Monday, so weekday arithmetic in the assertions is easy to follow.
NOW = datetime(2026, 8, 3, 9, 0, tzinfo=MT)


def _window(text: str):
    return infer_scheduling_window(subject="", body=text, now=NOW)


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        # The exact phrasing that broke LT-B1.
        ("I'm in Denver the week of the 10th", date(2026, 8, 10), date(2026, 8, 16)),
        ("week of August 10", date(2026, 8, 10), date(2026, 8, 16)),
        ("week of Monday the 10th", date(2026, 8, 10), date(2026, 8, 16)),
        # Explicit ranges — the range must win over the single date that starts it.
        ("anytime August 10 to August 14", date(2026, 8, 10), date(2026, 8, 14)),
        ("Aug 10-14 works", date(2026, 8, 10), date(2026, 8, 14)),
        ("August 10 through the 14th", date(2026, 8, 10), date(2026, 8, 14)),
        # Single dates.
        ("how about August 12?", date(2026, 8, 12), date(2026, 8, 12)),
        ("how about the 12th?", date(2026, 8, 12), date(2026, 8, 12)),
        # Weekdays.
        ("does this Thursday work?", date(2026, 8, 6), date(2026, 8, 6)),
        ("next Tuesday", date(2026, 8, 11), date(2026, 8, 11)),
    ],
)
def test_calendar_dates_produce_a_window(phrase, start, end) -> None:
    window = _window(phrase)
    assert window is not None, f"{phrase!r} produced no window — the engine would search unbounded"
    assert (window.start, window.end) == (start, end)


@pytest.mark.parametrize(
    "phrase",
    [
        "do you have 30 minutes sometime?",
        "a 45 minute call would be great",
        "looking for 15 min",
    ],
)
def test_durations_are_not_mistaken_for_dates(phrase) -> None:
    # "30 minutes" must not resolve to the 30th of the month.
    assert _window(phrase) is None


def test_relative_phrases_still_win_over_date_parsing() -> None:
    # "next week" is unambiguous and must not be re-read by the date branches.
    window = _window("next week sometime, maybe the 12th")
    assert window is not None
    assert window.label == "next week"
    assert (window.start, window.end) == (date(2026, 8, 10), date(2026, 8, 16))


def test_impossible_date_does_not_guess() -> None:
    assert _window("how about February 30?") is None


def test_month_day_in_recent_past_stays_this_year() -> None:
    # Written on Aug 3 about "August 1" — this year, not next.
    window = _window("following up on August 1")
    assert window is not None
    assert window.start.year == 2026
