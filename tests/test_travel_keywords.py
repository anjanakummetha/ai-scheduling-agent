"""Recurring meetings must not read as travel.

The LT-B1 root cause: "check-in" was a travel keyword, so
"IFG + Sujash | Check-in (Mon+Wed+Fri)" and the biweekly check-ins marked most
weekdays as travel. With no usable non-travel day left, the window shifted a
week out — and a sender who asked for the week of the 10th was offered the 18th.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.scheduling_window import SchedulingWindow
from app.scheduling.travel_window import (
    _is_travel_event,
    shift_window_after_travel,
    travel_date_set,
)

MT = ZoneInfo("America/Denver")


def _event(subject: str, day: str, start="09:00", end="10:00") -> dict:
    return {
        "subject": subject,
        "start": {"dateTime": f"{day}T{start}:00", "timeZone": "America/Denver"},
        "end": {"dateTime": f"{day}T{end}:00", "timeZone": "America/Denver"},
    }


@pytest.mark.parametrize(
    "subject",
    [
        "IFG + Sujash | Check-in (Mon+Wed+Fri)",
        "Kory + Dan Phillips | biweekly check-in",
        "Kory/Jason/Heidi | Biweekly check-in [Marketing]",
        "Weekly check-in",
    ],
)
def test_recurring_checkins_are_not_travel(subject) -> None:
    assert not _is_travel_event(_event(subject, "2026-08-10"))


@pytest.mark.parametrize(
    "subject",
    [
        "Flight to Houston (UA 2791)",
        "Kory in Chicago",
        "Hotel check-in",
        "Flight check-in",
    ],
)
def test_real_travel_still_detected(subject) -> None:
    assert _is_travel_event(_event(subject, "2026-08-10"))


def test_checkin_meetings_do_not_consume_the_week() -> None:
    busy = [
        _event("IFG + Sujash | Check-in (Mon+Wed+Fri)", "2026-08-10"),
        _event("IFG + Sujash | Check-in (Mon+Wed+Fri)", "2026-08-12"),
        _event("IFG + Sujash | Check-in (Mon+Wed+Fri)", "2026-08-14"),
        _event("Kory + Dan Phillips | biweekly check-in", "2026-08-11"),
    ]
    assert travel_date_set(busy) == set()


def test_window_is_not_shifted_by_checkin_meetings() -> None:
    window = SchedulingWindow(
        start=date(2026, 8, 10), end=date(2026, 8, 16), source="body", label="week of August 10"
    )
    busy = [
        _event("IFG + Sujash | Check-in (Mon+Wed+Fri)", "2026-08-10"),
        _event("IFG + Sujash | Check-in (Mon+Wed+Fri)", "2026-08-12"),
        _event("IFG + Sujash | Check-in (Mon+Wed+Fri)", "2026-08-14"),
    ]
    result = shift_window_after_travel(
        window, busy, now=datetime(2026, 8, 3, 9, 0, tzinfo=MT)
    )
    assert result == window, "requested window must survive recurring check-ins"


def test_genuine_full_week_travel_still_shifts() -> None:
    # V-3: shifting is correct only when no usable non-travel weekday remains.
    # Real trips are flagged by blocking_class, not the subject heuristic —
    # "Kory in Houston" matches no keyword, which is why the list can be narrowed.
    window = SchedulingWindow(
        start=date(2026, 8, 10), end=date(2026, 8, 16), source="body", label="week of August 10"
    )
    busy = [
        {**_event("Kory in Houston", f"2026-08-{d}", "00:00", "23:59"),
         "blocking_class": "travel_blocking"}
        for d in (10, 11, 12, 13, 14)
    ]
    result = shift_window_after_travel(
        window, busy, now=datetime(2026, 8, 3, 9, 0, tzinfo=MT)
    )
    assert result is not None
    assert result.source == "travel_shift"
    assert result.start > window.start
