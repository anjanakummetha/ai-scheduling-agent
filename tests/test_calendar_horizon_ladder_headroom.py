"""The loaded calendar must outrun the fallback ladder.

resolve_calendar_horizon_days trims the horizon to just past the requested
window — sensible, until the requested week is too full and propose_meeting_slots
retries at +1w/+2w/+3w. Those retries can only offer times the loaded context
covers, so the trim left them searching dates with no calendar data, finding
nothing, and escalating.

This was a regression from teaching the parser calendar dates: "week of the
17th" used to parse to None, which skipped the trim entirely.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduling.calendar_intelligence import resolve_calendar_horizon_days

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 3, 9, 0, tzinfo=MT)  # a Monday


def _days(body: str) -> int:
    return resolve_calendar_horizon_days(subject="Coffee", body=body, now=NOW)


def test_single_week_request_still_covers_three_weeks_past_it() -> None:
    # "week of the 17th" ends Aug 23 — 20 days out. Without headroom the context
    # stopped at ~22 days and the +1w/+2w/+3w retries saw an empty calendar.
    days = _days("Could we grab coffee the week of the 17th?")
    assert days >= 44, f"only {days} days loaded — the ladder cannot reach +3w"


def test_next_week_request_covers_the_ladder() -> None:
    days = _days("Coffee next week?")
    assert days >= 37, f"only {days} days loaded — +3w from next week is unreachable"


def test_horizon_still_has_a_floor() -> None:
    assert _days("Coffee tomorrow?") >= 7


def test_horizon_is_still_bounded() -> None:
    from app.config import settings

    for body in ("Coffee the week of the 17th?", "Coffee next week?", "Coffee in September?"):
        assert _days(body) <= settings.lexi_calendar_search_days_max
