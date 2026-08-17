"""The two real exchanges Anjana provided for final production checks
(2026-08-16): the Curtis multi-round negotiation and the Steve/Heidi
multi-party thread. Lexi must reproduce the HUMAN outcome of each.

Dates are shifted future-relative where the flow needs live "now" semantics;
the phrasing is verbatim.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from app.scheduling.inbound_availability import (
    body_looks_like_inbound_availability,
    extract_inbound_time_candidates,
)
from app.scheduling.recipient_slot import recipient_times_rejected
from app.scheduling.schedule_from_context import schedule_from_context
from app.scheduling.scheduling_window import weekdays_from_guidance

MT = ZoneInfo("America/Denver")
REF = datetime(2026, 7, 22, 10, 2, tzinfo=MT)  # when Curtis wrote


def _starts(cands):
    return [c["start"] for c in cands]


def _fake_calendar(**_kw):
    return {"status": "available", "horizon_days": 45, "busy_events": []}


# --- Curtis, message by message ---


def test_curtis_1_day_pair_dates_no_phantom_weekday():
    # "Thursday or Friday August 13th or 14th" — both dates, and NOT the
    # nearest bare Thursday (which fell in the weeks he said he's swamped).
    cands = extract_inbound_time_candidates(
        "Could we schedule a call for Thursday or Friday August 13th or 14th? Let me know.",
        reference=REF,
    )
    assert _starts(cands) == [
        "2026-08-13T09:00:00-06:00",
        "2026-08-14T09:00:00-06:00",
    ]


def test_curtis_2_ordinal_windows_parse_on_the_right_week():
    # "anytime after 12pm MST on Thursday the 13th, or anytime after 10:30am
    # on Friday the 14th" — ordinals resolve to Aug 13/14, never to the
    # nearest bare weekday three weeks early.
    cands = extract_inbound_time_candidates(
        "I'm available at 9:30am MST or anytime after 12pm MST on Thursday "
        "the 13th, or anytime after 10:30am on Friday the 14th.",
        reference=REF,
    )
    starts = _starts(cands)
    assert "2026-08-13T12:00:00-06:00" in starts
    assert "2026-08-14T10:30:00-06:00" in starts
    assert not any(s.startswith("2026-07-2") for s in starts), starts


def test_kory_anything_the_following_week_is_a_week_shift():
    assert recipient_times_rejected(
        "I am at a Canopy board meeting in Chicago those days. "
        "Anything the following week?"
    )


def test_kory_shorthand_ordinal_and_glued_range():
    # "Most of the 19th is open, and the 20th 12-3MT."
    ref = datetime(2026, 8, 14, 9, 0, tzinfo=MT)
    cands = extract_inbound_time_candidates(
        "Most of the 19th is open, and the 20th 12-3MT.", reference=ref
    )
    assert "2026-08-20T12:00:00-06:00" in _starts(cands)


@freeze_time("2026-08-14")
def test_curtis_full_flow_kory_time_on_curtis_days():
    """Kory: 'Either day works at 9 mountain' → offer stages exactly the
    sender's two days at 9:00, replicating what Kory did by hand.

    Clock pinned to just before the real thread ran. This is a historical
    replay, so the dates in the ask are the point and must not be made
    relative — but schedule_from_context reads the live clock, so without the
    pin September 10th silently became a past date and the run fell through to
    the slot engine instead of the inbound-availability path.
    """
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        side_effect=_fake_calendar,
    ):
        r = schedule_from_context(
            subject="[TEST] Re: check in",
            body=(
                "Could we schedule a call for Thursday or Friday "
                "September 10th or 11th? Let me know."
            ),
            intent="referral_or_intro",
            sender_email="curtis@example.com",
            kory_scheduling_guidance="That's perfect. Either day works at 9 mountain.",
            use_llm_plan=False,
        )
    assert r.ok, getattr(r, "failure_message", "")
    assert r.path == "inbound_availability"
    assert _starts(r.slots) == [
        "2026-09-10T09:00:00-06:00",
        "2026-09-11T09:00:00-06:00",
    ]


# --- Steve / Heidi multi-party ---


def test_steve_window_ask_parses_reasonable_days():
    cands = extract_inbound_time_candidates(
        "I'd like to come see you in Denver some time Tuesday to Friday next "
        "week and can meet you virtually when you meet Bill. I'm planning to "
        "be in Houston 13th and 14th otherwise",
        reference=datetime(2026, 7, 29, 13, 17, tzinfo=MT),
    )
    assert body_looks_like_inbound_availability(
        "I'd like to come see you in Denver some time Tuesday to Friday next "
        "week and can meet you virtually when you meet Bill."
    )
    for s in _starts(cands):
        assert s.startswith("2026-08-0"), s  # next week, not this


def test_kory_monday_packed_means_tuesday():
    assert weekdays_from_guidance(
        "I am in Denver Monday Tuesday but I know Monday is pretty packed. "
        "Could something Tuesday work?"
    ) == {1}


def test_steve_full_flow_tuesday_only():
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        side_effect=_fake_calendar,
    ):
        r = schedule_from_context(
            subject="[TEST] ICCI - Iconic Founders",
            body="I'd like to come see you in Denver some time Tuesday to Friday next week.",
            intent="meeting_request",
            sender_email="steve@example.com",
            kory_scheduling_guidance=(
                "I know Monday is pretty packed. Could something Tuesday work?"
            ),
            use_llm_plan=False,
        )
    assert r.ok
    assert r.slots
    for s in r.slots:
        day = datetime.fromisoformat(s["start"]).astimezone(MT).strftime("%A")
        assert day == "Tuesday", s
