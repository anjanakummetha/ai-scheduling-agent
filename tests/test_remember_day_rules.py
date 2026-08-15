"""Remembered day rules must reach the VALIDATOR, not just the prompt.

Live K-3 (RUN 8): "remember: no meetings before 8:30 AM MT Tuesdays" was
stored and recalled perfectly — and the engine offered Tuesday 7:00 AM
anyway, because engine slots never pass through the prompt. These pin the
2026-08-15 fix: weekday blocks, per-day/global floors, and after-caps parsed
from Kory's own words and enforced in validate_proposal_slots.
"""

from __future__ import annotations

from app.rules.validators import validate_proposal_slots
from app.scheduling.preferences import SchedulingPreferences, _apply_freeform_fact


def _prefs_from(*sentences: str) -> SchedulingPreferences:
    prefs = SchedulingPreferences()
    for sentence in sentences:
        _apply_freeform_fact(prefs, sentence)
    return prefs


def _validate(slot_start: str, slot_end: str, prefs: SchedulingPreferences):
    return validate_proposal_slots(
        [{"start": slot_start, "end": slot_end}],
        intent="referral_or_intro",
        meeting_format="virtual",
        preferences=prefs,
    )


# --- parsing ---


def test_no_meetings_on_fridays_blocks_the_day():
    prefs = _prefs_from("No meetings on Fridays going forward")
    assert prefs.blocked_weekdays == {4}


def test_keep_mondays_clear_blocks_the_day():
    assert _prefs_from("Keep Mondays clear please").blocked_weekdays == {0}


def test_kory_k3_floor_sentence_parses():
    prefs = _prefs_from("no meetings before 8:30 AM MT Tuesdays")
    assert prefs.earliest_start_by_day == {1: (8, 30)}


def test_global_floor_parses():
    prefs = _prefs_from("nothing before 10 am please")
    assert prefs.earliest_start_by_day == {-1: (10, 0)}


def test_after_cap_parses_per_day():
    prefs = _prefs_from("Don't schedule anything after 4 pm on Fridays")
    assert prefs.latest_end_by_day == {4: (16, 0)}


def test_daypart_sentence_is_not_a_full_day_block():
    # "no meetings Friday afternoons" is a time rule; blocking all of Friday
    # would over-block. (The afternoon nuance stays prompt-side for now.)
    prefs = _prefs_from("no meetings on Friday afternoons")
    assert 4 not in prefs.blocked_weekdays


def test_third_party_prose_does_not_block_days():
    prefs = _prefs_from("The Turn podcast records on Tuesdays")
    assert prefs.blocked_weekdays == set()
    assert prefs.earliest_start_by_day == {}


# --- enforcement ---


def test_blocked_friday_slot_is_refused():
    prefs = _prefs_from("No meetings on Fridays")
    # Friday 2026-08-21 10:00 MT — otherwise perfectly valid.
    result = _validate("2026-08-21T10:00:00-06:00", "2026-08-21T10:30:00-06:00", prefs)
    assert not result.valid
    assert any("saved rule" in v and "Friday" in v for v in result.violations)


def test_k3_tuesday_early_slot_is_refused_and_late_allowed():
    prefs = _prefs_from("no meetings before 8:30 AM MT Tuesdays")
    early = _validate("2026-08-18T07:00:00-06:00", "2026-08-18T07:30:00-06:00", prefs)
    assert not early.valid
    assert any("08:30" in v for v in early.violations)
    fine = _validate("2026-08-18T09:00:00-06:00", "2026-08-18T09:30:00-06:00", prefs)
    assert fine.valid, fine.violations


def test_after_cap_refuses_late_friday():
    prefs = _prefs_from("Don't schedule anything after 4 pm on Fridays")
    result = _validate("2026-08-21T16:30:00-06:00", "2026-08-21T17:00:00-06:00", prefs)
    assert not result.valid
    # Other weekdays untouched.
    ok = _validate("2026-08-20T16:30:00-06:00", "2026-08-20T17:00:00-06:00", prefs)
    assert ok.valid, ok.violations


def test_guidance_unblocks_a_day_for_this_run():
    # Standing rule from memory + a per-proposal exception from Teams.
    prefs = _prefs_from("No meetings on Fridays", "Friday is fine for this one")
    assert prefs.blocked_weekdays == set()


def test_guidance_lifts_a_floor_for_this_run():
    prefs = _prefs_from("nothing before 10 am", "early is fine for this one")
    assert prefs.earliest_start_by_day == {}
