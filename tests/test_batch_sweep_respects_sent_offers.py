"""The batch sweep must obey the same rule as the single-proposal path.

There are two ways into the scheduling engine: process_proposal_schedule for one
proposal, and process_pending_schedules — the sweep the orchestrator runs every
cycle to recover anything stranded in pending_triage. Both call _advance_proposal.

The guard against re-staging a proposal whose offer is already out was put on
the FIRST of those, which is the same mistake that produced the original bug:
the batch path was "safe" only because it selected on status, and the entire
point of the world-fact layer is that status can lie. A proposal rolled back to
pending_triage behind a sent offer — which _reschedule_unsent_offer can do —
was picked up by the sweep and had its sent draft rewritten.

The guard now lives in _advance_proposal, which both callers share, so a third
one is safe without knowing any of this.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.scheduler_agent import process_pending_schedules, process_proposal_schedule
from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}
THREAD = "batch-sweep-thread"
SENT_DRAFT = "ALREADY SENT — do not rewrite"


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=14)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 9, 30, tzinfo=MT).isoformat(),
    }


@pytest.fixture
def stranded_after_a_sent_offer():
    """pending_triage, but the offer really did go out."""
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] batch sweep", "Dana <dana@example.com>",
             "dana@example.com", "Can we meet next week?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, drafted_reply) VALUES (?,?,?,?,?)",
            (THREAD, ProposalStatus.PENDING_TRIAGE, "referral_or_intro",
             json.dumps([_slot()]), SENT_DRAFT),
        )
        pid = int(cur.lastrowid)
        record_fact(conn, pid, "offer_sent_at")
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _row(pid: int):
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT status, drafted_reply FROM proposals WHERE id = ?", (pid,)
        ).fetchone()


def test_the_sweep_does_not_rewrite_a_draft_that_was_already_sent(
    stranded_after_a_sent_offer,
):
    pid = stranded_after_a_sent_offer
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        processed = process_pending_schedules()

    row = _row(pid)
    assert pid not in processed, "the batch sweep re-staged a proposal whose offer is out"
    assert row["drafted_reply"] == SENT_DRAFT, "the sent draft was rewritten"
    assert row["status"] == ProposalStatus.PENDING_TRIAGE


def test_the_sweep_records_why_it_refused(stranded_after_a_sent_offer):
    """A silent no-op here looks identical to the sweep never running."""
    pid = stranded_after_a_sent_offer
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        process_pending_schedules()
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT message FROM audit_log WHERE reference_id = ?"
            " ORDER BY id DESC LIMIT 1", (str(pid),)
        ).fetchone()
    assert row and "already in the recipient's inbox" in row["message"], row


def test_the_sweep_still_advances_a_genuinely_unsent_proposal():
    """The guard must not freeze the recovery sweep, which is the whole reason
    it exists — a proposal stranded in pending_triage after a crash."""
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] batch sweep", "Dana <dana@example.com>",
             "dana@example.com", "Can we meet next week?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification)"
            " VALUES (?,?,?)",
            (THREAD, ProposalStatus.PENDING_TRIAGE, "referral_or_intro"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
    try:
        with patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value=FREE,
        ):
            processed = process_pending_schedules()
        assert pid in processed, "the recovery sweep stopped recovering"
        assert _row(pid)["status"] == ProposalStatus.PENDING_APPROVAL
    finally:
        with get_lexi_connection() as conn:
            conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
            conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
            conn.commit()


def test_a_deliberate_new_round_still_gets_through(stranded_after_a_sent_offer):
    """reoffer=True is how the decline and reschedule paths ask for new times."""
    pid = stranded_after_a_sent_offer
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        advanced = process_proposal_schedule(pid, reoffer=True)
    assert advanced is True, "a deliberate re-offer was refused"
    assert _row(pid)["drafted_reply"] != SENT_DRAFT, "new times were not staged"
