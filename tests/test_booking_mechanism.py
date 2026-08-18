"""How a held time actually becomes a meeting.

Coverage said _confirm_hold_event and _match_hold_for_slot were the least-tested
live functions in the send path, which is uncomfortable: between them they are
the mechanism that turns Kory's HOLD on the calendar into the booked meeting.
Everything above them was well covered, so a fault here would surface as "the
invite went out but the calendar looks wrong" — the hardest kind to diagnose.

_match_hold_for_slot also compared ISO strings rather than instants. The same
moment is written -06:00 by us, +00:00 by a mail client, sometimes with a Z or
trailing milliseconds, and string equality calls those different times.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import (
    ExecutionResult,
    _confirm_hold_event,
    _match_hold_for_slot,
    _same_time_token,
)

MT = ZoneInfo("America/Denver")
START_MT = "2026-09-01T09:00:00-06:00"
END_MT = "2026-09-01T09:30:00-06:00"


def _result() -> ExecutionResult:
    return ExecutionResult(
        ok=False, proposal_id=1, status="pending_invite",
        decision="approved", errors=[], warnings=[],
    )


def _hold(event_id: str = "evt-hold", start: str = START_MT, end: str = END_MT) -> dict:
    return {"id": 7, "event_id": event_id, "slot_start": start, "slot_end": end}


# ---------------------------------------------------------------------------
# Matching a chosen time to the hold placed for it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hold_start, hold_end",
    [
        (START_MT, END_MT),                                          # identical
        ("2026-09-01T15:00:00+00:00", "2026-09-01T15:30:00+00:00"),  # same instant, UTC
        ("2026-09-01T15:00:00Z", "2026-09-01T15:30:00Z"),            # Zulu
        ("2026-09-01T09:00:00.000-06:00", "2026-09-01T09:30:00.000-06:00"),  # millis
    ],
    ids=["identical", "utc-offset", "zulu", "milliseconds"],
)
def test_a_hold_is_matched_by_the_moment_not_the_spelling(hold_start, hold_end):
    matched = _match_hold_for_slot(
        [_hold(start=hold_start, end=hold_end)], {"start": START_MT, "end": END_MT}
    )
    assert matched is not None, (
        "the hold placed for this exact time was not recognised as its hold, so "
        "it would not be promoted to the meeting"
    )


def test_a_genuinely_different_time_is_not_matched():
    """Comparing instants must not turn into matching everything."""
    other = _hold(start="2026-09-02T09:00:00-06:00", end="2026-09-02T09:30:00-06:00")
    assert _match_hold_for_slot([other], {"start": START_MT, "end": END_MT}) is None


def test_a_time_that_is_not_a_datetime_still_falls_back_to_text():
    """Kory can pass an option token rather than an ISO time."""
    assert _same_time_token("Option 1", "option  1") is True
    assert _same_time_token("Option 1", "Option 2") is False


def test_the_right_hold_is_picked_out_of_several():
    holds = [
        _hold("evt-a", "2026-09-01T08:00:00-06:00", "2026-09-01T08:30:00-06:00"),
        _hold("evt-b", START_MT, END_MT),
        _hold("evt-c", "2026-09-01T10:00:00-06:00", "2026-09-01T10:30:00-06:00"),
    ]
    matched = _match_hold_for_slot(holds, {"start": START_MT, "end": END_MT})
    assert matched and matched["event_id"] == "evt-b"


# ---------------------------------------------------------------------------
# Turning that hold into the meeting
# ---------------------------------------------------------------------------


def test_a_real_hold_is_replaced_by_the_confirmed_meeting():
    """The tentative hold comes off and the meeting goes on, in that order."""
    calls: list[str] = []
    with (
        patch("app.agents.comms_agent.delete_calendar_event",
              side_effect=lambda eid: calls.append(f"delete:{eid}")),
        patch("app.agents.comms_agent.create_calendar_event",
              side_effect=lambda action: (calls.append("create"), ("evt-booked", None))[1]),
    ):
        event_id = _confirm_hold_event(
            hold=_hold(), invite_action={"title": "Intro"}, result=_result()
        )
    assert event_id == "evt-booked"
    assert calls == ["delete:evt-hold", "create"], calls


def test_a_dry_run_hold_is_promoted_without_trying_to_delete_it():
    """A mock hold has no Outlook event behind it, so deleting would fail."""
    with (
        patch("app.agents.comms_agent.delete_calendar_event") as delete,
        patch("app.agents.comms_agent.create_calendar_event",
              return_value=("evt-booked", None)),
    ):
        event_id = _confirm_hold_event(
            hold=_hold("hold-pending-1-01-abcd"),
            invite_action={"title": "Intro"}, result=_result(),
        )
    assert event_id == "evt-booked"
    assert delete.called is False


def test_a_failed_promotion_reports_and_keeps_the_reference():
    """Outlook refuses the new event. The caller must learn about it."""
    result = _result()
    with (
        patch("app.agents.comms_agent.delete_calendar_event"),
        patch("app.agents.comms_agent.create_calendar_event",
              side_effect=RuntimeError("Graph 503")),
    ):
        event_id = _confirm_hold_event(
            hold=_hold(), invite_action={"title": "Intro"}, result=result
        )
    assert event_id == "evt-hold", "the hold reference must survive a failed promotion"
    assert any("Hold conversion failed" in w for w in result.warnings), result.warnings


def test_a_mock_hold_that_cannot_be_promoted_returns_nothing_and_says_so():
    """Returning an id here would record a booking that does not exist."""
    result = _result()
    with patch("app.agents.comms_agent.create_calendar_event",
               side_effect=RuntimeError("Graph 503")):
        event_id = _confirm_hold_event(
            hold=_hold("hold-pending-1-01-abcd"),
            invite_action={"title": "Intro"}, result=result,
        )
    assert event_id is None
    assert any("could not be promoted" in w for w in result.warnings), result.warnings


def test_a_hold_that_cannot_be_deleted_does_not_stop_the_booking():
    """A stale hold is untidy; a missing meeting is not. The booking wins."""
    result = _result()
    with (
        patch("app.agents.comms_agent.delete_calendar_event",
              side_effect=RuntimeError("already gone")),
        patch("app.agents.comms_agent.create_calendar_event",
              return_value=("evt-booked", None)),
    ):
        event_id = _confirm_hold_event(
            hold=_hold(), invite_action={"title": "Intro"}, result=result
        )
    assert event_id == "evt-booked"
    assert any("Could not delete tentative hold" in w for w in result.warnings)
