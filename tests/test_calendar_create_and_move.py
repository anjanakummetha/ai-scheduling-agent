"""Plain calendar events and moves — the two writes Lexi could not do.

Before these tools existed, `lexi_place_calendar_hold` was the only way onto the
calendar (so "create an event called X" produced "HOLD: X"), and a move fell
through the generic Outlook passthrough, hand-assembling a payload that Graph
accepted and ignored — Lexi reported "Done! shifted to 11:45" while the event
never moved. Both failures presented as success, so every test here asserts on
what the calendar says afterwards, not on what the write call returned.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.assistant import actions
from app.config import settings


def _instant(naive_iso, timezone):
    """The absolute moment a (wall clock, zone) pair refers to."""
    return datetime.fromisoformat(naive_iso).replace(tzinfo=ZoneInfo(timezone))


def _event(event_id, subject, start, end):
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "America/Denver"},
        "end": {"dateTime": end, "timeZone": "America/Denver"},
    }


# ── create ───────────────────────────────────────────────────────────────────


def test_create_uses_the_title_verbatim(live_writes):
    """The whole point of the tool: no canonical HOLD: prefix."""
    created = {}

    def fake_create(action, *, calendar_name=None):
        created.update(action)
        return "evt-1", "log-1"

    observed = _event("evt-1", "TEST move event", "2026-08-12T11:30:00", "2026-08-12T12:00:00")
    with (
        patch.object(actions, "has_conflict", return_value=(False, [], None)),
        patch("app.integrations.named_calendars.create_event_on_calendar", side_effect=fake_create),
        patch("app.integrations.outlook_calendar.get_calendar_event", return_value=observed),
    ):
        result = actions.create_calendar_event(
            title="TEST move event",
            start_iso="2026-08-12T11:30:00",
            end_iso="2026-08-12T12:00:00",
            confirm=True,
        )

    assert created["title"] == "TEST move event"
    assert not created["title"].upper().startswith("HOLD")
    assert result["ok"] is True and result["verified"] is True
    assert result["observed_title"] == "TEST move event"


def test_create_refuses_a_conflict_but_offers_an_override(live_writes):
    busy = [_event("evt-busy", "WOB", "2026-08-12T11:00:00", "2026-08-12T13:00:00")]
    with (
        patch.object(actions, "has_conflict", return_value=(True, busy, None)),
        patch("app.integrations.named_calendars.create_event_on_calendar") as create,
    ):
        result = actions.create_calendar_event(
            title="Coffee", start_iso="2026-08-12T11:30:00",
            end_iso="2026-08-12T12:00:00", confirm=True,
        )
    assert result["ok"] is False
    assert result["conflicting_events"] == busy
    assert "allow_conflict" in result["override"]
    create.assert_not_called()


def test_create_allow_conflict_books_anyway_and_says_so(live_writes):
    busy = [_event("evt-busy", "WOB", "2026-08-12T11:00:00", "2026-08-12T13:00:00")]
    observed = _event("evt-2", "Coffee", "2026-08-12T11:30:00", "2026-08-12T12:00:00")
    with (
        patch.object(actions, "has_conflict", return_value=(True, busy, None)),
        patch("app.integrations.named_calendars.create_event_on_calendar",
              return_value=("evt-2", "log-2")),
        patch("app.integrations.outlook_calendar.get_calendar_event", return_value=observed),
    ):
        result = actions.create_calendar_event(
            title="Coffee", start_iso="2026-08-12T11:30:00", end_iso="2026-08-12T12:00:00",
            allow_conflict=True, confirm=True,
        )
    assert result["ok"] is True
    assert result["double_booked"] is True


def test_create_without_an_event_id_is_not_success(live_writes):
    with (
        patch.object(actions, "has_conflict", return_value=(False, [], None)),
        patch("app.integrations.named_calendars.create_event_on_calendar",
              return_value=(None, "log-3")),
    ):
        result = actions.create_calendar_event(
            title="Ghost", start_iso="2026-08-12T11:30:00",
            end_iso="2026-08-12T12:00:00", confirm=True,
        )
    assert result["ok"] is False
    assert "not created" in result["error"]


def test_create_landing_at_the_wrong_time_is_reported_as_failure(live_writes):
    """Created, but an hour off — that is not 'Done!'."""
    wrong = _event("evt-4", "Coffee", "2026-08-12T12:30:00", "2026-08-12T13:00:00")
    with (
        patch.object(actions, "has_conflict", return_value=(False, [], None)),
        patch("app.integrations.named_calendars.create_event_on_calendar",
              return_value=("evt-4", "log-4")),
        patch("app.integrations.outlook_calendar.get_calendar_event", return_value=wrong),
    ):
        result = actions.create_calendar_event(
            title="Coffee", start_iso="2026-08-12T11:30:00",
            end_iso="2026-08-12T12:00:00", confirm=True,
        )
    assert result["ok"] is False and result["verified"] is False


def test_create_requires_approval():
    from app.config import settings

    object.__setattr__(settings, "lexi_dry_run", False)
    try:
        with pytest.raises(PermissionError):
            actions.create_calendar_event(
                title="Unapproved", start_iso="2026-08-12T11:30:00",
                end_iso="2026-08-12T12:00:00",
            )
    finally:
        object.__setattr__(settings, "lexi_dry_run", True)


# ── move ─────────────────────────────────────────────────────────────────────


def test_move_sends_the_flat_payload_graph_actually_reads(live_writes):
    """Regression guard for the live defect.

    OUTLOOK_UPDATE_CALENDAR_EVENT wants flat start_datetime/end_datetime/time_zone.
    The nested {"start": {"dateTime": ...}} shape used by the create path is
    accepted and silently ignored, which is exactly how the improvised moves
    returned in ~0.00s and changed nothing.
    """
    from app.integrations import outlook_calendar

    sent = {}

    def fake_write(slug, args):
        sent["slug"] = slug
        sent["args"] = args
        return {"data": {"id": "evt-5"}, "successful": True, "log_id": "log-5"}

    observed = _event("evt-5", "Sync", "2026-08-12T11:45:00", "2026-08-12T12:15:00")
    with (
        patch.object(outlook_calendar, "execute_write_tool", side_effect=fake_write),
        patch.object(outlook_calendar, "get_calendar_event", return_value=observed),
    ):
        result = outlook_calendar.move_calendar_event(
            "evt-5", start_iso="2026-08-12T11:45:00", end_iso="2026-08-12T12:15:00"
        )

    assert sent["slug"] == "OUTLOOK_UPDATE_CALENDAR_EVENT"
    # Flat keys, and no nested create-shape leaking in beside them.
    assert set(sent["args"]) == {
        "user_id", "event_id", "start_datetime", "end_datetime", "time_zone",
    }
    # The times are sent in Outlook's zone, so assert on the instant they denote
    # rather than the wall clock — a naive string compare would pass a payload
    # that is silently two hours off.
    assert _instant(sent["args"]["start_datetime"], sent["args"]["time_zone"]) == _instant(
        "2026-08-12T11:45:00", settings.scheduling_timezone
    )
    assert _instant(sent["args"]["end_datetime"], sent["args"]["time_zone"]) == _instant(
        "2026-08-12T12:15:00", settings.scheduling_timezone
    )
    assert result["ok"] is True and result["verified"] is True


def test_move_that_did_not_move_is_reported_as_failure(live_writes):
    """The exact live defect: update accepted, event still at the old time."""
    from app.integrations import outlook_calendar

    stale = _event("evt-6", "Sync", "2026-08-12T11:30:00", "2026-08-12T12:00:00")
    with (
        patch.object(outlook_calendar, "execute_write_tool",
                     return_value={"data": {}, "successful": True, "log_id": "log-6"}),
        patch.object(outlook_calendar, "get_calendar_event", return_value=stale),
    ):
        result = outlook_calendar.move_calendar_event(
            "evt-6", start_iso="2026-08-12T11:45:00", end_iso="2026-08-12T12:15:00"
        )
    assert result["ok"] is False and result["verified"] is False
    assert result["observed"]["start"].startswith("2026-08-12T11:30")


def test_move_honours_successful_false(live_writes):
    """Composio answers 200 with successful=false and no error."""
    from app.integrations import outlook_calendar

    with (
        patch.object(outlook_calendar, "execute_write_tool",
                     return_value={"data": {}, "successful": False, "log_id": "log-7"}),
        patch.object(outlook_calendar, "get_calendar_event") as read_back,
    ):
        result = outlook_calendar.move_calendar_event(
            "evt-7", start_iso="2026-08-12T11:45:00", end_iso="2026-08-12T12:15:00"
        )
    assert result["ok"] is False
    read_back.assert_not_called()


def test_move_that_cannot_be_read_back_is_not_success(live_writes):
    from app.integrations import outlook_calendar

    with (
        patch.object(outlook_calendar, "execute_write_tool",
                     return_value={"data": {}, "successful": True, "log_id": "log-8"}),
        patch.object(outlook_calendar, "get_calendar_event", return_value=None),
    ):
        result = outlook_calendar.move_calendar_event(
            "evt-8", start_iso="2026-08-12T11:45:00", end_iso="2026-08-12T12:15:00"
        )
    assert result["ok"] is False and result["verified"] is False


def test_move_without_an_end_keeps_the_existing_duration(live_writes):
    """'move it 15 minutes later' must not silently shorten a 2-hour block."""
    before = _event("evt-9", "WOB", "2026-08-12T11:00:00", "2026-08-12T13:00:00")
    moved = {}

    def fake_move(event_id, *, start_iso, end_iso):
        moved.update({"start": start_iso, "end": end_iso})
        return {"ok": True, "verified": True, "event_id": event_id}

    with (
        patch("app.integrations.outlook_calendar.get_calendar_event", return_value=before),
        patch.object(actions, "has_conflict", return_value=(False, [], None)),
        patch("app.integrations.outlook_calendar.move_calendar_event", side_effect=fake_move),
    ):
        result = actions.move_calendar_event(
            event_id="evt-9", start_iso="2026-08-12T11:15:00", confirm=True
        )

    assert moved["start"] == "2026-08-12T11:15:00"
    assert moved["end"] == "2026-08-12T13:15:00"  # 120 minutes preserved
    assert result["moved_from"]["start"].startswith("2026-08-12T11:00")


def test_move_with_unknown_duration_asks_instead_of_guessing(live_writes):
    with patch("app.integrations.outlook_calendar.get_calendar_event", return_value=None):
        result = actions.move_calendar_event(
            event_id="evt-10", start_iso="2026-08-12T11:15:00", confirm=True
        )
    assert result["ok"] is False
    assert "end_iso" in result["error"]


def test_move_does_not_conflict_with_itself(live_writes):
    """The event being moved must be excluded from its own conflict check."""
    before = _event("evt-11", "Sync", "2026-08-12T11:30:00", "2026-08-12T12:00:00")
    seen = {}

    def fake_conflict(action, *, ignore_event_ids=None):
        seen["ignored"] = ignore_event_ids
        return False, [], None

    with (
        patch("app.integrations.outlook_calendar.get_calendar_event", return_value=before),
        patch.object(actions, "has_conflict", side_effect=fake_conflict),
        patch("app.integrations.outlook_calendar.move_calendar_event",
              return_value={"ok": True, "verified": True, "event_id": "evt-11"}),
    ):
        actions.move_calendar_event(
            event_id="evt-11", start_iso="2026-08-12T11:45:00",
            end_iso="2026-08-12T12:15:00", confirm=True,
        )
    assert seen["ignored"] == ["evt-11"]


def test_move_refuses_a_conflict_but_offers_an_override(live_writes):
    before = _event("evt-12", "Sync", "2026-08-12T11:30:00", "2026-08-12T12:00:00")
    busy = [_event("evt-busy", "Doug (Executive Coach)", "2026-08-12T13:15:00",
                   "2026-08-12T14:15:00")]
    with (
        patch("app.integrations.outlook_calendar.get_calendar_event", return_value=before),
        patch.object(actions, "has_conflict", return_value=(True, busy, None)),
        patch("app.integrations.outlook_calendar.move_calendar_event") as move,
    ):
        result = actions.move_calendar_event(
            event_id="evt-12", start_iso="2026-08-12T13:30:00",
            end_iso="2026-08-12T14:00:00", confirm=True,
        )
    assert result["ok"] is False
    assert "allow_conflict" in result["override"]
    move.assert_not_called()


def test_move_requires_an_event_id(live_writes):
    result = actions.move_calendar_event(
        event_id="  ", start_iso="2026-08-12T11:45:00", confirm=True
    )
    assert result["ok"] is False and "event id" in result["error"]


def test_move_requires_approval():
    from app.config import settings

    object.__setattr__(settings, "lexi_dry_run", False)
    try:
        with pytest.raises(PermissionError):
            actions.move_calendar_event(event_id="evt-13", start_iso="2026-08-12T11:45:00")
    finally:
        object.__setattr__(settings, "lexi_dry_run", True)


# ── finding an event to move ─────────────────────────────────────────────────


def test_calendar_window_summary_carries_event_ids():
    """A move needs an id. Without this the summary was a dead end for any day
    but today, and Lexi would be back to improvising."""
    from datetime import date

    from app.scheduling.calendar_summary import build_calendar_window_summary
    from app.scheduling.scheduling_window import SchedulingWindow

    busy = [_event("AAMkAD-real-id", "Pipeline", "2026-08-17T09:00:00", "2026-08-17T10:00:00")]
    summary = build_calendar_window_summary(
        busy_events=busy,
        window=SchedulingWindow(
            start=date(2026, 8, 17), end=date(2026, 8, 17), label="Mon 17", source="test"
        ),
    )
    day = summary["days"][0]
    assert day["events"][0]["event_id"] == "AAMkAD-real-id"
    # The text Kory reads stays clean.
    assert "AAMkAD-real-id" not in summary["formatted_summary"]
