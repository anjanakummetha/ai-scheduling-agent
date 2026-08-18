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

from app.agents.scheduler_agent import process_proposal_schedule
from app.scheduling.proposal_state import (
    ALL_STATUSES,
    SCHEDULABLE,
    ProposalStatus,
    record_fact,
)
from app.storage.lexi_db import get_lexi_connection

# Every status the engine must refuse to stage a first offer from, derived
# from the state machine rather than re-listed here — a new status is
# classified in one place and this test picks it up automatically.
NOT_SCHEDULABLE = sorted(ALL_STATUSES - SCHEDULABLE)

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


@pytest.mark.parametrize("status", NOT_SCHEDULABLE)
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


@pytest.mark.parametrize("status", sorted(SCHEDULABLE))
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


def test_a_sent_offer_is_refused_even_if_the_status_was_rolled_back():
    """The guard that matters, and the one the old code did not have.

    Suppose something puts a proposal back to pending_triage after its offer
    email has already gone out — a redelivered webhook, a follow-up handler, a
    maintenance script, a bug nobody has written yet. Status alone says "safe to
    stage"; the world says an email is sitting in somebody's inbox and holds are
    on the calendar.

    Lexi must believe the world. Re-staging here is what emailed the same person
    twice.
    """
    pid = _seed(ProposalStatus.PENDING_TRIAGE)
    with get_lexi_connection() as conn:
        assert record_fact(conn, pid, "offer_sent_at") is True
        conn.commit()

    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        advanced = process_proposal_schedule(pid)

    row = _row(pid)
    assert advanced is False
    assert row["drafted_reply"] == "ALREADY SENT", "the sent offer's draft was rewritten"


def test_a_world_fact_cannot_be_un_recorded():
    """An email cannot be un-sent, so the column recording it is write-once.

    Enforced by a database trigger, not by convention, because the whole point
    of the fact layer is that the rest of the system may trust it absolutely.
    """
    import sqlite3

    pid = _seed(ProposalStatus.OFFER_SENT)
    with get_lexi_connection() as conn:
        record_fact(conn, pid, "offer_sent_at")
        conn.commit()

        # A second record_fact is a no-op rather than a refresh: a retry that
        # sends nothing must not make a two-day-old offer look like it just went.
        assert record_fact(conn, pid, "offer_sent_at") is False

        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE proposals SET offer_sent_at = NULL WHERE id = ?", (pid,))
