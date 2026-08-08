"""R1/R2 from the send-path analysis.

R1: "mornings HER time" must mean the recipient's morning — the window was
applied in MT, so a Boston morning ask accepted noon-ET slots.
R2: the outbound path shipped out-of-window offers with no disclosure note;
inbound offers carry "no availability for <window> — offering <dates> instead".
"""

from __future__ import annotations

from app.agents.outbound_agent import _outbound_disclosure_note
from app.scheduling.scheduling_window import infer_time_of_day_window


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
    note = _outbound_disclosure_note(
        subject="[TEST] intro call",
        body="Outbound delegation by kory. next week",
        intent="meeting",
        slots=[
            {"start": "2026-08-26T09:30:00-06:00", "end": "2026-08-26T10:30:00-06:00"},
            {"start": "2026-09-02T09:30:00-06:00", "end": "2026-09-02T10:30:00-06:00"},
        ],
        calendar_context={"status": "available", "busy_events": []},
    )
    assert "No availability for next week" in note
    assert "August 26" in note and "September 2" in note


def test_outbound_note_quiet_when_slots_fit_the_window():
    note = _outbound_disclosure_note(
        subject="[TEST] intro call",
        body="Outbound delegation by kory. week of August 24",
        intent="meeting",
        slots=[
            {"start": "2026-08-26T09:30:00-06:00", "end": "2026-08-26T10:30:00-06:00"},
        ],
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
