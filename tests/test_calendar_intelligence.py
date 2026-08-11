"""Tests for calendar intelligence (Kory blocking vs kid-only / copies)."""

from __future__ import annotations

from app.scheduling.calendar_intelligence import (
    EventBlockingClass,
    classify_event,
    dedupe_and_filter_blocking_events,
    resolve_calendar_horizon_days,
    resolve_write_calendar_name,
)


def _event(subject: str, *, calendar: str = "Calendar", start: str = "2026-07-01T16:30:00") -> dict:
    return {
        "subject": subject,
        "calendar_name": calendar,
        "start": {"dateTime": start, "timeZone": "America/Denver"},
        "end": {"dateTime": "2026-07-01T17:30:00", "timeZone": "America/Denver"},
        "showAs": "busy",
    }


def test_kid_summer_camp_on_master_does_not_block() -> None:
    classified = classify_event(
        _event(
            "Maclain @ ISD Camp : Mystery Camp (8:30-3:30pm) (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.KID_ONLY_NON_BLOCKING
    assert classified.blocks_kory is False


def test_km_personal_training_blocks() -> None:
    classified = classify_event(
        _event(
            "KM Personal Training Session (copy)",
            calendar="Kory Master Calendar (ALL)",
            start="2026-07-01T08:30:00",
        )
    )
    assert classified.blocking_class == EventBlockingClass.PERSONAL_KORY_BLOCKING
    assert classified.blocks_kory is True


def test_work_hold_on_calendar_blocks() -> None:
    classified = classify_event(_event("HOLD: Intro call w/ Steve"))
    assert classified.blocking_class == EventBlockingClass.WORK_BLOCKING
    assert classified.blocks_kory is True


def test_duplicate_copy_on_master_skipped_when_work_exists() -> None:
    work = _event("HOLD: Intro call w/ Steve", calendar="Calendar")
    copy = _event(
        "HOLD: Intro call w/ Steve (copy)",
        calendar="Kory Master Calendar (ALL)",
    )
    blocking, _ = dedupe_and_filter_blocking_events([work, copy])
    assert len(blocking) == 1
    assert blocking[0]["calendar_name"] == "Calendar"


def test_travel_stay_blocks() -> None:
    classified = classify_event(
        _event(
            "Stay at Comfort Suites Denver",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.TRAVEL_BLOCKING
    assert classified.blocks_kory is True


def test_horizon_extends_for_far_future_email() -> None:
    days = resolve_calendar_horizon_days(
        subject="Meeting in July",
        body="Can we meet late next month?",
    )
    assert days >= 60


def test_horizon_tightens_for_next_week() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("America/Denver"))
    days = resolve_calendar_horizon_days(
        subject="Intro call",
        body="Find me 3 times next week for a 30-minute virtual intro call.",
        now=now,
    )
    # Still far tighter than the max — the point of the trim is not loading a
    # full quarter for a one-week ask. But it must clear the requested week by
    # three weeks, because propose_meeting_slots retries at +1w/+2w/+3w when that
    # week is full and can only offer times the loaded calendar covers.
    from app.config import settings

    assert days >= 7
    assert days < settings.lexi_calendar_search_days_max
    assert days <= 14 + 24


def test_cape_town_tour_on_master_blocks_as_travel() -> None:
    classified = classify_event(
        _event(
            "🏔 Private Cape Town City Tour — Table Mountain + Kirstenbosch (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.TRAVEL_BLOCKING
    assert classified.blocks_kory is True


def test_kory_named_work_meeting_copy_classified_work_then_deduped() -> None:
    classified = classify_event(
        _event(
            "Kory Mitchell and Brad Beldon (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.WORK_BLOCKING
    work = _event("Kory Mitchell and Brad Beldon", calendar="Calendar")
    copy = _event(
        "Kory Mitchell and Brad Beldon (copy)",
        calendar="Kory Master Calendar (ALL)",
    )
    blocking, _ = dedupe_and_filter_blocking_events([work, copy])
    assert len(blocking) == 1


def test_km_prefix_without_work_blocks_personal() -> None:
    classified = classify_event(
        _event(
            "KM dentist appointment (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.PERSONAL_KORY_BLOCKING
    assert classified.blocks_kory is True


def test_km_prefix_with_work_signal_is_work_not_personal() -> None:
    classified = classify_event(
        _event(
            "KM IFG pipeline review (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.WORK_BLOCKING
    assert classified.blocks_kory is True


def test_birthday_on_birthdays_calendar_is_informational() -> None:
    classified = classify_event(
        _event("Birthday: Alice", calendar="Birthdays")
    )
    assert classified.blocking_class == EventBlockingClass.INFORMATIONAL
    assert classified.blocks_kory is False


def test_birthday_on_master_is_informational() -> None:
    classified = classify_event(
        _event(
            "Cody Cornell - Birthday (1980) (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.INFORMATIONAL
    assert classified.blocks_kory is False


def test_doug_only_on_master_blocks() -> None:
    doug = _event(
        "Doug (copy)",
        calendar="Kory Master Calendar (ALL)",
        start="2026-06-09T13:15:00",
    )
    blocking, _ = dedupe_and_filter_blocking_events([doug])
    assert len(blocking) == 1
    assert blocking[0]["blocking_class"] == "work_blocking"


def test_geneva_glen_dropoff_blocks_kory() -> None:
    classified = classify_event(
        _event(
            "Maclain - Geneva Glen Drop Off K+B (copy)",
            calendar="Kory Master Calendar (ALL)",
            start="2026-06-08T10:00:00",
        )
    )
    assert classified.blocking_class == EventBlockingClass.PERSONAL_KORY_BLOCKING
    assert classified.blocks_kory is True


def test_eidson_house_personal_block() -> None:
    classified = classify_event(
        _event("Eidson's house (copy)", calendar="Kory Master Calendar (ALL)")
    )
    assert classified.blocking_class == EventBlockingClass.PERSONAL_KORY_BLOCKING
    assert classified.blocks_kory is True


def test_safari_day_travel_block() -> None:
    classified = classify_event(
        _event(
            "🦁 Africa Safari Day 1 — Arrive Livingstone + Welcome Dinner (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.TRAVEL_BLOCKING
    assert classified.blocks_kory is True


def test_kory_label_on_master_personal() -> None:
    classified = classify_event(
        _event(
            "Kory - Eidson birthday dinner (copy)",
            calendar="Kory Master Calendar (ALL)",
        )
    )
    assert classified.blocking_class == EventBlockingClass.PERSONAL_KORY_BLOCKING
    assert classified.blocks_kory is True


def test_scheduling_holds_write_to_work_calendar() -> None:
    assert resolve_write_calendar_name(intent="coffee") == "Calendar"
    assert resolve_write_calendar_name(intent="lunch") == "Calendar"
    assert resolve_write_calendar_name(intent="happy_hour") == "Calendar"


def test_scheduling_hold_events_block_conflicts() -> None:
    from app.integrations.outlook_calendar import is_blocking_event, is_scheduling_hold

    hold = {
        "subject": "HOLD: Coffee w/ Alejandra Harvey (copy)",
        "showAs": "busy",
        "isCancelled": False,
    }
    assert is_scheduling_hold(hold)
    assert is_blocking_event(hold) is True


def test_all_day_location_markers_do_not_block_timed_meetings() -> None:
    """An all-day entry says where Kory is, not that he is busy.

    An all-day "Kory in Chicago" made every hour of that Thursday unbookable,
    and the confirm-time guard fails closed with no override — so he could not
    place a 1:00pm event at all, even after explicitly saying to go ahead.
    """
    from app.integrations.outlook_calendar import is_blocking_event

    travel = {"subject": "Kory in Chicago", "showAs": "busy", "isCancelled": False, "isAllDay": True}
    assert is_blocking_event(travel) is False

    # Multi-day entries reach us without the isAllDay flag set.
    spanning = {
        "subject": "Kory in Chicago",
        "showAs": "busy",
        "isCancelled": False,
        "start": {"dateTime": "2026-08-13T00:00:00", "timeZone": "America/Denver"},
        "end": {"dateTime": "2026-08-14T00:00:00", "timeZone": "America/Denver"},
    }
    assert is_blocking_event(spanning) is False

    # A real timed meeting still blocks — the guard must stay closed for those.
    timed = {
        "subject": "Doug (Executive Coach)",
        "showAs": "busy",
        "isCancelled": False,
        "start": {"dateTime": "2026-08-13T13:15:00", "timeZone": "America/Denver"},
        "end": {"dateTime": "2026-08-13T14:15:00", "timeZone": "America/Denver"},
    }
    assert is_blocking_event(timed) is True
