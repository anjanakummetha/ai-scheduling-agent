"""Tests for Kory escalation (Heidi path removed 2026-08-04), travel shift, outbound guard."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.safety.outbound_guard import outbound_writes_allowed, teams_push_allowed
from app.scheduling.scheduling_window import SchedulingWindow, infer_scheduling_window
from app.scheduling.scheduling_plan import SchedulingPlan
from app.scheduling.travel_window import (
    maybe_shift_plan_window,
    shift_window_after_travel,
    usable_non_travel_days,
)


MT = ZoneInfo("America/Denver")


def test_infer_two_weeks_window() -> None:
    window = infer_scheduling_window(
        subject="TEST",
        body="Can we meet in two weeks?",
        now=datetime(2026, 6, 10, 10, 0, tzinfo=MT),
    )
    assert window is not None
    assert window.label == "two weeks out"


def test_travel_shift_moves_window_when_trip_consumes_it() -> None:
    """Only defer past travel when the requested window has no usable day left."""
    # Mon-Fri window, travelling the whole work week.
    window = SchedulingWindow(
        start=date(2026, 6, 8),
        end=date(2026, 6, 12),
        source="body",
        label="next week",
    )
    busy = [
        {
            "subject": "Flight to Chicago",
            "start": datetime(2026, 6, 8, 8, 0, tzinfo=MT).isoformat(),
            "end": datetime(2026, 6, 8, 12, 0, tzinfo=MT).isoformat(),
            "blocking_class": "travel_blocking",
        },
        {
            "subject": "Kory in Chicago - All Day",
            "start": datetime(2026, 6, 9, 0, 0, tzinfo=MT).isoformat(),
            "end": datetime(2026, 6, 12, 23, 59, tzinfo=MT).isoformat(),
            "blocking_class": "travel_blocking",
        },
    ]
    shifted = shift_window_after_travel(window, busy, now=datetime(2026, 6, 8, 9, 0, tzinfo=MT))
    assert shifted is not None
    assert shifted.start > date(2026, 6, 12)
    assert "after travel" in shifted.label


def test_partial_travel_keeps_the_requested_window() -> None:
    """Live regression: Kory flew Mon evening and was away Thu-Fri, so a request
    for "next week" was answered with the week *after* — discarding a wide-open
    Tuesday. Travel days are already blocking events, so the engine skips them."""
    window = SchedulingWindow(
        start=date(2026, 7, 27),
        end=date(2026, 8, 2),
        source="body",
        label="next week",
    )
    busy = [
        {
            "subject": "Flight to Sioux City",
            "start": datetime(2026, 7, 27, 21, 5, tzinfo=MT).isoformat(),
            "end": datetime(2026, 7, 27, 22, 41, tzinfo=MT).isoformat(),
            "blocking_class": "travel_blocking",
        },
        {
            "subject": "Kory in CA - All Day",
            "start": datetime(2026, 7, 30, 7, 30, tzinfo=MT).isoformat(),
            "end": datetime(2026, 7, 31, 20, 0, tzinfo=MT).isoformat(),
            "blocking_class": "travel_blocking",
        },
    ]
    now = datetime(2026, 7, 26, 13, 45, tzinfo=MT)
    kept = shift_window_after_travel(window, busy, now=now)
    assert kept == window, "must not defer past travel while Tue/Wed are free"

    plan = SchedulingPlan(task_type="offer_times", window=window, source="rules")
    assert maybe_shift_plan_window(plan, busy, now=now).window == window

    free = usable_non_travel_days(window, busy, today=now.date())
    assert date(2026, 7, 28) in free and date(2026, 7, 29) in free
    assert date(2026, 7, 31) not in free


@patch("app.safety.outbound_guard.settings")
def test_teams_suppressed_when_dry_run(mock_settings) -> None:
    mock_settings.lexi_dry_run = True
    mock_settings.lexi_teams_enabled = True
    mock_settings.lexi_suppress_teams_push = False
    assert outbound_writes_allowed() is False
    assert teams_push_allowed() is False
