"""Kory's retry on a declined offer must actually search — not narrate one.

Live proposal 10563 (2026-08-22): the counterpart declined all offered times
("anything the following week?"), the proposal parked in pending_reoffer, and
Kory answered the guidance prompt. retry_scheduling_with_guidance called the
engine WITHOUT reoffer=True, so the world-fact guard refused — the offer email
was already in the recipient's inbox, and a retry on such a thread is by
definition a new round. No search ran. The escalation then told Kory
"I'm not finding open morning coffee slots … your mornings that week are
mostly booked" — a search result that was fabricated, and false on the real
calendar.

Two pins: (1) the retry passes reoffer=True when the offer fact is set, so
the engine genuinely runs; (2) when the engine refuses without recording a
search failure, the escalation says it has NOT searched — it never claims
"I couldn't find times".
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.inbound_reply import retry_scheduling_with_guidance
from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}
THREAD = "retry-after-decline-thread"


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=10)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 10, 0, tzinfo=MT).isoformat(),
    }


@pytest.fixture
def declined_offer():
    """pending_reoffer with the offer fact set — the state Kory's retry meets."""
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] Coffee next week", "Dana <dana@example.com>",
             "dana@example.com", "Would Tuesday or Thursday work? I'm on Eastern."),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots) VALUES (?,?,?,?)",
            (THREAD, ProposalStatus.PENDING_REOFFER, "referral_or_intro",
             json.dumps([_slot()])),
        )
        pid = int(cur.lastrowid)
        record_fact(conn, pid, "offer_sent_at")
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def test_retry_on_a_declined_offer_actually_searches(declined_offer):
    pid = declined_offer
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=FREE,
    ), patch("app.bot.teams_publisher.schedule_teams_approval_push"):
        result = retry_scheduling_with_guidance(pid, "whatever works next week")

    assert result.get("ok") is True, result
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, proposed_slots FROM proposals WHERE id = ?", (pid,)
        ).fetchone()
    assert row["status"] == ProposalStatus.PENDING_APPROVAL, (
        "the engine never ran — the guard refusal path (live 10563)"
    )
    assert row["proposed_slots"], "no slots were staged by the new round"

    with get_lexi_connection() as conn:
        msgs = [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM audit_log WHERE reference_id = ? ORDER BY id",
                (str(pid),),
            ).fetchall()
        ]
    assert any("New round opened" in m for m in msgs), msgs
    assert not any("Refused to re-stage" in m for m in msgs), (
        "the retry still hits the world-fact guard instead of opening a round"
    )


def test_a_refusal_is_never_narrated_as_a_failed_search(declined_offer):
    """If the engine refuses without a recorded search failure, the message to
    Kory must say no search happened — not 'I couldn't find times'."""
    pid = declined_offer
    with patch(
        "app.agents.inbound_reply.process_proposal_schedule", return_value=False
    ), patch("app.agents.inbound_reply._latest_scheduler_failure", return_value=""), \
         patch("app.bot.teams_publisher.schedule_teams_approval_push"):
        result = retry_scheduling_with_guidance(pid, "mornings if possible")

    text = " ".join(
        str(result.get(k) or "") for k in ("kory_message", "message", "reason", "error")
    ).lower()
    assert "not actually searched" in text or "have not" in text, result
    assert "couldn't find" not in text, (
        f"a refusal was narrated as a search result: {result}"
    )
