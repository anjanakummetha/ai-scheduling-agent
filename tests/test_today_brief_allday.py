"""Stale all-day events must not appear on today's brief.

Live bug: "Kory in Houston" (all-day Aug 6-7, created in Eastern time) was
listed on Aug 8's today-brief as a "10:00 PM" meeting — Graph's calendarView
returned it even though it never touches the local day.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.assistant.briefings import _event_overlaps_window, _parse_event_boundary

MT = ZoneInfo("America/Denver")
DAY_START = datetime(2026, 8, 8, 0, 0, tzinfo=MT)
DAY_END = datetime(2026, 8, 9, 0, 0, tzinfo=MT)


def _event(start, end, all_day=False):
    return {
        "isAllDay": all_day,
        "start": {"dateTime": start, "timeZone": "America/Denver"},
        "end": {"dateTime": end, "timeZone": "America/Denver"},
    }


def test_yesterdays_allday_event_is_excluded():
    houston = _event("2026-08-06T22:00:00", "2026-08-07T22:00:00", all_day=True)
    assert _event_overlaps_window(houston, DAY_START, DAY_END) is False


def test_todays_meeting_is_kept():
    coffee = _event("2026-08-08T10:00:00", "2026-08-08T11:00:00")
    assert _event_overlaps_window(coffee, DAY_START, DAY_END) is True


def test_multiday_event_spanning_today_is_kept():
    trip = _event("2026-08-07T00:00:00", "2026-08-10T00:00:00", all_day=True)
    assert _event_overlaps_window(trip, DAY_START, DAY_END) is True


def test_unparseable_bounds_are_kept_not_hidden():
    weird = {"start": {"dateTime": "not-a-date"}, "end": None}
    assert _event_overlaps_window(weird, DAY_START, DAY_END) is True


def test_boundary_touching_events_are_excluded():
    # Ends exactly at midnight -> yesterday's event; starts at midnight tomorrow -> tomorrow's.
    ends_at_midnight = _event("2026-08-07T23:00:00", "2026-08-08T00:00:00")
    starts_tomorrow = _event("2026-08-09T00:00:00", "2026-08-09T01:00:00")
    assert _event_overlaps_window(ends_at_midnight, DAY_START, DAY_END) is False
    assert _event_overlaps_window(starts_tomorrow, DAY_START, DAY_END) is False


def test_boundary_parse_honours_stated_zone():
    eastern = _parse_event_boundary({"dateTime": "2026-08-07T00:00:00", "timeZone": "America/New_York"}, MT)
    assert eastern is not None and eastern.utcoffset().total_seconds() == -4 * 3600
