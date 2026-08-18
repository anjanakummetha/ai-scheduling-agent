"""The cases Kory says keep going wrong: no availability, a busy requested day,
and asking for different times.

His standing rules, in priority order:
  1. Lexi may NEVER make up a time. Every offered slot has to come from the
     engine reading the real calendar.
  2. If there is a problem she tells him what it actually is, with the real
     dates, and does not paper over it.
  3. He can ask for something different and get it.

These drive the real engine with a synthetic calendar, so every date asserted
here is one the engine chose — no fixture pretends to be an offer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.schedule_from_context import schedule_from_context

MT = ZoneInfo("America/Denver")


def _monday(weeks_out: int = 1) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=weeks_out)


def _fill(start: date, days: int) -> list[dict]:
    """Wall-to-wall busy, 06:00-19:00, for `days` days from `start`."""
    events = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for hour in range(6, 19):
            begin = datetime(day.year, day.month, day.day, hour, 0, tzinfo=MT)
            events.append(
                {
                    "subject": f"Booked {hour}:00",
                    "start": {"dateTime": begin.isoformat()},
                    "end": {"dateTime": (begin + timedelta(hours=1)).isoformat()},
                }
            )
    return events


def _run(body: str, busy: list[dict], guidance: str = ""):
    context = {"status": "available", "horizon_days": 45, "busy_events": busy}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=context,
    ):
        return schedule_from_context(
            subject="[TEST] intro call",
            body=body,
            intent="referral_or_intro",
            sender_email="curtis@example.com",
            kory_scheduling_guidance=guidance,
            use_llm_plan=False,
            calendar_context=context,
        )


def _slot_days(result) -> set[date]:
    return {datetime.fromisoformat(s["start"]).date() for s in result.slots}


def _busy_days(busy: list[dict]) -> set[date]:
    return {
        datetime.fromisoformat(e["start"]["dateTime"]).date() for e in busy
    }


# --- rule 1: never offer a time that is not free -------------------------


def test_no_offered_slot_ever_lands_on_a_booked_day():
    """The whole requested week is full. Whatever she offers must be elsewhere."""
    monday = _monday(1)
    busy = _fill(monday, 7)
    result = _run("Can we meet next week?", busy)
    assert result.slots, "she must still find something rather than give up"
    assert not (_slot_days(result) & _busy_days(busy)), (
        "an offer landed on a day that is booked solid"
    )


def test_whatever_is_offered_is_always_named_to_kory_in_full():
    """Kory must be able to read the actual dates without opening the calendar.

    Asserted against the rendered lines rather than only the caveat note: the
    note is produced when a window was inferred AND expanded, which depends on
    the phrasing and on what day of the year the suite runs. What must hold
    unconditionally is that every offered slot appears, spelled out, in what he
    is shown.
    """
    monday = _monday(1)
    result = _run("Can we meet next week?", _fill(monday, 7))
    shown = " ".join(result.formatted_slots) + " " + result.scheduling_note()
    assert result.slots
    for day in _slot_days(result):
        assert f"{day:%B} {day.day}" in shown, f"{day} offered but never named"


def test_when_a_window_is_expanded_the_caveat_says_so():
    """The C-1 defect: offers landed outside the requested window silently."""
    monday = _monday(1)
    result = _run(
        f"Can we meet the week of {monday:%B} {monday.day}?", _fill(monday, 7)
    )
    note = result.scheduling_note()
    assert "no availability" in note.lower(), note
    # It must name where the offer LANDED, not just the window it searched.
    for day in _slot_days(result):
        assert f"{day:%B} {day.day}" in note + " ".join(result.formatted_slots)


def test_a_busy_requested_day_is_named_and_the_alternative_is_real():
    """'They want this day but you're busy' — he must be told, with real dates."""
    monday = _monday(1)
    busy = _fill(monday, 1)  # only that Monday is full
    result = _run(f"Can we meet Monday {monday:%B} {monday.day}?", busy)
    note = result.scheduling_note()
    assert "no availability" in note.lower(), note
    assert f"{monday:%B} {monday.day}" in note, "the day he asked for must be named"
    assert monday not in _slot_days(result), "never offer the booked day"
    assert result.slots


# --- rule 2: an unreadable calendar is never guessed around --------------


def test_an_unreadable_calendar_refuses_rather_than_guessing():
    context = {"status": "unavailable", "error": "graph timeout", "busy_events": []}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=context,
    ):
        result = schedule_from_context(
            subject="[TEST] intro",
            body="Can we meet next week?",
            intent="referral_or_intro",
            sender_email="curtis@example.com",
            use_llm_plan=False,
        )
    assert result.ok is False
    assert not result.slots, "no slot may be offered against a calendar we cannot read"
    assert "calendar" in result.failure_message.lower()


# --- rule 3: Kory can ask for something different -------------------------


def test_kory_can_narrow_to_one_weekday_and_gets_only_that_day():
    result = _run("Can we meet next week?", [], guidance="Thursday only please.")
    assert result.ok, result.failure_message
    assert result.slots
    for day in _slot_days(result):
        assert day.weekday() == 3, f"{day} is not a Thursday"


def test_kory_can_change_the_time_of_day():
    result = _run("Can we meet next week?", [], guidance="Afternoons only.")
    assert result.ok, result.failure_message
    assert result.slots
    for slot in result.slots:
        hour = datetime.fromisoformat(slot["start"]).astimezone(MT).hour
        assert hour >= 12, f"{hour}:00 is not an afternoon"


def test_guidance_moves_the_offer_off_the_days_first_proposed():
    """'Find different times' has to actually produce different times."""
    first = _run("Can we meet next week?", [])
    assert first.slots
    narrowed = _run("Can we meet next week?", [], guidance="Friday only please.")
    assert narrowed.ok, narrowed.failure_message
    assert _slot_days(narrowed) != _slot_days(first)
    for day in _slot_days(narrowed):
        assert day.weekday() == 4


@pytest.mark.parametrize("guidance", ["Thursday only please.", "Afternoons only."])
def test_every_offered_slot_is_still_in_the_future(guidance: str):
    result = _run("Can we meet next week?", [], guidance=guidance)
    now = datetime.now(MT)
    for slot in result.slots:
        assert datetime.fromisoformat(slot["start"]).astimezone(MT) > now


def test_the_engine_never_proposes_a_weekend_even_when_fully_booked():
    """Kory: "Default is no work meetings on weekends — it creates issues at home."

    The pressure case is a calendar with nowhere to go: a fallback ladder that
    widens far enough will eventually reach a Saturday unless the rule holds.
    """
    monday = _monday(1)
    for busy in ([], _fill(monday, 14)):
        result = _run("Can we meet soon? Any day works.", busy)
        assert result.slots, "she must still find something"
        for day in _slot_days(result):
            assert day.weekday() < 5, f"offered {day:%A} {day}"


def test_a_draft_offering_a_weekend_is_refused_at_the_send_gate():
    """Belt and braces: if a weekend ever reaches a draft, nothing sends.

    Seen live 2026-08-18 — a draft carrying "Sunday, August 30 at 9:00 AM MT"
    was refused with nothing sent and no holds touched. That refusal is the
    9187 protection doing its job, not a failure.
    """
    from app.scheduling.draft_slot_sync import verify_draft_slots

    today = date.today()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7 + 7)
    draft = (
        "Hi,\n\nA few times:\n\n"
        f"• {sunday:%A}, {sunday:%B} {sunday.day} at 9:00-9:30 AM MT\n"
    )
    check = verify_draft_slots(
        draft_body=draft,
        intent="referral_or_intro",
        subject="[TEST] intro",
        calendar_context={"status": "available", "busy_events": []},
    )
    assert not check.ok, "a weekend draft must never pass the send gate"
