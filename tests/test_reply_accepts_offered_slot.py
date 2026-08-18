"""The counterpart says yes — does the pick land on the staged slot?

This is the last step of the flow and the one the live E2E kept failing. Driving
it here rather than only live means the answer is repeatable, and it isolates
the production path from the E2E driver's own fixtures.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "reply-accept-thread"
SENDER = "anjanakummetha@gmail.com"


def _future(weekday: int, weeks: int = 2) -> date:
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks)
    return monday + timedelta(days=weekday)


def _slot(d: date, hour: int) -> dict[str, str]:
    start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=MT)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}


@pytest.fixture
def offer_sent():
    """A proposal in the state it is in right after the offer email goes out."""
    day = _future(3)  # Thursday
    slots = [_slot(day, 9)]
    _purge()
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, sender_email, raw_body)"
            " VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] Quick call?", SENDER, SENDER, "Could we find 30 minutes?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification, proposed_slots,"
            " drafted_reply) VALUES (?,?,?,?,?)",
            (THREAD, "offer_sent", "referral_or_intro", json.dumps(slots), "Offer sent."),
        )
        pid = cur.lastrowid
        conn.commit()
    yield pid, day, slots
    _purge()


def _purge() -> None:
    with get_lexi_connection() as conn:
        rows = conn.execute("SELECT id FROM proposals WHERE thread_id = ?", (THREAD,)).fetchall()
        for row in rows:
            conn.execute("DELETE FROM holds WHERE proposal_id = ?", (row["id"],))
            conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (row["id"],))
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _reply(body: str) -> dict:
    return {
        "message_id": f"{THREAD}-r1",
        "thread_id": THREAD,
        "conversation_id": THREAD,
        "subject": "Re: [TEST] Quick call?",
        "sender": SENDER,
        "raw_body": body,
    }


def _run(body: str):
    from app.agents.lexi_thread_followup import try_handle_lexi_thread_followup

    free = {"status": "available", "horizon_days": 45, "busy_events": []}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=free
    ):
        return try_handle_lexi_thread_followup(_reply(body)) or {}


def _stored(pid: int) -> tuple[str, str]:
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, COALESCE(recipient_selected_slot, '') AS pick FROM proposals WHERE id=?",
            (pid,),
        ).fetchone()
    return row["status"], row["pick"]


@pytest.mark.parametrize(
    "phrasing",
    [
        "{weekday} the {day} at 9 works for me. Looking forward to it!",
        "{weekday} the {day}th at 9 works for me.",
        "9am on the {day} is great, thanks.",
        "Let's lock in {weekday} at 9.",
    ],
)
def test_an_acceptance_records_the_offered_slot(offer_sent, phrasing: str):
    pid, day, slots = offer_sent
    body = phrasing.format(weekday=day.strftime("%A"), day=day.day)
    result = _run(body)
    assert result.get("ok"), f"{body!r} -> {result}"
    status, pick = _stored(pid)
    assert pick, f"{body!r} recorded no slot (status={status})"
    assert json.loads(pick)["start"] == slots[0]["start"], (
        f"{body!r} picked {pick}, offered {slots[0]['start']}"
    )


def test_a_rejection_does_not_record_a_pick(offer_sent):
    pid, _day, _slots = offer_sent
    _run("None of those work I'm afraid — anything the following week?")
    _status, pick = _stored(pid)
    assert not pick, "a rejection must never look like an acceptance"


def test_a_time_that_was_never_offered_is_not_silently_accepted(offer_sent):
    """They propose their own time — that is a new suggestion, not a pick of ours."""
    pid, day, slots = offer_sent
    other = day + timedelta(days=1)
    result = _run(f"Could we do {other.strftime('%A')} the {other.day} at 2 instead?")
    _status, pick = _stored(pid)
    if pick:
        assert json.loads(pick)["start"] != slots[0]["start"], (
            "a different day was recorded as acceptance of our slot"
        )
    assert result.get("action") != "recipient_slot_choice" or not pick


def test_the_reply_answers_the_offer_that_was_SENT_not_a_newer_draft(offer_sent):
    """Two proposals on one conversation — the live E2E's real failure.

    A newer proposal still sitting in pending_approval was winning the lookup
    on `ORDER BY id DESC`, so the counterpart's "that works" was recorded
    against a draft nobody had sent, while the offer they were actually
    replying to stayed untouched and the meeting was never booked.
    """
    sent_pid, day, slots = offer_sent
    later = _future(4)
    with get_lexi_connection() as conn:
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification, proposed_slots,"
            " drafted_reply) VALUES (?,?,?,?,?)",
            (
                THREAD,
                "pending_approval",
                "referral_or_intro",
                json.dumps([_slot(later, 11)]),
                "A newer draft, not sent.",
            ),
        )
        newer_pid = cur.lastrowid
        conn.commit()
    assert newer_pid > sent_pid, "precondition: the unsent draft is newer"

    result = _run(f"{day.strftime('%A')} the {day.day} at 9 works for me.")
    assert result.get("ok"), result

    sent_status, sent_pick = _stored(sent_pid)
    _newer_status, newer_pick = _stored(newer_pid)
    assert sent_pick, "the acceptance was not recorded against the offer that was sent"
    assert json.loads(sent_pick)["start"] == slots[0]["start"]
    assert not newer_pick, "the acceptance leaked onto an unsent draft"


def test_our_own_hold_does_not_block_them_accepting_the_slot_we_offered(offer_sent):
    """The booking-breaking one, found by the live E2E on 2026-08-18.

    When we offer times we HOLD them, so a HOLD event sits on the calendar at
    exactly the time we offered. Re-validating the acceptance against a calendar
    containing our own hold made Lexi refuse it as "already booked" — the
    meeting never got booked and the hold stayed. It looked intermittent only
    because the calendar cache sometimes still held a pre-hold read.
    """
    pid, day, slots = offer_sent
    start = datetime.fromisoformat(slots[0]["start"])
    our_hold = {
        "subject": f"HOLD: Intro call — {SENDER}",
        "start": {"dateTime": slots[0]["start"]},
        "end": {"dateTime": slots[0]["end"]},
    }
    from app.agents.lexi_thread_followup import try_handle_lexi_thread_followup

    busy = {"status": "available", "horizon_days": 45, "busy_events": [our_hold]}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=busy
    ):
        result = try_handle_lexi_thread_followup(_reply(
            f"{day.strftime('%A')} the {day.day} at {start.hour} works for me."
        )) or {}
    assert result.get("action") != "inbound_time_blocked", (
        "our own hold blocked them accepting the slot we are holding for them"
    )
    _status, pick = _stored(pid)
    assert pick, f"acceptance not recorded: {result}"
    assert json.loads(pick)["start"] == slots[0]["start"]


def test_a_genuinely_booked_time_they_propose_is_still_refused(offer_sent):
    """The guard must not become a blanket 'accept anything'."""
    pid, day, _slots = offer_sent
    other = day + timedelta(days=1)
    clash = {
        "subject": "Board call",
        "start": {"dateTime": _slot(other, 14)["start"]},
        "end": {"dateTime": _slot(other, 14)["end"]},
    }
    from app.agents.lexi_thread_followup import try_handle_lexi_thread_followup

    busy = {"status": "available", "horizon_days": 45, "busy_events": [clash]}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=busy
    ):
        try_handle_lexi_thread_followup(_reply(
            f"Actually could we do {other.strftime('%A')} the {other.day} at 2 instead?"
        ))
    _status, pick = _stored(pid)
    assert not pick, "a time that clashes with a real meeting was accepted"
