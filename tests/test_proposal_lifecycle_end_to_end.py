"""One scheduling thread from cold email to booked meeting — with every step
delivered twice.

Kory's complaint was never "the times are wrong". It was that Lexi breaks when
he uses her. Every failure behind that turned out to be the same thing: a step
happening a second time, from a retry, a redelivered webhook, a card tap racing
a typed command, or a follow-up landing on a thread mid-flight. The single-step
tests all passed while the second delivery quietly rewrote history.

So each step here runs TWICE and the assertion is that the second one changes
nothing: no second email, no second set of holds, no second meeting, no status
that contradicts what already happened in the world.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import (
    cancel_booked_meeting,
    execute_lexi_approval,
    mark_recipient_reoffer_request,
    mark_recipient_slot_choice,
)
from app.agents.scheduler_agent import process_proposal_schedule
from app.scheduling.proposal_state import (
    ProposalStatus,
    offer_already_sent,
    offer_is_outstanding,
)
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "lifecycle-thread"
CONV = "lifecycle-conversation"
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}


def _weekday(offset: int) -> date:
    day = date.today() + timedelta(days=offset)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _slots(count: int = 2) -> list[dict[str, str]]:
    out = []
    for index in range(count):
        d = _weekday(10 + index * 2)
        out.append({
            "start": datetime(d.year, d.month, d.day, 9 + index, 0, tzinfo=MT).isoformat(),
            "end": datetime(d.year, d.month, d.day, 9 + index, 30, tzinfo=MT).isoformat(),
        })
    return out


@pytest.fixture
def proposal():
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, conversation_id, subject,"
            " sender, sender_email, raw_body) VALUES (?,?,?,?,?,?)",
            (THREAD, CONV, "[TEST] Intro call?", "Dana <dana@example.com>",
             "dana@example.com", "Could we find 30 minutes in the next couple of weeks?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, drafted_reply, recipient_timezone) VALUES (?,?,?,?,?,?)",
            (THREAD, ProposalStatus.PENDING_APPROVAL, "referral_or_intro",
             json.dumps(_slots()), "Hi Dana,\n\nA couple of options.\n\nLet's Win,\nKory",
             "America/Denver"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _row(pid: int):
    with get_lexi_connection() as conn:
        return conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()


def _hold_count(pid: int) -> int:
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE proposal_id = ?"
            " AND COALESCE(expires_at,'') != 'released'", (pid,)
        ).fetchone()["n"]


def _approve(pid: int, phase: str = "send_offer"):
    sent: list[int] = []

    def _send(proposal, result):
        sent.append(1)
        return True, None

    holds: list[int] = []

    def _place(conn, **kwargs):
        for slot in kwargs.get("slots") or []:
            conn.execute(
                "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)"
                " VALUES (?,?,?,?,?)",
                (kwargs["proposal_id"], f"evt-{slot['start']}", slot["start"],
                 slot["end"], (datetime.now(MT) + timedelta(days=3)).isoformat()),
            )
            holds.append(1)
        return len(kwargs.get("slots") or [])

    with (
        patch("app.agents.comms_agent._send_drafted_reply", side_effect=_send),
        patch("app.integrations.hold_placement.place_offered_holds", side_effect=_place),
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value=FREE,
        ),
        patch("app.agents.comms_agent._confirm_selected_hold", return_value=("evt-confirmed", [])),
        patch("app.agents.comms_agent._confirm_time_conflict", return_value=None),
        patch("app.agents.comms_agent.delete_calendar_event"),
    ):
        result = execute_lexi_approval(
            pid, "approved", _slots()[0]["start"], "kory",
            decision_source="test", execution_phase=phase,
        )
    return result, len(sent)


# ---------------------------------------------------------------------------


def test_the_whole_flow_survives_every_step_happening_twice(proposal):
    pid = proposal

    # --- 1. Kory approves the offer. Then the tool times out and retries. ----
    first, sends_1 = _approve(pid)
    assert first.ok and first.status == ProposalStatus.OFFER_SENT, first.errors
    assert sends_1 == 1
    holds_after_first = _hold_count(pid)
    assert holds_after_first == 2, "an offer holds every time it offers"

    second, sends_2 = _approve(pid)
    assert sends_2 == 0, "the retry sent the counterpart a SECOND offer"
    assert _hold_count(pid) == holds_after_first, "the retry placed a second set of holds"
    assert second.status == ProposalStatus.OFFER_SENT

    with get_lexi_connection() as conn:
        assert offer_already_sent(conn, pid) is True
        assert offer_is_outstanding(conn, pid) is True

    # --- 2. A redelivered webhook pushes the thread back through the engine --
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        assert process_proposal_schedule(pid) is False, (
            "the engine re-staged a proposal whose offer is in the recipient's inbox"
        )
    row = _row(pid)
    assert row["status"] == ProposalStatus.OFFER_SENT
    assert "Let's Win" in row["drafted_reply"], "the sent draft was rewritten"

    # --- 3. Dana picks a time. The poller delivers her reply twice. ----------
    chosen = _slots()[0]
    assert mark_recipient_slot_choice(pid, chosen, reply_body="Tuesday works").get("ok")
    assert _row(pid)["status"] == ProposalStatus.PENDING_INVITE
    again = mark_recipient_slot_choice(pid, chosen, reply_body="Tuesday works")
    assert again.get("ok") is False, "the duplicate reply was processed as a new pick"
    assert _row(pid)["status"] == ProposalStatus.PENDING_INVITE

    # --- 4. Kory sends the invite. Card tap plus typed command. -------------
    invite, _ = _approve(pid, phase="send_invite")
    assert invite.ok, invite.errors
    assert _row(pid)["status"] == ProposalStatus.EXECUTED
    with get_lexi_connection() as conn:
        invited_at = conn.execute(
            "SELECT invite_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()["invite_sent_at"]
    assert invited_at, "the booking was not recorded as a world fact"

    _approve(pid, phase="send_invite")
    with get_lexi_connection() as conn:
        assert conn.execute(
            "SELECT invite_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()["invite_sent_at"] == invited_at, (
            "a second invite dispatch moved the booking timestamp"
        )

    # --- 5. Kory cancels. Then says it again. -------------------------------
    with patch("app.agents.comms_agent.delete_calendar_event"):
        with get_lexi_connection() as conn:
            conn.execute(
                "UPDATE proposals SET invite_event_id = 'evt-confirmed' WHERE id = ?", (pid,)
            )
            conn.commit()
        assert cancel_booked_meeting(pid, reason="conflict").get("ok") is True
        assert _row(pid)["status"] == ProposalStatus.CANCELLED
        repeat = cancel_booked_meeting(pid, reason="conflict")
    assert repeat.get("ok") is False, "cancelling twice must not be reported as a cancel"
    assert _row(pid)["status"] == ProposalStatus.CANCELLED


def test_a_decline_releases_the_holds_before_new_times_are_searched(proposal):
    """The re-offer round. Old holds must not survive into the new one."""
    pid = proposal
    _approve(pid)
    assert _hold_count(pid) == 2

    with patch("app.agents.comms_agent.delete_calendar_event"):
        declined = mark_recipient_reoffer_request(pid, reply_body="None of those work.")
    assert declined.get("ok") is True
    assert _row(pid)["status"] == ProposalStatus.PENDING_REOFFER
    assert _hold_count(pid) == 0, "declined times were left held on Kory's calendar"

    with get_lexi_connection() as conn:
        assert offer_already_sent(conn, pid) is True, "an email cannot be un-sent"
        assert offer_is_outstanding(conn, pid) is False, (
            "they declined and the holds came off — nothing is outstanding, so a "
            "new round is exactly what should happen"
        )


def test_a_second_approval_racing_the_first_never_double_sends(proposal):
    """A card tap and a typed `approve` arriving together."""
    pid = proposal
    results = []
    for _ in range(3):
        result, sends = _approve(pid)
        results.append((result.status, sends))
    statuses = [status for status, _ in results]
    total_sends = sum(sends for _, sends in results)
    assert total_sends == 1, f"the offer was emailed {total_sends} times"
    assert set(statuses) == {ProposalStatus.OFFER_SENT}


def test_every_status_change_in_the_whole_flow_is_explained(proposal):
    """An unexplained status change is what made these bugs invisible."""
    pid = proposal
    _approve(pid)
    mark_recipient_slot_choice(pid, _slots()[0], reply_body="works")

    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT message FROM audit_log WHERE reference_id = ?"
            " AND step_name = 'proposal_transition' ORDER BY id", (str(pid),)
        ).fetchall()
    messages = [r["message"] for r in rows]
    assert any("pending_approval -> offer_sent" in m for m in messages), messages
    assert any("offer_sent -> pending_invite" in m for m in messages), messages
    for message in messages:
        _, _, reason = message.partition(": ")
        assert reason.strip(), f"a status moved with no reason recorded: {message}"
