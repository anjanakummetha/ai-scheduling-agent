"""R1/R2 from the send-path analysis.

R1: "mornings HER time" must mean the recipient's morning — the window was
applied in MT, so a Boston morning ask accepted noon-ET slots.
R2: the outbound path shipped out-of-window offers with no disclosure note;
inbound offers carry "no availability for <window> — offering <dates> instead".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.agents.outbound_agent import _outbound_disclosure_note
from app.scheduling.scheduling_window import infer_time_of_day_window

_MT = ZoneInfo("America/Denver")


def _day_in_week_after_next(weekday: int) -> date:
    """A date guaranteed to fall outside a "next week" ask.

    `scheduling_window` reads "next week" as the Monday-Sunday week after the
    current one, so it ends 13 days past this Monday. Anchoring to the week
    after that keeps these slots outside the window on whatever weekday the
    suite happens to run, with two days of slack for MT-vs-UTC skew.
    """
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday + timedelta(days=14 + weekday)


def _slot(day: date, hour: int = 9, minutes: int = 30, length_minutes: int = 60) -> dict[str, str]:
    """A slot on `day` in Kory's real zone — the UTC offset follows DST rather
    than a pinned -06:00, so these stay correct across the March/November flips."""
    start = datetime(day.year, day.month, day.day, hour, minutes, tzinfo=_MT)
    end = start + timedelta(minutes=length_minutes)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _long_date(day: date) -> str:
    """How the disclosure note renders a date: "August 26", no zero padding."""
    return f"{day.strftime('%B')} {day.day}"


def test_mornings_her_time_shifts_to_recipient_zone():
    w = infer_time_of_day_window(
        subject="Intro call",
        body="mornings her time, she is in Boston (Eastern time), next week",
    )
    assert w is not None
    # ET mornings 8:00-12:00 = 6:00-10:00 MT (EDT is MT+2)
    assert (w.start_hour, w.start_minute) == (6, 0), w
    assert (w.end_hour, w.end_minute) == (10, 0), w
    assert "mornings" in w.label


def test_plain_mornings_stays_mountain_time():
    w = infer_time_of_day_window(subject="", body="mornings work best for me")
    assert w is not None
    assert (w.start_hour, w.end_hour) == (8, 12)


def test_cue_without_stated_zone_stays_mountain_time():
    w = infer_time_of_day_window(subject="", body="mornings her time please")
    assert w is not None
    assert (w.start_hour, w.end_hour) == (8, 12), "no stated zone — never guess"


def test_pacific_shift_goes_the_other_way():
    w = infer_time_of_day_window(
        subject="", body="afternoons his time — he is in San Francisco, CA"
    )
    assert w is not None
    # PT afternoons 12:00-17:00 = 13:00-18:00 MT (PDT is MT-1)
    assert (w.start_hour, w.end_hour) == (13, 18), w


def test_outbound_note_discloses_out_of_window_offers():
    # Anchored to the week after next so the slots stay OUTSIDE a "next week"
    # ask on whatever day the suite runs. Hardcoded dates made this test pass
    # only until the calendar caught up with them — on 2026-08-17 the pinned
    # August 26 fell *inside* next week and the assertion inverted.
    wednesday = _day_in_week_after_next(2)
    monday = _day_in_week_after_next(7)  # the following Monday
    note = _outbound_disclosure_note(
        subject="[TEST] intro call",
        body="Outbound delegation by kory. next week",
        intent="meeting",
        slots=[_slot(wednesday), _slot(monday)],
        calendar_context={"status": "available", "busy_events": []},
    )
    assert "No availability for next week" in note
    assert _long_date(wednesday) in note and _long_date(monday) in note


def test_outbound_note_quiet_when_slots_fit_the_window():
    # The ask names the slot's own week explicitly, so nothing is out of window.
    wednesday = _day_in_week_after_next(2)
    week_start = wednesday - timedelta(days=wednesday.weekday())
    note = _outbound_disclosure_note(
        subject="[TEST] intro call",
        body=f"Outbound delegation by kory. week of {_long_date(week_start)}",
        intent="meeting",
        slots=[_slot(wednesday)],
        calendar_context={"status": "available", "busy_events": []},
    )
    assert "No availability" not in note


def test_outbound_note_never_raises_without_window():
    note = _outbound_disclosure_note(
        subject="[TEST] intro call",
        body="Outbound delegation by kory for 30 minutes.",
        intent="meeting",
        slots=[{"start": "2026-08-26T09:30:00-06:00", "end": "2026-08-26T10:30:00-06:00"}],
        calendar_context={"status": "available", "busy_events": []},
    )
    assert isinstance(note, str)


def test_gate_honors_east_coast_cue_like_the_engine():
    """The gate re-validated without the cue, so 6 AM slots offered FOR a
    Boston contact came back "only for East Coast contacts"."""
    from app.scheduling.pre_approval_gate import verify_before_kory_approval

    gate = verify_before_kory_approval(
        slots=[{"start": "2026-08-18T06:00:00-06:00", "end": "2026-08-18T06:30:00-06:00"}],
        calendar_context={"status": "available", "busy_events": []},
        intent="meeting",
        subject="[TEST] intro call",
        body="mornings her time, she is in Boston (Eastern time)",
    )
    assert not any("only for East Coast" in v for v in (gate.summary() or "").split(";"))


def test_note_drops_redundant_window_entries():
    # Tue/Thu of the week after next: the body asks for "next week", so these
    # slots miss the window and the note must disclose that. Previously hardcoded
    # to Aug 18/20, which silently stopped testing anything once the calendar
    # reached the point where those dates *were* next week.
    tue = _day_in_week_after_next(1)
    thu = _day_in_week_after_next(3)
    note = _outbound_disclosure_note(
        subject="[TEST] intro call",
        body="Outbound delegation by kory. mornings her time, she is in Boston (Eastern time), next week",
        intent="meeting",
        slots=[
            {"start": f"{tue}T06:00:00-06:00", "end": f"{tue}T06:30:00-06:00"},
            {"start": f"{thu}T06:00:00-06:00", "end": f"{thu}T06:30:00-06:00"},
        ],
        calendar_context={"status": "available", "busy_events": []},
    )
    assert "No availability for next week" in note
    assert "outside requested window" not in note
    assert "only for East Coast" not in note
