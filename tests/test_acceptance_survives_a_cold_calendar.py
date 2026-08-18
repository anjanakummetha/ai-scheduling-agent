"""Accepting a time we offered must not depend on reading the calendar.

The live end-to-end runs failed roughly one time in six, and the failure was
always the same: the counterpart's acceptance did not persist. It was always the
FIRST run after a restart, which is the signature of the cold per-process
calendar cache — the first availability read took 17.5 seconds where later ones
took 7ms.

The cause was ordering. The check for "did they accept a time we already
offered" sat BELOW a calendar read, so a read that was slow or unavailable
returned early and dropped the acceptance entirely: nothing recorded, holds left
on the calendar, meeting never booked.

That check needs no calendar. We validated the time before offering it and we
are holding it for them. Only a NEW time they propose needs one.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.lexi_thread_followup import try_handle_lexi_thread_followup
from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "cold-calendar-thread"
CONV = "cold-calendar-conversation"
SUBJECT = "[TEST] Quick call?"
COUNTERPART = "dana@example.com"

CALENDAR_DOWN = {"status": "unavailable", "error": "composio timeout", "busy_events": []}
CALENDAR_UP = {"status": "available", "horizon_days": 45, "busy_events": []}


def _offered_day() -> date:
    day = date.today() + timedelta(days=10)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


@pytest.fixture
def offer_sent():
    day = _offered_day()
    slots = [
        {
            "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
            "end": datetime(day.year, day.month, day.day, 9, 30, tzinfo=MT).isoformat(),
        },
        {
            "start": datetime(day.year, day.month, day.day, 14, 0, tzinfo=MT).isoformat(),
            "end": datetime(day.year, day.month, day.day, 14, 30, tzinfo=MT).isoformat(),
        },
    ]
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, conversation_id, subject,"
            " sender, sender_email, raw_body) VALUES (?,?,?,?,?,?)",
            (THREAD, CONV, SUBJECT, f"Dana <{COUNTERPART}>", COUNTERPART, "can we talk?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, recipient_timezone) VALUES (?,?,?,?,?)",
            (THREAD, ProposalStatus.OFFER_SENT, "referral_or_intro",
             json.dumps(slots), "America/Denver"),
        )
        pid = int(cur.lastrowid)
        record_fact(conn, pid, "offer_sent_at")
        conn.commit()
    yield pid, day, slots
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _reply(body: str) -> dict:
    return {
        "message_id": f"{THREAD}-reply",
        "thread_id": THREAD,
        "conversation_id": CONV,
        "subject": f"Re: {SUBJECT}",
        "sender": COUNTERPART,
        "raw_body": body,
    }


def _handle(body: str, calendar: dict):
    with (
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value=calendar,
        ),
        patch("app.bot.teams_publisher.schedule_teams_invite_prompt_push"),
    ):
        return try_handle_lexi_thread_followup(_reply(body)) or {}


def _state(pid: int):
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT status, recipient_selected_slot FROM proposals WHERE id = ?", (pid,)
        ).fetchone()


@pytest.mark.parametrize("calendar", [CALENDAR_UP, CALENDAR_DOWN],
                         ids=["calendar-reachable", "calendar-unreachable"])
def test_an_acceptance_persists_whether_or_not_the_calendar_answers(offer_sent, calendar):
    pid, day, slots = offer_sent
    weekday = datetime.fromisoformat(slots[0]["start"]).strftime("%A")

    _handle(f"{weekday} the {day.day} at 9 works for me. Looking forward to it!", calendar)

    row = _state(pid)
    assert row["status"] == ProposalStatus.PENDING_INVITE, (
        "the counterpart accepted a time we offered and are holding; a calendar "
        "read must not be able to swallow that"
    )
    picked = json.loads(row["recipient_selected_slot"])
    assert picked["start"] == slots[0]["start"]


def test_the_slot_is_recorded_as_WE_offered_it_not_as_they_wrote_it(offer_sent):
    """Bookings must use the times we validated and hold, not their prose."""
    pid, day, slots = offer_sent
    _handle(f"9am on the {day.day} is great, thanks.", CALENDAR_DOWN)

    picked = json.loads(_state(pid)["recipient_selected_slot"])
    assert picked["start"] == slots[0]["start"]
    assert picked["end"] == slots[0]["end"]


def test_a_brand_new_time_they_propose_still_requires_the_calendar(offer_sent):
    """The other half. A time we have never checked and are not holding must not
    be accepted while the calendar is unreadable."""
    pid, day, _slots = offer_sent
    other = day + timedelta(days=1)
    while other.weekday() >= 5:
        other = other + timedelta(days=1)

    _handle(
        f"None of those work — could we do {other.strftime('%A')} the {other.day} at 11am instead?",
        CALENDAR_DOWN,
    )

    row = _state(pid)
    assert row["status"] != ProposalStatus.PENDING_INVITE, (
        "an unvalidated new time must never be recorded as a confirmed pick"
    )
    assert not row["recipient_selected_slot"]
