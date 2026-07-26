"""Travel must not blanket-block workdays.

Built from Kory's real calendar for the week of 2026-07-27, which is what
exposed the bug: a 9:05 PM flight and a recurring meeting whose title contains
"Check-in" made Mon-Wed unschedulable, so a request for "next week" produced
nothing but Tuesday.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.rules.validators import validate_proposal_slots

MT = ZoneInfo("America/Denver")


def _ev(day: int, start: str, end: str, subject: str, cls: str = "work_blocking") -> dict:
    return {
        "subject": subject,
        "blocking_class": cls,
        "start": {"dateTime": f"2026-07-{day:02d}T{start}:00", "timeZone": "America/Denver"},
        "end": {"dateTime": f"2026-07-{day:02d}T{end}:00", "timeZone": "America/Denver"},
    }


BUSY = [
    # Monday: trainer, meetings, and an evening flight out.
    _ev(27, "06:30", "08:00", "KM Personal Training Session", "personal_kory_blocking"),
    _ev(27, "09:00", "10:00", "IFG Deal + Pipeline Review"),
    _ev(27, "10:00", "14:00", "WOB"),
    _ev(27, "16:00", "16:30", "IFG + Sujash | Check-in (Mon+Wed+Fri)"),
    _ev(27, "21:05", "22:41", "Flight to Sioux City (UA 5101)", "travel_blocking"),
    # Wednesday: no travel at all, but has a "Check-in" meeting.
    _ev(29, "06:30", "08:30", "KM Personal Training Session", "personal_kory_blocking"),
    _ev(29, "10:00", "12:00", "IFG | Weekly Stand Up"),
    _ev(29, "16:00", "16:30", "IFG + Sujash | Check-in (Mon+Wed+Fri)"),
    # Thursday: genuinely away all day.
    _ev(30, "07:30", "20:00", "Kory in CA - All Day", "travel_blocking"),
]


def _check(day: int, hour: int, minute: int = 0):
    start = datetime(2026, 7, day, hour, minute, tzinfo=MT)
    slot = {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}
    return validate_proposal_slots(
        [slot],
        intent="referral_or_intro",
        meeting_format="virtual",
        urgent=False,
        busy_events=BUSY,
        batch_slots=[slot],
    )


def test_morning_is_bookable_before_an_evening_flight():
    assert _check(27, 8).valid, "8:00 AM is 13 hours before a 9:05 PM flight"


def test_hours_around_the_flight_stay_blocked():
    assert not _check(27, 19).valid, "19:00 is inside the pre-flight buffer"


def test_day_with_no_travel_is_not_a_travel_day():
    """'Check-in' in a recurring meeting title used to mean hotel check-in."""
    for hour in (9, 13):
        assert _check(29, hour).valid, f"Wednesday {hour}:00 has no travel event"


def test_all_day_travel_still_blocks_the_whole_day():
    for hour in (9, 13, 16):
        assert not _check(30, hour).valid, "Kory is in CA all day Thursday"


def test_travel_violation_names_travel():
    res = _check(30, 9)
    assert any("traveling" in v for v in res.violations), res.violations
