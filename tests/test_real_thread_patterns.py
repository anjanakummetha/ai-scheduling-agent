"""Patterns pinned from REAL back-and-forth threads in Kory's mailbox.

Sourced 2026-08-16 from a read-only extraction of 72 multi-message scheduling
conversations (inbox + sent, ~60 full bodies). Names/companies are swapped;
the structural phrasing is verbatim from what his real correspondents write.
The corpus itself stays out of git.

The scenarios: a counterpart EA offering day-header WINDOWS ("Monday,
August 17: 9am-10am or 12pm-1pm ET"), an EA-style pick from those windows,
a "push to later this week" reschedule, board-date ranges with a stated
blackout week, and Outlook's glued mid-line reply headers.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.lexi_thread_followup import _RESCHEDULE_RE
from app.scheduling.inbound_availability import (
    body_looks_like_inbound_availability,
    extract_inbound_time_candidates,
    strip_quoted_reply,
)
from app.scheduling.recipient_slot import match_recipient_slot_choice

MT = ZoneInfo("America/Denver")
REF = datetime(2026, 8, 13, 9, 0, tzinfo=MT)  # Thursday


def _starts(cands):
    return [c["start"] for c in cands]


EA_WINDOWS = (
    "Hi Kory,\n"
    "Apologies the delay!\n"
    "Would one of the following times work?\n"
    "Friday, August 14: 1pm-3pm ET\n"
    "Monday, August 17: 9am-10am or 12pm-1pm ET\n"
    "Tuesday, August 18: 9am-1pm ET\n"
    "Thanks,\nCarla\n"
)


def test_ea_day_header_windows_all_parse():
    """The real EA-availability shape. The 'or 12pm-1pm' second window was
    the one the human EA ultimately booked — it must not be dropped."""
    assert body_looks_like_inbound_availability(EA_WINDOWS)
    cands = extract_inbound_time_candidates(
        EA_WINDOWS, reference=REF, default_tz="America/New_York"
    )
    starts = _starts(cands)
    assert "2026-08-14T11:00:00-06:00" in starts  # Fri 1pm ET
    assert "2026-08-17T07:00:00-06:00" in starts  # Mon 9am ET
    assert "2026-08-17T10:00:00-06:00" in starts  # Mon 12pm ET (the or-window)
    assert "2026-08-18T07:00:00-06:00" in starts  # Tue 9am ET


def test_minuteless_range_trailing_zone_label():
    # "1pm-3pm ET" — real EAs skip the minutes; the trailing label must
    # still reach the range start even with no stored sender tz.
    cands = extract_inbound_time_candidates(
        "Would Friday, August 14: 1pm-3pm ET work?", reference=REF
    )
    assert _starts(cands) == ["2026-08-14T11:00:00-06:00"]


def test_ea_style_pick_resolves_in_recipient_zone():
    """Heidi's real phrasing: 'Kory is available Monday, August 17th at
    12 pm ET' — an ET clock naming an MT-stored slot."""
    slots = [
        {"start": "2026-08-14T11:00:00-06:00", "end": "2026-08-14T11:30:00-06:00"},
        {"start": "2026-08-17T10:00:00-06:00", "end": "2026-08-17T10:30:00-06:00"},
        {"start": "2026-08-18T07:00:00-06:00", "end": "2026-08-18T07:30:00-06:00"},
    ]
    picked = match_recipient_slot_choice(
        "From the options you offered, Kory is available Monday, August 17th at 12 pm ET.",
        slots,
        recipient_tz="America/New_York",
    )
    assert picked == slots[1]


def test_push_to_later_this_week_is_a_reschedule_not_availability():
    """Real reschedule ask: 'can we push to later this week? Sorry to move
    around' — must hit the reschedule route, not the inbound-time parser."""
    body = "Kory - can we push to later this week? Sorry to move around"
    assert _RESCHEDULE_RE.search(body)
    assert not body_looks_like_inbound_availability(body)


def test_stated_blackout_dates_are_not_candidates():
    """Real board thread: 'Oct 5-6 would be best ... AGM Oct 13-14 and prep
    the week prior so anything else around then would be tough.' The
    blackout must not become an availability candidate."""
    body = (
        "Think moving the board meeting makes sense. Oct 5-6 in Chicago "
        "would be best (dinner 10/5, meeting 10/6). We have our Annual "
        "General Meeting Oct 13-14 and prep the week prior so anything "
        "else around then would be tough."
    )
    starts = _starts(
        extract_inbound_time_candidates(body, reference=REF, default_tz="America/New_York")
    )
    assert not any(s.startswith("2026-10-13") or s.startswith("2026-10-14") for s in starts)
    assert any(s.startswith("2026-10-05") for s in starts)


def test_glued_outlook_reply_header_is_stripped():
    """Outlook HTML→text glues the header mid-line ('...mobile deviceFrom:
    Matt M <matt@x.com>') — its 'August 7 at 6:42 AM' parsed as a proposed
    time a year out (real corpus find)."""
    body = (
        "Do you mind sending instructions for the day pass?\n"
        "Sent from my mobile deviceFrom: Matt Maley <matt@example.com>\n"
        "Date: Friday, August 7, 2026 at 6:42 AM\n"
        "To: Sam <sam@example.com>\n"
        "Subject: Re: Membership\n"
        "Original text here."
    )
    kept = strip_quoted_reply(body)
    assert "6:42" not in kept
    assert extract_inbound_time_candidates(body, reference=REF) == []


def test_kory_tied_up_reply_shape_is_not_availability_noise():
    """Kory's own real counter: 'I'm totally tied up tomorrow, but I could
    probably open some spots up on Friday and then next week is fairly
    open' — as guidance this must not invent clock times."""
    cands = extract_inbound_time_candidates(
        "I'm totally tied up tomorrow, but I could probably open some spots "
        "up on Friday and then next week is fairly open",
        reference=datetime(2026, 8, 12, 8, 52, tzinfo=MT),
    )
    # No explicit clock time exists — anything parsed is a defaulted day
    # candidate; it must at least not land on the "tied up" day (tomorrow).
    assert all(not s.startswith("2026-08-13") for s in _starts(cands))
