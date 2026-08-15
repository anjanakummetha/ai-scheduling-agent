"""Kory's guidance redirects the search — ALL of it, in one message.

Live I-2 class: guidance reached the draft prompt but the deterministic
engine silently out-voted it (the merged body still led with the sender's
"30 minutes"/"next week"). These pin the 2026-08-15 fix: a multi-change
directive ("Try Thursday instead, make it 45 minutes, afternoon only")
constrains weekday, duration, AND time-of-day — with or without the LLM
planner — and "avoid Fridays" excludes rather than selects.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduling.schedule_from_context import merge_scheduling_body
from app.scheduling.scheduling_plan import build_scheduling_plan
from app.scheduling.scheduling_window import weekdays_from_guidance
from app.scheduling.slot_engine import propose_meeting_slots
from app.scheduling.window_fallback import build_failure_kory_message

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 14, 9, 0, tzinfo=MT)  # Friday
EMPTY_WEEK = {"status": "available", "horizon_days": 30, "busy_events": []}


def _plan(body: str, guidance: str, intent: str = "referral_or_intro"):
    merged = merge_scheduling_body(body, guidance)
    plan = build_scheduling_plan(
        subject="Intro call", body=merged, intent=intent,
        reference_now=NOW, use_llm=False,
    )
    plan.kory_guidance = guidance
    return merged, plan


def _slots(body: str, guidance: str):
    merged, plan = _plan(body, guidance)
    prop = propose_meeting_slots(
        EMPTY_WEEK, intent="referral_or_intro", subject="Intro call",
        body=merged, plan=plan, reference_now=NOW,
    )
    return prop


SENDER_BODY = (
    "Would love to find 30 minutes to connect next week. "
    "Happy to work around your schedule."
)


def test_multi_change_guidance_honors_every_directive():
    prop = _slots(SENDER_BODY, "Try Thursday instead, make it 45 minutes, and keep it to the afternoon")
    assert prop.slots, prop.diagnostics
    for s in prop.slots:
        start = datetime.fromisoformat(s["start"])
        end = datetime.fromisoformat(s["end"])
        assert start.astimezone(MT).strftime("%A") == "Thursday", s
        assert (end - start).total_seconds() == 45 * 60, s
        assert start.astimezone(MT).hour >= 12, s


def test_guidance_duration_beats_sender_duration():
    prop = _slots(SENDER_BODY, "make it 45 minutes")
    assert prop.slots
    for s in prop.slots:
        start = datetime.fromisoformat(s["start"])
        end = datetime.fromisoformat(s["end"])
        assert (end - start).total_seconds() == 45 * 60, s


def test_single_day_guidance_constrains_weekday():
    prop = _slots(SENDER_BODY, "try Friday")
    assert prop.slots
    for s in prop.slots:
        assert datetime.fromisoformat(s["start"]).astimezone(MT).strftime("%A") == "Friday", s


def test_avoid_day_guidance_excludes_not_selects():
    assert weekdays_from_guidance("avoid Fridays") == {0, 1, 2, 3}
    assert weekdays_from_guidance("try Thursday instead") == {3}
    assert weekdays_from_guidance("Tuesday or Wednesday work") == {1, 2}
    assert weekdays_from_guidance("redo it in my voice") is None
    prop = _slots(SENDER_BODY, "avoid Friday")
    assert prop.slots
    for s in prop.slots:
        assert datetime.fromisoformat(s["start"]).astimezone(MT).strftime("%A") != "Friday", s


def test_style_guidance_changes_nothing_about_slots():
    baseline = _slots(SENDER_BODY, "")
    redo = _slots(SENDER_BODY, "redo the draft in my voice")
    assert [s["start"] for s in redo.slots] == [s["start"] for s in baseline.slots]


# --- fully-booked week: expansion is DISCLOSED, and total failure names the ask ---


def _solid_week(day_iso_list):
    events = []
    for day in day_iso_list:
        events.append({
            "subject": "Busy block",
            "start": {"dateTime": f"{day}T06:00:00", "timeZone": "America/Denver"},
            "end": {"dateTime": f"{day}T20:00:00", "timeZone": "America/Denver"},
            "showAs": "busy",
        })
    return {"status": "available", "horizon_days": 30, "busy_events": events}


def test_fully_booked_week_expands_with_disclosure():
    # Next week (Aug 17–21) is wall-to-wall; the ladder must find the week
    # after AND say so — Kory reads "they asked for X, offering Y" on the card.
    ctx = _solid_week(["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"])
    merged, plan = _plan(SENDER_BODY, "")
    prop = propose_meeting_slots(
        ctx, intent="referral_or_intro", subject="Intro call", body=merged, plan=plan,
        reference_now=NOW,
    )
    assert prop.slots, prop.diagnostics
    assert prop.diagnostics.get("window_expanded") is True
    assert prop.diagnostics.get("original_window")
    assert prop.diagnostics.get("expanded_window")
    for s in prop.slots:
        assert datetime.fromisoformat(s["start"]).astimezone(MT).date().isoformat() >= "2026-08-24"


def test_total_failure_message_names_the_requested_window():
    msg = build_failure_kory_message(intent="coffee", requested_label="next week")
    assert "coffee" in msg
    assert "next week" in msg
    assert "?" in msg  # it asks Kory, it doesn't guess
