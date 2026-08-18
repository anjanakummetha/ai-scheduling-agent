""""Thursday the 27 at 9" — an ordinal date written without its suffix.

Found 2026-08-18 by the live full-flow E2E, which failed at the step where the
counterpart accepts a time. Its reply was "Thursday the 27 at 9 works for me",
and the parser resolved it to the 20th — the NEAREST Thursday, a week early.

The bare-weekday branch has a negative lookahead so an explicit date wins, but
that lookahead required an ordinal suffix (st/nd/rd/th). "the 27" has none, so
the weekday branch matched first and the day-of-month was discarded. People
drop the suffix constantly, and the failure is silent: a real date, a real
weekday, and a plausible time, just seven days wrong.

This is the same shape as the Curtis defect ("Thursday or Friday August 13th or
14th" landing on the nearest bare Thursday) that the lookahead was added for —
it simply did not cover the suffix-less form.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from freezegun import freeze_time

from app.scheduling.inbound_availability import extract_inbound_time_candidates
from app.scheduling.recipient_slot import match_recipient_slot_choice

TODAY = "2026-08-18"  # a Tuesday; the coming Thursday is the 20th


def _starts(text: str) -> list[str]:
    return [c["start"] for c in extract_inbound_time_candidates(text)]


@pytest.mark.parametrize(
    "text",
    [
        "Thursday the 27 at 9 works for me. Looking forward to it!",
        "Thursday the 27th at 9 works for me.",
        "the 27 at 9 works",
        "Let's do the 27 at 9.",
    ],
)
@freeze_time(TODAY)
def test_a_named_day_of_month_beats_the_nearest_weekday(text: str):
    starts = _starts(text)
    assert starts, text
    assert any(s.startswith("2026-08-27") for s in starts), (
        f"{text!r} resolved to {starts} — the 20th is the nearest Thursday, "
        "not the date they named"
    )
    assert not any(s.startswith("2026-08-20") for s in starts), starts


@freeze_time(TODAY)
def test_a_bare_weekday_with_no_date_still_means_the_nearest_one():
    starts = _starts("Can we meet Thursday at 9?")
    assert starts == ["2026-08-20T09:00:00-06:00"], starts


@freeze_time(TODAY)
def test_the_existing_suffixed_forms_are_unchanged():
    assert any(s.startswith("2026-08-20T12:00") for s in _starts("How about the 20th 12-3MT"))
    assert any(
        s.startswith("2026-09-13T12:00")
        for s in _starts("anytime after 12pm MST on Thursday the 13th")
    )


@freeze_time(TODAY)
def test_a_counted_noun_after_the_is_not_a_date():
    """"the 2 people" must not become the 2nd."""
    for text in (
        "I need the 2 people from your team there",
        "the 3 slides you sent were great",
    ):
        assert not _starts(text), f"{text!r} -> {_starts(text)}"


@freeze_time(TODAY)
def test_the_acceptance_matches_the_slot_that_was_offered():
    """The end of the flow: their yes has to land on the staged time."""
    offered = [
        {"start": "2026-08-27T09:00:00-06:00", "end": "2026-08-27T09:30:00-06:00"}
    ]
    picked = match_recipient_slot_choice(
        "Thursday the 27 at 9 works for me. Looking forward to it!", offered
    )
    assert picked is not None
    assert picked["start"] == offered[0]["start"]


@freeze_time(TODAY)
def test_a_suffixless_date_that_disagrees_with_its_weekday_is_still_caught():
    """The weekday check must survive the suffix becoming optional."""
    cands = extract_inbound_time_candidates("Monday the 27 at 9 works")
    assert cands
    mismatch = cands[0].get("weekday_mismatch")
    assert mismatch, "the 27th is a Thursday, not a Monday"
    assert mismatch["stated"] == "Monday"
    assert mismatch["actual"] == "Thursday"
