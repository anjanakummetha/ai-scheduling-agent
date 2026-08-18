"""A scheduling thread over its whole life, not one step at a time.

Kory's complaint is that Lexi "always makes scheduling mistakes after" — the
failures appear at round two or three, once guidance, a counter-proposal and a
revision have all touched the same proposal. Single-step tests never see that.

Each test here walks a sequence and checks the state after EVERY step, so a
regression is caught at the step that caused it rather than at the end.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from app.scheduling.draft_slot_sync import verify_draft_slots
from app.scheduling.recipient_slot import recipient_times_rejected
from app.scheduling.schedule_from_context import schedule_from_context

MT = ZoneInfo("America/Denver")
TODAY = "2026-09-01"  # Tuesday
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}


def _d(weekday: int, weeks: int = 2) -> date:
    base = date(2026, 9, 1) + timedelta(weeks=weeks)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


def _busy(d: date, hour: int, hours: int = 1, subject: str = "Booked") -> dict:
    start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=MT)
    return {
        "subject": subject,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=hours)).isoformat()},
    }


def _offer(body: str, guidance: str = "", busy: list | None = None, intent="referral_or_intro"):
    ctx = {"status": "available", "horizon_days": 45, "busy_events": busy or []}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=ctx
    ):
        return schedule_from_context(
            subject="[TEST] intro call",
            body=body,
            intent=intent,
            sender_email="curtis@example.com",
            kory_scheduling_guidance=guidance,
            use_llm_plan=False,
            calendar_context=ctx,
        )


def _days(r) -> set[date]:
    return {datetime.fromisoformat(s["start"]).date() for s in r.slots}


def _hours(r) -> set[int]:
    return {datetime.fromisoformat(s["start"]).astimezone(MT).hour for s in r.slots}


# --- round 1 -> Kory changes his mind -> round 2 ---------------------------


@freeze_time(TODAY)
def test_kory_narrows_the_day_then_narrows_the_time_and_both_stick():
    first = _offer("Any time in the next couple of weeks works.")
    assert first.ok and first.slots

    day_only = _offer("Any time in the next couple of weeks works.", guidance="Thursdays only.")
    assert day_only.ok, day_only.failure_message
    assert {d.weekday() for d in _days(day_only)} == {3}

    both = _offer(
        "Any time in the next couple of weeks works.",
        guidance="Thursdays only, and afternoons please.",
    )
    assert both.ok, both.failure_message
    assert {d.weekday() for d in _days(both)} == {3}, "the day constraint must survive"
    assert all(h >= 12 for h in _hours(both)), f"afternoon lost: {_hours(both)}"


@freeze_time(TODAY)
def test_a_later_instruction_overrides_an_earlier_one_in_the_same_guidance():
    r = _offer(
        "Any time works.",
        guidance="Mondays are good. Actually no — make it Wednesday instead.",
    )
    assert r.ok, r.failure_message
    assert {d.weekday() for d in _days(r)} == {2}, "the correction must win"


@freeze_time(TODAY)
def test_guidance_that_contradicts_the_sender_wins():
    """The sender asked for Monday; Kory says Tuesday. Kory is the principal."""
    mon = _d(0)
    r = _offer(f"Could we do Monday {mon:%B} {mon.day}?", guidance="Monday is packed — Tuesday.")
    assert r.ok, r.failure_message
    assert {d.weekday() for d in _days(r)} == {1}


# --- counter-proposal, then revision --------------------------------------


@freeze_time(TODAY)
def test_a_counter_then_a_re_offer_moves_off_the_rejected_week():
    wk1 = _offer("Next week if possible.")
    assert wk1.ok and wk1.slots
    assert recipient_times_rejected("None of those work — anything the following week?")

    wk2 = _offer("Next week if possible.", guidance="They want the following week instead.")
    assert wk2.ok, wk2.failure_message
    assert _days(wk2) != _days(wk1), "a re-offer must actually move"
    assert min(_days(wk2)) > min(_days(wk1))


@freeze_time(TODAY)
def test_the_day_they_rejected_is_not_re_offered():
    tue = _d(1)
    first = _offer(f"How about Tuesday {tue:%B} {tue.day}?")
    assert tue in _days(first)

    second = _offer(
        f"How about Tuesday {tue:%B} {tue.day}?",
        guidance=f"They can't do the {tue.day}th anymore — find something else.",
    )
    assert second.ok, second.failure_message
    assert tue not in _days(second), "re-offering a refused day wastes a round"


# --- the calendar changes underneath --------------------------------------


@freeze_time(TODAY)
def test_a_slot_booked_after_the_offer_is_caught_before_sending():
    wed = _d(2)
    offered = _offer(f"Wednesday {wed:%B} {wed.day} at 9?")
    assert offered.ok and wed in _days(offered)

    # Someone books over it between the offer and the approval.
    draft = f"Hi,\n\n• {wed:%A}, {wed:%B} {wed.day} at 9:00–9:30 AM MT\n"
    check = verify_draft_slots(
        draft_body=draft,
        intent="referral_or_intro",
        subject="[TEST] intro call",
        calendar_context={
            "status": "available",
            "busy_events": [_busy(wed, 9, subject="Board call")],
        },
    )
    assert not check.ok
    assert any("Board call" in c for c in check.conflicts), check.conflicts


@freeze_time(TODAY)
def test_a_day_that_fills_up_pushes_the_offer_elsewhere_without_inventing():
    thu = _d(3)
    packed = [_busy(thu, h) for h in range(6, 19)]
    r = _offer(f"Thursday {thu:%B} {thu.day}?", busy=packed)
    assert r.ok, r.failure_message
    assert thu not in _days(r), "the packed day must not be offered"
    assert r.slots, "she must still find something"
    note = r.scheduling_note()
    assert f"{thu:%B} {thu.day}" in note, "Kory must be told which day failed"


# --- the whole sequence, in order -----------------------------------------


@freeze_time(TODAY)
def test_four_rounds_in_sequence_never_produce_a_past_or_booked_slot():
    """Guidance, counter, re-offer, narrow — state must stay coherent throughout."""
    tue = _d(1)
    steps = [
        ("Any time in the next two weeks.", ""),
        ("Any time in the next two weeks.", "Tuesdays or Thursdays."),
        ("Any time in the next two weeks.", "They pushed — make it the following week."),
        ("Any time in the next two weeks.", "Thursday only, afternoon, 45 minutes."),
    ]
    now = datetime(2026, 9, 1, tzinfo=MT)
    busy = [_busy(tue, 9, subject="Existing")]
    for body, guidance in steps:
        r = _offer(body, guidance=guidance, busy=busy)
        assert r.ok, f"{guidance!r} -> {r.failure_message}"
        assert r.slots, f"{guidance!r} produced nothing"
        for slot in r.slots:
            start = datetime.fromisoformat(slot["start"]).astimezone(MT)
            assert start > now, f"{guidance!r} produced a past slot {start}"
            assert not (start.date() == tue and start.hour == 9), (
                f"{guidance!r} offered over an existing booking"
            )


@freeze_time(TODAY)
def test_repeating_the_same_request_is_stable_not_drifting():
    """The same ask twice must not wander — Kory noticing a changed answer with
    no changed input is how trust goes."""
    a = _offer("Any time in the next two weeks.", guidance="Wednesdays, mornings.")
    b = _offer("Any time in the next two weeks.", guidance="Wednesdays, mornings.")
    assert _days(a) == _days(b)
    assert _hours(a) == _hours(b)
