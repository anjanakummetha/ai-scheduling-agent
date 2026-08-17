"""The 2026-08-11 defect class (proposal 9187): hand-edited drafts must never
carry unvalidated times, and the send gate must refuse a draft whose times
diverge from the staged slots."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.scheduling.draft_slot_sync import (
    draft_matches_slots,
    extract_offer_times_from_draft,
    verify_draft_slots,
)

_MT = ZoneInfo("America/Denver")

# The style the model actually writes (Aug-11 transcript).
#
# Dates are generated from today rather than pinned. The pinned versions read
# "Tuesday, August 18", which stopped meaning anything once the calendar passed
# it: on 2026-09-14 the parser rolled it to August *2027* (a Wednesday), the
# busy fixture no longer overlapped, and three safety tests quietly inverted —
# test_booked_time_is_refused_with_the_clash_named asserted a refusal and got a
# pass. Generated dates also keep the weekday/date pairs honest, which the send
# gate now checks (tests/test_weekday_date_disagreement.py).


def _weekday_two_weeks_out(weekday: int) -> date:
    """A date on `weekday`, always in the future, clear of month boundaries."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=14)
    return monday + timedelta(days=weekday)


_D1 = _weekday_two_weeks_out(1)  # Tuesday
_D2 = _weekday_two_weeks_out(3)  # Thursday
_D3 = _weekday_two_weeks_out(8)  # the following Tuesday


def _line(d: date, times: str) -> str:
    return f"• {d:%A}, {d:%B} {d.day} at {times} MT"


def _mt(d: date, hour: int, minute: int = 0) -> str:
    """An ISO stamp in Kory's real zone. A hardcoded -06:00 is wrong for half
    the year — these tests failed only on dates past the November DST flip."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_MT).isoformat()


_DRAFT = f"""Hi Heidi,

Checking in — would love to grab 30 minutes to connect. Here are a few times that work on my end:

{_line(_D1, "1:00–1:30 PM")}
{_line(_D2, "3:30–4:00 PM")}
{_line(_D3, "9:00–9:30 AM")}

Let me know what works!
"""

# Overlaps the third offered slot (09:00) so the clash is a real one.
_ALEJANDRA = {
    "subject": "Coffee: Alejandra Harvey <> Kory Mitchell | 9 am (copy)",
    "start": {"dateTime": _mt(_D3, 9)},
    "end": {"dateTime": _mt(_D3, 10)},
}

_CTX_FREE = {"status": "available", "busy_events": []}
_CTX_BOOKED = {"status": "available", "busy_events": [_ALEJANDRA]}


def test_extracts_all_three_offered_times():
    from app.scheduling.busy_intervals import local_dt, parse_iso_datetime

    slots = extract_offer_times_from_draft(_DRAFT)
    starts = {
        local_dt(parse_iso_datetime(s["start"])).strftime("%m-%d %H:%M") for s in slots
    }
    assert starts == {
        f"{_D1:%m-%d} 13:00",
        f"{_D2:%m-%d} 15:30",
        f"{_D3:%m-%d} 09:00",
    }


def test_booked_time_is_refused_with_the_clash_named():
    check = verify_draft_slots(
        draft_body=_DRAFT,
        intent="internal_sync",
        subject="Check in",
        calendar_context=_CTX_BOOKED,
    )
    assert not check.ok
    assert any("Alejandra Harvey" in c for c in check.conflicts)


def test_free_times_pass_and_become_the_staged_slots():
    check = verify_draft_slots(
        draft_body=_DRAFT,
        intent="internal_sync",
        subject="Check in",
        calendar_context=_CTX_FREE,
    )
    assert check.ok, check.conflicts
    assert len(check.slots) == 3


def test_no_times_in_draft_keeps_existing_slots():
    existing = [{"start": "2026-08-18T13:00:00-06:00", "end": "2026-08-18T13:30:00-06:00"}]
    check = verify_draft_slots(
        draft_body="Hi Heidi,\n\nAdding a quick P.S. — looking forward to it!\n",
        intent="internal_sync",
        subject="Check in",
        existing_slots=existing,
        calendar_context=_CTX_FREE,
    )
    assert check.ok
    assert check.slots == existing
    assert check.warnings


def test_unreadable_calendar_refuses_rather_than_validating_blind():
    check = verify_draft_slots(
        draft_body=_DRAFT,
        intent="internal_sync",
        subject="Check in",
        calendar_context={"status": "unavailable", "busy_events": []},
    )
    assert not check.ok
    assert any("calendar" in c.lower() for c in check.conflicts)


def test_draft_matching_staged_slots_passes_send_gate():
    slots = extract_offer_times_from_draft(_DRAFT)
    ok, err = draft_matches_slots(draft_body=_DRAFT, proposed_slots=slots)
    assert ok, err


def test_divergent_draft_fails_send_gate():
    staged = [
        {"start": "2026-08-24T10:00:00-06:00", "end": "2026-08-24T10:30:00-06:00"},
        {"start": "2026-08-26T11:00:00-06:00", "end": "2026-08-26T11:30:00-06:00"},
        {"start": "2026-08-28T09:00:00-06:00", "end": "2026-08-28T09:30:00-06:00"},
    ]
    ok, err = draft_matches_slots(draft_body=_DRAFT, proposed_slots=staged)
    assert not ok
    assert "not in the staged slots" in err


def test_draft_without_times_passes_send_gate():
    staged = [{"start": "2026-08-24T10:00:00-06:00", "end": "2026-08-24T10:30:00-06:00"}]
    ok, _ = draft_matches_slots(
        draft_body="Just circling back — do the times still work?",
        proposed_slots=staged,
    )
    assert ok


def test_pre_send_gate_blocks_divergent_draft():
    from app.agents.comms_agent import _pre_send_slot_gate

    proposal = {
        "proposed_slots": [
            {"start": "2026-08-24T10:00:00-06:00", "end": "2026-08-24T10:30:00-06:00"}
        ],
        "drafted_reply": _DRAFT,
        "subject": "Check in",
        "raw_body": "",
    }
    err = _pre_send_slot_gate(proposal)
    assert err is not None and "staged slots" in err


def test_pre_send_gate_blocks_newly_booked_slot():
    from app.agents.comms_agent import _pre_send_slot_gate

    slots = extract_offer_times_from_draft(_DRAFT)
    proposal = {
        "proposed_slots": slots,
        "drafted_reply": _DRAFT,
        "subject": "Check in",
        "raw_body": "",
    }
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=_CTX_BOOKED,
    ):
        err = _pre_send_slot_gate(proposal)
    assert err is not None
    assert "Alejandra Harvey" in err and "NOT SENT" in err


def test_pre_send_gate_passes_clean_proposal():
    from app.agents.comms_agent import _pre_send_slot_gate

    slots = extract_offer_times_from_draft(_DRAFT)
    proposal = {
        "proposed_slots": slots,
        "drafted_reply": _DRAFT,
        "subject": "Check in",
        "raw_body": "",
    }
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=_CTX_FREE,
    ):
        err = _pre_send_slot_gate(proposal)
    assert err is None


def test_dual_timezone_offer_lines_pass_send_gate():
    # The production offer format quotes recipient time first with the zone
    # label after the range end: "11:00 AM–11:30 AM ET (9:00 AM–9:30 AM MT)".
    # Live 2026-08-15: the gate misread 11:00 as MT and refused a correct
    # draft. The label must apply to the range start.
    mon, tue = _weekday_two_weeks_out(0), _weekday_two_weeks_out(1)
    line = "at 11:00 AM–11:30 AM ET (9:00 AM–9:30 AM MT)"
    draft = (
        "Hi Anjana,\n\nA few times:\n\n"
        f"• {mon:%A}, {mon:%B} {mon.day} {line}\n"
        f"• {tue:%A}, {tue:%B} {tue.day} {line}\n\n"
        "Would any of these work?"
    )
    staged = [
        {"start": _mt(d, 9), "end": _mt(d, 9, 30)}
        for d in (mon, tue)
    ]
    ok, err = draft_matches_slots(draft_body=draft, proposed_slots=staged)
    assert ok, err
