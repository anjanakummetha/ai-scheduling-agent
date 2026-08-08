"""Locks in the 2026-08-08 preference-conformance fixes from the paused
meeting-type audit sweep (docs/PREFERENCES_AUDIT.md) + the scheduling-problem
sweep: venue addresses on invites, sender-stated durations honored, meal cues
outranking podcast text cues, lunch bookable at 60, reschedule = 2 options,
and warnings reaching Kory's Teams renderers."""

from __future__ import annotations

from app.scheduling.invite_builder import default_location_for_intent, is_online_meeting
from app.scheduling.meeting_type import resolve_meeting_type


def test_coffee_location_carries_street_address():
    loc = default_location_for_intent("coffee")
    assert loc.startswith("Olive & Finch (")
    assert "Denver, CO" in loc


def test_happy_hour_location_is_clean_name_plus_address():
    loc = default_location_for_intent("happy_hour", "2026-08-11T15:30:00-06:00")
    assert loc.startswith("Cherry Creek Grill (")
    assert "opens 3:30" not in loc  # the old annotation leaked onto invites


def test_happy_hour_never_offers_a_closed_venue():
    # Quality Italian opens at 4:00 — a 3:30 slot must never land there.
    loc_330 = default_location_for_intent("happy_hour", "2026-08-11T15:30:00-06:00")
    assert "Quality Italian" not in loc_330


def test_venue_locations_read_as_in_person():
    for intent in ("coffee", "happy_hour"):
        loc = default_location_for_intent(intent, "2026-08-11T16:00:00-06:00")
        assert is_online_meeting(intent, loc) is False


def test_referral_honors_stated_45_minutes():
    spec = resolve_meeting_type(
        intent="referral_or_intro",
        subject="Intro call?",
        body="Would 45 minutes work for a call next week?",
    )
    assert spec.duration_minutes == 45


def test_coffee_before_recording_is_still_coffee():
    spec = resolve_meeting_type(
        intent="meeting_request",
        subject="Coffee before the recording session?",
        body="Would love to grab coffee before we record.",
    )
    assert spec.type_key == "coffee"
    assert spec.calendar_block_minutes == 90


def test_podcast_triage_intent_still_wins():
    spec = resolve_meeting_type(
        intent="podcast",
        subject="The Turn scheduling",
        body="Excited to record an episode.",
    )
    assert spec.type_key == "podcast"


def test_lunch_books_a_real_hour_not_30_minutes():
    spec = resolve_meeting_type(intent="lunch_request", subject="Lunch?", body="")
    assert spec.duration_minutes == 60


def test_reschedule_offers_two_options_max():
    from unittest.mock import patch

    from app.scheduling import slot_engine

    captured: dict[str, int] = {}
    original = slot_engine._candidate_start_times

    def spy(*args, **kwargs):
        return original(*args, **kwargs)

    with patch.object(slot_engine, "_candidate_start_times", side_effect=spy):
        proposal = slot_engine.find_valid_slots(
            {"status": "available", "busy_events": []},
            intent="reschedule",
            subject="Need to move our call",
            body="Can we find a new time next week?",
        )
    captured["n"] = len(proposal.slots)
    assert captured["n"] <= 2


def test_teams_renderers_surface_warnings():
    """The E-6 remedy copy lives in result.warnings — every Teams render path
    must include it (it was composed and silently dropped before)."""
    from app.teams.commands import _result_warnings

    class FakeResult:
        warnings = ["Holds are intact — clear the clash and re-approve."]

    assert "clear the clash" in _result_warnings(FakeResult())

    class NoWarnings:
        warnings = None

    assert _result_warnings(NoWarnings()) == ""
