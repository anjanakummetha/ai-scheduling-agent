"""A proposal whose offer has already gone out must never be re-staged.

The root cause behind "she keeps breaking", found 2026-08-18.

process_proposal_schedule — the SINGLE-proposal entry point, the one a retry, a
redelivered webhook or a follow-up reaches — called _advance_proposal
unconditionally. _advance_proposal rewrites the draft and sets the status back
to pending_approval. So a proposal in offer_sent silently reverted to "unsent":
the email was already in the recipient's inbox and the holds were on Kory's
calendar, but `pending` showed it as a draft awaiting approval.

Approving it then sent the SAME person a second offer and placed a second set of
holds. Nothing anywhere said so.

The batch path was always safe (WHERE status = pending_triage). This entry point
had no equivalent guard.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.scheduler_agent import TERMINAL_OR_SENT_STATUSES, process_proposal_schedule
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "no-restage-thread"
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}


def _seed(status: str) -> int:
    day = date.today() + timedelta(days=14)
    slot = {
        "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 9, 30, tzinfo=MT).isoformat(),
    }
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, sender_email,"
            " raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] no restage", "x@example.com", "x@example.com",
             "Can we meet next week?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification, proposed_slots,"
            " drafted_reply) VALUES (?,?,?,?,?)",
            (THREAD, status, "referral_or_intro", json.dumps([slot]), "ALREADY SENT"),
        )
        pid = cur.lastrowid
        conn.commit()
    return pid


def _row(pid: int):
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT status, drafted_reply FROM proposals WHERE id = ?", (pid,)
        ).fetchone()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


@pytest.mark.parametrize("status", sorted(TERMINAL_OR_SENT_STATUSES))
def test_a_sent_or_finished_proposal_is_never_re_staged(status: str):
    pid = _seed(status)
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        advanced = process_proposal_schedule(pid)
    row = _row(pid)
    assert advanced is False, f"{status} was advanced"
    assert row["status"] == status, f"{status} became {row['status']}"
    assert row["drafted_reply"] == "ALREADY SENT", f"{status} had its draft rewritten"


def test_the_refusal_is_recorded_so_it_is_not_a_silent_no_op():
    pid = _seed("offer_sent")
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        process_proposal_schedule(pid)
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT message FROM audit_log WHERE reference_id = ? ORDER BY id DESC LIMIT 1",
            (str(pid),),
        ).fetchone()
    assert row and "Refused to re-stage" in row["message"], row


@pytest.mark.parametrize("status", ["pending_triage", "awaiting_reply_prompt", "pending_reoffer"])
def test_a_proposal_that_SHOULD_advance_still_does(status: str):
    """The guard must not freeze the normal path."""
    pid = _seed(status)
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        process_proposal_schedule(pid)
    assert _row(pid)["status"] != status or _row(pid)["drafted_reply"] != "ALREADY SENT", (
        f"{status} should still be advanceable"
    )
