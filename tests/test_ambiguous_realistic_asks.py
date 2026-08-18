"""Long, messy, ambiguous asks — how people actually write to a CEO.

The clean cases pass everywhere. What reaches production is the email with a
buried constraint, two contradictory sentences, a date and a weekday that
disagree, a blackout stated as a courtesy, or four paragraphs of context with
the actual ask in the last line.

Two standing rules govern every assertion here:
  * Lexi may never offer a time she was told is unavailable.
  * When the ask cannot be resolved, she says so — she does not pick.

Every date is computed from the frozen clock, never pinned.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from app.scheduling.inbound_availability import (
    body_looks_like_inbound_availability,
    extract_inbound_time_candidates,
)
from app.scheduling.recipient_slot import recipient_times_rejected
from app.scheduling.schedule_from_context import schedule_from_context

MT = ZoneInfo("America/Denver")
TODAY = "2026-09-01"  # a Tuesday
CTX = {"status": "available", "horizon_days": 45, "busy_events": []}


def _d(weekday: int, weeks: int = 2) -> date:
    base = date(2026, 9, 1) + timedelta(weeks=weeks)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


def _starts(cands) -> list[str]:
    return [c["start"] for c in cands]


def _days(result) -> set[date]:
    return {datetime.fromisoformat(s["start"]).date() for s in result.slots}


def _run(body: str, guidance: str = "", intent: str = "referral_or_intro"):
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=CTX,
    ):
        return schedule_from_context(
            subject="[TEST] intro",
            body=body,
            intent=intent,
            sender_email="curtis@example.com",
            kory_scheduling_guidance=guidance,
            use_llm_plan=False,
            calendar_context=CTX,
        )


# --- the ask is buried in a long email ------------------------------------


@freeze_time(TODAY)
def test_the_ask_in_the_last_line_of_a_long_email_is_still_found():
    wed = _d(2)
    body = (
        "Kory — great to meet you at the conference last month, and thanks again "
        "for the introduction to Natalie, which has already gone somewhere.\n\n"
        "We closed our third acquisition in June and the integration has taken "
        "most of my attention since. The team is now 140 people across four "
        "states and we are starting to think seriously about what the next "
        "eighteen months look like, particularly on the financing side.\n\n"
        "I know you have seen a lot of these situations and I would value your "
        "read before we commit to a direction.\n\n"
        f"Would {wed:%A}, {wed:%B} {wed.day} at 2:00 PM work for a call?"
    )
    cands = extract_inbound_time_candidates(body)
    assert cands, "the ask was buried but it is still an ask"
    resolved = datetime.fromisoformat(cands[0]["start"])
    assert resolved.date() == wed
    assert resolved.hour == 14


@freeze_time(TODAY)
def test_dates_mentioned_as_history_are_not_treated_as_offers():
    """"We spoke on August 3rd" is not a proposal."""
    thu = _d(3)
    body = (
        "Good speaking on August 3rd — the notes you sent afterwards were useful. "
        f"Following up on that: could we do {thu:%A} {thu:%B} {thu.day} at 10:00 AM?"
    )
    starts = _starts(extract_inbound_time_candidates(body))
    assert any(datetime.fromisoformat(s).date() == thu for s in starts)
    assert not any(datetime.fromisoformat(s).month == 8 for s in starts), starts


# --- stated blackouts -----------------------------------------------------


@freeze_time(TODAY)
def test_a_blackout_stated_politely_is_not_read_as_availability():
    body = (
        "I am travelling the week of the 14th and would rather not squeeze this in "
        "around flights. Anything after that is wide open on my side."
    )
    for start in _starts(extract_inbound_time_candidates(body)):
        assert datetime.fromisoformat(start).day not in range(14, 19), start


@freeze_time(TODAY)
def test_an_offer_plus_a_blackout_keeps_only_the_offer():
    tue, thu = _d(1), _d(3)
    body = (
        f"I could do {tue:%A} the {tue.day}th at 9:00 AM. "
        f"I am out all day {thu:%A} the {thu.day}th, so not that one."
    )
    days = {datetime.fromisoformat(s).date() for s in _starts(extract_inbound_time_candidates(body))}
    assert tue in days
    assert thu not in days, "a day they said they are out must never be offered"


# --- contradictions must be surfaced, not resolved -------------------------


@freeze_time(TODAY)
def test_a_weekday_that_contradicts_its_date_is_not_silently_picked():
    d = _d(2)  # Wednesday
    wrong = (d + timedelta(days=1)).strftime("%A")  # Thursday, deliberately wrong
    result = _run(f"Can we meet {wrong}, {d:%B} {d.day} at 2:00 PM?")
    note = " ".join(result.inbound_notes) + " " + result.failure_message
    assert "Which did they mean?" in note
    assert d not in _days(result), "nothing may be offered off a contradictory time"


@freeze_time(TODAY)
def test_two_contradictory_sentences_do_not_produce_a_confident_offer():
    """"Mornings are best" then "I can only do afternoons" — do not average them."""
    result = _run(
        "Mornings are usually best for me. Actually, scratch that — this month I "
        "can only do afternoons, anything after 1."
    )
    assert result.ok, result.failure_message
    for slot in result.slots:
        hour = datetime.fromisoformat(slot["start"]).astimezone(MT).hour
        assert hour >= 13, f"the later statement wins: got {hour}:00"


# --- rejection and re-offer language --------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "None of those work I'm afraid — anything the following week?",
        "Unfortunately I'm double booked for all three. Could we look at the week after?",
        "Those are tough. Do you have anything later in the month?",
        "Sorry, that week is gone. Next week?",
    ],
)
@freeze_time(TODAY)
def test_a_polite_rejection_is_recognised_as_a_rejection(reply: str):
    assert recipient_times_rejected(reply), reply


@pytest.mark.parametrize(
    "reply",
    [
        "Thursday at 2 works, see you then.",
        "The first one is perfect.",
        "Let's do the Tuesday slot.",
    ],
)
@freeze_time(TODAY)
def test_an_acceptance_is_not_mistaken_for_a_rejection(reply: str):
    assert not recipient_times_rejected(reply), reply


# --- vague asks -----------------------------------------------------------


@freeze_time(TODAY)
def test_shoot_me_some_times_is_an_ask_to_propose_not_an_offer():
    body = "Would love to connect — shoot me some days and times that work for you."
    assert not extract_inbound_time_candidates(body), "there is no time in this message"
    result = _run(body)
    assert result.ok, result.failure_message
    assert result.slots, "an ask-us-to-propose must produce an offer"


@freeze_time(TODAY)
def test_a_relative_window_with_no_dates_still_produces_real_slots():
    result = _run("Sometime in the next couple of weeks would be great, no rush.")
    assert result.ok, result.failure_message
    assert result.slots
    horizon = date(2026, 9, 1) + timedelta(days=30)
    for day in _days(result):
        assert day <= horizon, f"{day} is outside 'the next couple of weeks'"


@freeze_time(TODAY)
def test_a_duration_mentioned_in_passing_does_not_become_a_time():
    """"a quick 15 minutes" / "half an hour" are lengths, not clock times."""
    for body in ("Could I grab a quick 15 minutes with you next week?",
                 "Half an hour sometime next week would be plenty."):
        assert not body_looks_like_inbound_availability(body) or not [
            c for c in extract_inbound_time_candidates(body) if c.get("explicit_time")
        ], body


@pytest.mark.parametrize(
    "reply",
    [
        # These contain week words but are ACCEPTANCES. The bare-week-shift
        # pattern is anchored to a sentence boundary and requires a question
        # mark precisely so these stay untouched.
        "Thursday next week works great.",
        "Next week I am back in Denver, so the Tuesday slot is perfect.",
        "Booked it, thanks!",
        "The following week I'm on holiday, but your Tuesday option works.",
    ],
)
@freeze_time(TODAY)
def test_week_words_inside_an_acceptance_are_not_a_rejection(reply: str):
    assert not recipient_times_rejected(reply), reply


@freeze_time(TODAY)
def test_an_unresolved_contradiction_stops_the_run_rather_than_offering_the_day():
    """Asking "which did they mean?" while offering the disputed day is worse
    than either alone — it reads as a confident answer with a footnote."""
    d = _d(2)
    wrong = (d + timedelta(days=1)).strftime("%A")
    result = _run(f"Can we meet {wrong}, {d:%B} {d.day} at 2:00 PM?")
    assert result.ok is False
    assert result.status == "weekday_date_contradiction"
    assert not result.slots, "nothing may be offered while the ask is ambiguous"
