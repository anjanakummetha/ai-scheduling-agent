"""The advertised commands must DO the thing, not merely parse.

`send invite #N` is the step that books the meeting, and `retry scheduling for
#N — <times>` is how Kory answers an escalation. Both were advertised by Lexi
and neither reached a handler; they fell through to "Hermes may reply
conversationally", leaving the model to guess which tool to call on the two
most consequential actions in the flow.

Driven through tests/teams_parity.py, the gateway's own entry point.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection
from tests.teams_parity import message_of, teams

MT = ZoneInfo("America/Denver")
THREAD = "advertised-act-thread"


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=12)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 10, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 10, 30, tzinfo=MT).isoformat(),
    }


def _make(status: str, *, selected: dict | None = None) -> int:
    slot = _slot()
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] advertised", "Dana <dana@example.com>",
             "dana@example.com", "Can we meet the week after next?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, drafted_reply, recipient_selected_slot)"
            " VALUES (?,?,?,?,?,?)",
            (THREAD, status, "referral_or_intro", json.dumps([slot]),
             "Hi Dana,\n\nHappy to find a time.\n\nLet's Win,\nKory",
             json.dumps(selected) if selected else None),
        )
        pid = int(cur.lastrowid)
        if status in {ProposalStatus.OFFER_SENT, ProposalStatus.PENDING_INVITE}:
            record_fact(conn, pid, "offer_sent_at")
        conn.commit()
    return pid


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_lexi_connection() as conn:
        conn.execute(
            "DELETE FROM approvals WHERE proposal_id IN"
            " (SELECT id FROM proposals WHERE thread_id = ?)", (THREAD,)
        )
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.execute("DELETE FROM teams_list_snapshots")
        conn.commit()


def test_send_invite_dispatches_the_invite(monkeypatch):
    """The counterpart picked a time; this is the command that books it."""
    slot = _slot()
    pid = _make(ProposalStatus.PENDING_INVITE, selected=slot)

    from app.agents.comms_agent import ExecutionResult

    with patch("app.agents.comms_agent.execute_lexi_invite") as invite:
        invite.return_value = ExecutionResult(
            ok=True, proposal_id=pid, status=ProposalStatus.EXECUTED,
            decision="approved", errors=[], warnings=[],
        )
        reply = message_of(teams(f"send invite #{pid}"))

    assert invite.called, (
        "`send invite #N` is what Lexi tells Kory to type when the counterpart "
        "has picked a time; it must dispatch the invite, not fall through"
    )
    assert "not a lexi command" not in reply.lower(), reply


def test_retry_scheduling_reruns_the_search_with_his_guidance():
    pid = _make(ProposalStatus.NEEDS_KORY)

    with patch("app.agents.inbound_reply.retry_scheduling_with_guidance") as retry:
        retry.return_value = {"ok": True, "message": "New times drafted."}
        reply = message_of(teams(f"retry scheduling for #{pid} — offer Monday 10:30"))

    assert retry.called, "`retry scheduling for #N — <times>` must reach the retry"
    args, kwargs = retry.call_args
    assert args[0] == pid
    assert "Monday 10:30" in args[1], f"his guidance was dropped: {args}"
    assert "not a lexi command" not in reply.lower(), reply


def test_retry_scheduling_without_guidance_still_reaches_the_retry():
    pid = _make(ProposalStatus.NEEDS_KORY)
    with patch("app.agents.inbound_reply.retry_scheduling_with_guidance") as retry:
        retry.return_value = {"ok": False, "error": "guidance cannot be empty."}
        message_of(teams(f"retry scheduling #{pid}"))
    assert retry.called


def test_a_failed_retry_explains_itself_rather_than_going_silent():
    pid = _make(ProposalStatus.NEEDS_KORY)
    with patch("app.agents.inbound_reply.retry_scheduling_with_guidance") as retry:
        retry.return_value = {"ok": False, "error": "No valid slots in that window."}
        reply = message_of(teams(f"retry scheduling #{pid} — next week"))
    assert "No valid slots" in reply, reply
