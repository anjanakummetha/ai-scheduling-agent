"""Regressions pinned from the 2026-08-15 adversarial review of 9068bd5.

Ten confirmed defects, each with the reviewer's exact reproduction input.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduling.inbound_availability import (
    extract_inbound_time_candidates,
    strip_quoted_reply,
)
from app.scheduling.preferences import SchedulingPreferences, _apply_freeform_fact
from app.scheduling.recipient_slot import match_recipient_slot_choice
from app.scheduling.scheduling_plan import SchedulingPlan, apply_guidance_window
from app.scheduling.scheduling_window import weekdays_from_guidance

MT = ZoneInfo("America/Denver")
REF = datetime(2026, 8, 14, 9, 0, tzinfo=MT)  # Friday

TWO_TUESDAY_SLOTS = [
    {"start": "2026-08-18T14:00:00-06:00", "end": "2026-08-18T14:30:00-06:00"},
    {"start": "2026-08-18T16:00:00-06:00", "end": "2026-08-18T16:30:00-06:00"},
]


def _starts(cands):
    return [c["start"] for c in cands]


def test_r1_mt_labeled_hour_beats_recipient_zone_reading():
    # ET recipient replies "4:00 PM MT works" — the MT label is explicit and
    # must pick the 16:00 MT slot, not 14:00 MT (= 4 PM ET).
    chosen = match_recipient_slot_choice(
        "4:00 PM MT works for me.",
        TWO_TUESDAY_SLOTS,
        recipient_tz="America/New_York",
    )
    assert chosen == TWO_TUESDAY_SLOTS[1]


def test_r2_unlabeled_continuation_shares_first_times_label():
    # "10:30 AM MT or 3:30 PM" from an Eastern sender: the continuation
    # inherits MT from the first time, not the sender's zone.
    cands = extract_inbound_time_candidates(
        "Monday August 17 at 10:30 AM MT or 3:30 PM works for me.",
        reference=REF,
        default_tz="America/New_York",
    )
    assert _starts(cands) == [
        "2026-08-17T10:30:00-06:00",
        "2026-08-17T15:30:00-06:00",
    ]


def test_r3_counts_and_durations_are_not_clock_times():
    cands = extract_inbound_time_candidates(
        "Thursday works for us; it'll take around 5 minutes.", reference=REF
    )
    assert all("17:00" not in s for s in _starts(cands)), cands
    cands2 = extract_inbound_time_candidates(
        "Happy to meet Wednesday; we'll be around 10 people.", reference=REF
    )
    assert all("T10:00" not in s for s in _starts(cands2)), cands2


def test_r4_hyphenated_half_hour_is_not_january_second():
    cands = extract_inbound_time_candidates(
        "Could we do a 1/2-hour call at 3pm?", reference=REF
    )
    assert all(not s.startswith("2027-01-02") for s in _starts(cands)), cands


def test_r5_after_lift_clears_caps_not_floors():
    prefs = SchedulingPreferences()
    _apply_freeform_fact(prefs, "no meetings before 8:30 am on Tuesdays")
    _apply_freeform_fact(prefs, "nothing after 4 pm on Fridays")
    _apply_freeform_fact(prefs, "after 4 pm is fine today")
    assert prefs.earliest_start_by_day == {1: (8, 30)}  # floor untouched
    assert prefs.latest_end_by_day == {}  # cap lifted


def test_r5b_before_lift_clears_floors_not_caps():
    prefs = SchedulingPreferences()
    _apply_freeform_fact(prefs, "no meetings before 8:30 am on Tuesdays")
    _apply_freeform_fact(prefs, "nothing after 4 pm on Fridays")
    _apply_freeform_fact(prefs, "before 8 is fine for this one")
    assert prefs.earliest_start_by_day == {}
    assert prefs.latest_end_by_day == {4: (16, 0)}


def test_r6_bare_evening_cap_reads_pm():
    prefs = SchedulingPreferences()
    _apply_freeform_fact(prefs, "don't schedule anything after 8")
    assert prefs.latest_end_by_day == {-1: (20, 0)}


def test_r7_cant_do_day_is_a_negation():
    assert weekdays_from_guidance(
        "the sender can't do Friday, pick another day"
    ) == {0, 1, 2, 3}
    assert weekdays_from_guidance("try Thursday instead of Friday") == {3}


def test_r8_thanks_clause_does_not_swallow_the_proposal():
    cands = extract_inbound_time_candidates(
        "Thanks for reaching out, Tuesday at 3 works for me.", reference=REF
    )
    assert _starts(cands) == ["2026-08-18T15:00:00-06:00"]
    # The genuinely-past reference stays suppressed.
    past = extract_inbound_time_candidates(
        "Thanks for the call Monday at 3. How about Friday at 2?", reference=REF
    )
    # Monday (the past call) suppressed; Friday-at-2 (same day as REF) kept.
    assert _starts(past) == ["2026-08-14T14:00:00-06:00"]


def test_r9_possessive_today_does_not_collapse_the_window():
    plan = SchedulingPlan(task_type="offer_times")
    apply_guidance_window(plan, "redo it, today's draft was too stiff")
    assert plan.window is None
    # A real window directive still applies.
    plan2 = SchedulingPlan(task_type="offer_times")
    apply_guidance_window(plan2, "offer the week of August 24 instead")
    assert plan2.window is not None
    assert plan2.window.source == "kory_guidance"


def test_r10_senders_own_on_line_survives_quote_stripping():
    body = (
        "Sounds good.\n"
        "On Monday I can do 2 pm, otherwise Wednesday morning.\n"
        "On Thu, Jul 24, 2026 at 3:12 PM Lexi Knightly <lexi@iconicfounders.com> wrote:\n"
        "> Here are a few times.\n"
    )
    kept = strip_quoted_reply(body)
    assert "On Monday I can do 2 pm" in kept
    assert "wrote:" not in kept
    # And the wrapped two-line attribution is still stripped.
    wrapped = (
        "Sounds good!\n\n"
        "On Thu, Jul 24, 2026 at 3:12 PM Lexi Knightly\n"
        "<lexi@iconicfounders.com> wrote:\n"
        "> • Monday, August 17 at 10:30 AM MT\n"
    )
    assert strip_quoted_reply(wrapped) == "Sounds good!"


def test_r11_avoid_mornings_guidance_does_not_become_mornings_only():
    from app.scheduling.slot_engine import propose_meeting_slots

    plan = SchedulingPlan(task_type="offer_times", kory_guidance="avoid mornings")
    prop = propose_meeting_slots(
        {"status": "available", "horizon_days": 21, "busy_events": []},
        intent="referral_or_intro",
        subject="Intro",
        body="Would love to find 30 minutes to connect next week.",
        plan=plan,
        reference_now=REF,
    )
    assert prop.slots
    assert any(
        datetime.fromisoformat(s["start"]).astimezone(MT).hour >= 12
        for s in prop.slots
    ), prop.slots


def test_r12_temporally_scoped_block_is_not_standing():
    prefs = SchedulingPreferences()
    _apply_freeform_fact(prefs, "no meetings on Friday this week")
    assert prefs.blocked_weekdays == set()
    standing = SchedulingPreferences()
    _apply_freeform_fact(standing, "no meetings on Fridays")
    assert standing.blocked_weekdays == {4}
