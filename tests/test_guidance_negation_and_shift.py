"""Two defects found 2026-08-17 while replaying multi-round negotiations.

Both sit in the most common re-offer in scheduling: the counterpart declines and
Kory redirects. Lexi recognised the decline and then failed to act on it.

1. **A week push had nowhere to go.** "the following week" and "the week after"
   were not recognised as windows at all, so "none of those work — anything the
   following week?" produced the SAME week again. recipient_times_rejected has
   matched that exact sentence since the Curtis thread, so Lexi saw the
   rejection, agreed with it, and re-offered what had just been refused.

2. **A ruled-out date became the target.** "Avoid September 15" inferred a
   window OF the 15th, so the one day Kory excluded became the only day offered.
   This is the date-shaped twin of the "can't do Friday restricted TO Friday"
   defect, which was fixed for weekday names and never for dates.

Fixing the inversion alone was not enough — the 15th stopped being the only
option but stayed among the options, so exclusions are now honoured by the
engine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from app.scheduling.schedule_from_context import schedule_from_context
from app.scheduling.scheduling_plan import excluded_dates_from_guidance
from app.scheduling.scheduling_window import infer_scheduling_window

MT = ZoneInfo("America/Denver")
TODAY = "2026-09-01"  # Tuesday
CTX = {"status": "available", "horizon_days": 45, "busy_events": []}


def _offer(guidance: str = "", body: str = "Any time in the next two weeks."):
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=CTX
    ):
        return schedule_from_context(
            subject="[TEST] intro",
            body=body,
            intent="referral_or_intro",
            sender_email="c@example.com",
            kory_scheduling_guidance=guidance,
            use_llm_plan=False,
            calendar_context=CTX,
        )


def _days(r) -> set[date]:
    return {datetime.fromisoformat(s["start"]).date() for s in r.slots}


# --- 1. the week push -----------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    ["the following week", "following week", "the week after", "the week after next"],
)
@freeze_time(TODAY)
def test_a_week_push_resolves_to_the_week_after_next(phrase: str):
    window = infer_scheduling_window(body=phrase)
    assert window is not None, f"{phrase!r} inferred no window at all"
    assert window.start == date(2026, 9, 14), phrase
    assert window.end == date(2026, 9, 20), phrase


@freeze_time(TODAY)
def test_next_week_is_unchanged_by_the_new_pattern():
    window = infer_scheduling_window(body="next week")
    assert (window.start, window.end) == (date(2026, 9, 7), date(2026, 9, 13))
    assert window.label == "next week"


@freeze_time(TODAY)
def test_the_week_after_next_is_not_swallowed_by_next_week():
    """"the week after next" contains the words "next week" — order matters."""
    window = infer_scheduling_window(body="can we look at the week after next")
    assert window.start == date(2026, 9, 14)


@freeze_time(TODAY)
def test_a_rejected_offer_re_offered_with_a_push_actually_moves():
    first = _offer(body="Next week if possible.")
    pushed = _offer("They want the following week instead.", body="Next week if possible.")
    assert pushed.ok, pushed.failure_message
    assert _days(pushed), "a push must still produce times"
    assert min(_days(pushed)) > max(_days(first)), (
        f"re-offered {sorted(_days(pushed))} after {sorted(_days(first))}"
    )


# --- 2. ruled-out dates ---------------------------------------------------


@pytest.mark.parametrize(
    "guidance",
    [
        "Not the 15th.",
        "Avoid September 15.",
        "Anything but the 15th.",
        "They can't do the 15th anymore — find something else.",
        "The 15th is out.",
    ],
)
@freeze_time(TODAY)
def test_a_ruled_out_date_is_neither_targeted_nor_offered(guidance: str):
    excluded = excluded_dates_from_guidance(guidance)
    assert date(2026, 9, 15) in excluded, guidance
    result = _offer(guidance)
    assert result.ok, result.failure_message
    assert result.slots, "excluding one day must not empty the offer"
    assert date(2026, 9, 15) not in _days(result), guidance


@freeze_time(TODAY)
def test_a_positive_date_steer_is_not_mistaken_for_an_exclusion():
    """"Week of September 14" must still MEAN the week of the 14th."""
    assert excluded_dates_from_guidance("Week of September 14.") == set()
    result = _offer("Week of September 14.")
    assert result.ok
    for day in _days(result):
        assert date(2026, 9, 14) <= day <= date(2026, 9, 20), day


@freeze_time(TODAY)
def test_an_exclusion_and_a_push_in_one_sentence_both_apply():
    result = _offer("Not the 15th, try the following week.")
    assert result.ok, result.failure_message
    assert date(2026, 9, 15) not in _days(result)
    assert result.slots


@freeze_time(TODAY)
def test_weekday_guidance_is_untouched_by_date_exclusion_logic():
    assert excluded_dates_from_guidance("Thursdays only.") == set()
    result = _offer("Thursdays only.")
    assert {d.weekday() for d in _days(result)} == {3}


@freeze_time(TODAY)
def test_the_exclusion_is_recorded_in_diagnostics():
    """A wrong exclusion must be visible, not mysterious."""
    result = _offer("Avoid September 15.")
    assert result.diagnostics.get("excluded_dates") == ["2026-09-15"]
