"""A named weekday that disagrees with the named date must never be resolved silently.

Found 2026-08-17 while auditing the suite under a pinned clock. Two real defects,
one root cause — the parser kept the date and discarded the weekday:

1. **The common one.** A counterpart writes "Wednesday, September 10" when the
   10th is a Thursday. People get these pairs wrong constantly. Lexi booked the
   Thursday and said nothing, so whoever meant Wednesday found out at the meeting.

2. **The year-rollover one.** A draft written in September saying "Tuesday,
   August 18" resolved to August **2027** — 338 days out — with ok=True, no
   conflicts and no warnings, because a bare month-day rolls to its next future
   occurrence. The weekday check catches this for free: Aug 18 2026 is a Tuesday,
   Aug 18 2027 is a Wednesday.

We cannot know which half of the pair is the typo. Kory's standing rule is that
Lexi asks when she is unsure and never invents a time, so the only correct
behaviour is to refuse and name the contradiction.

Every date here is computed from the frozen clock, never hardcoded — hardcoded
fixtures are what let defect 2 hide in the first place.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from freezegun import freeze_time

from app.scheduling.draft_slot_sync import verify_draft_slots
from app.scheduling.inbound_availability import extract_inbound_time_candidates

_TODAY = "2026-09-01"  # a Tuesday
_CTX_FREE = {"status": "available", "busy_events": []}


def _next(weekday: int, weeks_out: int = 2) -> date:
    """A future date landing on `weekday`, well clear of today."""
    base = date(2026, 9, 1) + timedelta(weeks=weeks_out)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


def _name(d: date) -> str:
    return d.strftime("%A")


def _wrong_name(d: date) -> str:
    """A weekday name that is deliberately NOT the one `d` falls on.

    Stays inside Mon-Fri: the ordinal pattern only consumes weekday prefixes it
    recognises, and it does not list Saturday or Sunday.
    """
    return _WEEKDAY_NAMES[(d.weekday() + 1) % 5]


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@freeze_time(_TODAY)
def test_matching_weekday_and_date_is_not_flagged():
    d = _next(2)  # a Wednesday
    text = f"Can we meet {_name(d)}, {d:%B} {d.day} at 2:00 PM?"
    cands = extract_inbound_time_candidates(text)
    assert cands, text
    assert not any(c.get("weekday_mismatch") for c in cands)


@freeze_time(_TODAY)
def test_month_date_disagreeing_with_weekday_is_flagged():
    d = _next(2)
    text = f"Can we meet {_wrong_name(d)}, {d:%B} {d.day} at 2:00 PM?"
    cands = extract_inbound_time_candidates(text)
    assert cands
    mm = cands[0]["weekday_mismatch"]
    assert mm["stated"] == _wrong_name(d)
    assert mm["actual"] == _name(d)


@freeze_time(_TODAY)
def test_ordinal_date_disagreeing_with_weekday_is_flagged():
    d = _next(3)  # a Thursday
    text = f"How about {_wrong_name(d)} the {d.day}th at 2:00 PM?"
    cands = extract_inbound_time_candidates(text)
    assert cands
    assert cands[0]["weekday_mismatch"]["actual"] == _name(d)


@freeze_time(_TODAY)
@pytest.mark.parametrize(
    "text",
    [
        "Can we meet Wednesday at 2:00 PM?",  # weekday alone — nothing to contradict
        "Can we meet September 16 at 2:00 PM?",  # date alone
    ],
)
def test_no_false_positive_without_a_stated_pair(text: str):
    for c in extract_inbound_time_candidates(text):
        assert not c.get("weekday_mismatch"), text


@freeze_time(_TODAY)
def test_send_gate_refuses_a_draft_whose_weekday_contradicts_its_date():
    d = _next(2)
    draft = (
        "Hi Heidi,\n\nHere are a few times:\n\n"
        f"• {_wrong_name(d)}, {d:%B} {d.day} at 1:00-1:30 PM MT\n\n"
        "Let me know!\n"
    )
    check = verify_draft_slots(
        draft_body=draft,
        intent="internal_sync",
        subject="Check in",
        calendar_context=_CTX_FREE,
    )
    assert not check.ok
    joined = " ".join(check.conflicts)
    assert _name(d) in joined and _wrong_name(d) in joined
    assert "Confirm which one is right" in joined


@freeze_time("2026-09-14")
def test_a_stale_month_day_rolling_into_next_year_is_caught():
    """The 338-days-out defect: "Tuesday, August 18" written in September.

    August 18 2026 was a Tuesday; the parser rolls a past month-day forward to
    2027, where the 18th is a Wednesday. Before this check the draft sailed
    through with ok=True.
    """
    draft = "Here are a few times:\n\n• Tuesday, August 18 at 1:00-1:30 PM MT\n"
    cands = extract_inbound_time_candidates(draft)
    assert cands
    resolved = datetime.fromisoformat(cands[0]["start"])
    assert resolved.year == 2027, "precondition: the parser still rolls the year"
    assert cands[0]["weekday_mismatch"]["stated"] == "Tuesday"

    check = verify_draft_slots(
        draft_body=draft,
        intent="internal_sync",
        subject="Check in",
        calendar_context=_CTX_FREE,
    )
    assert not check.ok, "a slot 11 months out must not pass the send gate silently"
