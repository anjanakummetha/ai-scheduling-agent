"""Kory's 2026-08-08 rulings (via Anjana):

1. Urgency in a sender's email NEVER self-relaxes gates (lunch / travel week /
   6 AM). It warns, and a failed urgent request escalates to Kory instead.
2. Tue/Thu early starts (7:00/8:00) only when the contact's schedule needs it
   (East Coast, or a stated early window) — never a routine menu item. The
   6 AM lane is East-Coast only, and "6 AM ET" must be reachable (floor 6:00
   with an East-Coast cue, 7:00 otherwise).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.rules.validators import validate_proposal_slots
from app.scheduling.scheduling_window import infer_time_of_day_window
from app.scheduling.slot_engine import _candidate_start_times

MT = ZoneInfo("America/Denver")

# Tuesday (an EARLY_START_DAY, not a workout day)
TUESDAY = datetime(2026, 8, 11, 0, 0, tzinfo=MT)


def _tue_slot(hhmm: str, minutes: int = 30) -> dict[str, str]:
    h, m = (int(x) for x in hhmm.split(":"))
    start = TUESDAY.replace(hour=h, minute=m)
    from datetime import timedelta

    return {"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat()}


def test_urgent_does_not_unlock_lunch():
    result = validate_proposal_slots(
        [_tue_slot("12:00", 60)], intent="lunch_request", urgent=True
    )
    assert not result.valid
    assert any("exception-only" in v for v in result.violations)
    assert any("no rules were auto-relaxed" in w for w in result.warnings)


def test_urgent_does_not_unlock_6am():
    result = validate_proposal_slots([_tue_slot("06:00")], intent="meeting_request", urgent=True)
    assert not result.valid
    assert any("East Coast" in v for v in result.violations)


def test_east_coast_6am_tue_thu_is_allowed():
    result = validate_proposal_slots(
        [_tue_slot("06:00")], intent="meeting_request", east_coast=True
    )
    assert result.valid, result.violations


def test_tue_thu_early_slots_are_not_routine():
    times = _candidate_start_times(
        TUESDAY, "unknown", "virtual", east_coast=False, urgent=False, early_ok=False
    )
    hhmm = {t.strftime("%H:%M") for t in times}
    assert "07:00" not in hhmm and "08:00" not in hhmm and "06:00" not in hhmm


def test_tue_thu_early_slots_offered_when_contact_needs_them():
    stated_early = _candidate_start_times(
        TUESDAY, "unknown", "virtual", east_coast=False, urgent=False, early_ok=True
    )
    hhmm = {t.strftime("%H:%M") for t in stated_early}
    assert "07:00" in hhmm and "08:00" in hhmm
    assert "06:00" not in hhmm  # 6 AM stays East-Coast-only

    east_coast = _candidate_start_times(
        TUESDAY, "unknown", "virtual", east_coast=True, urgent=False, early_ok=False
    )
    assert "06:00" in {t.strftime("%H:%M") for t in east_coast}


def test_urgent_alone_does_not_add_6am_candidates():
    times = _candidate_start_times(
        TUESDAY, "unknown", "virtual", east_coast=False, urgent=True, early_ok=False
    )
    assert "06:00" not in {t.strftime("%H:%M") for t in times}


def test_6am_et_window_is_reachable_for_east_coast():
    window = infer_time_of_day_window(
        subject="Quick call?",
        body="Early morning works — even 6 am ET is fine on our end (New York).",
    )
    assert window is not None
    assert window.earliest_minutes() == 6 * 60


def test_early_window_floor_stays_7am_without_east_coast_cue():
    window = infer_time_of_day_window(
        subject="Quick call?",
        body="Early morning works — even 6 am is fine on our end.",
    )
    assert window is not None
    assert window.earliest_minutes() == 7 * 60
