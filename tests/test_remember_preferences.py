"""`remember` must change the rules, not just the prose.

Kory's own words are stored under an opaque key (email:<thread-id>), so the
deterministic engine never saw them — he could tell Lexi he was fine with
lunch and still never be offered one.
"""

from __future__ import annotations

from app.scheduling.preferences import _apply_freeform_fact, SchedulingPreferences


def _prefs_from(sentence: str) -> SchedulingPreferences:
    prefs = SchedulingPreferences()
    _apply_freeform_fact(prefs, sentence)
    return prefs


def test_remembering_lunch_is_allowed():
    for sentence in (
        "I'm fine with lunch meetings on Fridays",
        "Kory is okay with lunches now",
        "yes - happy to do lunch meetings",
    ):
        assert _prefs_from(sentence).lunch_allowed is True, sentence


def test_remembering_lunch_is_off():
    for sentence in (
        "No lunch meetings, I work through lunch",
        "Don't book lunches for me",
    ):
        assert _prefs_from(sentence).lunch_allowed is False, sentence


def test_weekly_caps_from_plain_language():
    assert _prefs_from("cap me at 1 happy hour per week").happy_hour_max_per_week == 1
    assert _prefs_from("no more than 2 dinners a week").dinner_max_per_week == 2
    assert _prefs_from("only 2 meetings on a travel week").travel_week_max_meetings == 2


def test_unrelated_notes_change_nothing():
    baseline = SchedulingPreferences()
    for sentence in (
        "Remind me to review the term sheet Friday",
        "Kory prefers Cherry Creek for coffee",
        "The Turn podcast records on Tuesdays",
    ):
        prefs = _prefs_from(sentence)
        assert prefs.lunch_allowed == baseline.lunch_allowed, sentence
        assert prefs.happy_hour_max_per_week == baseline.happy_hour_max_per_week, sentence
        assert prefs.dinner_max_per_week == baseline.dinner_max_per_week, sentence
