"""A staging pass that loses a race to a send must discard its work.

Live proposal 10562 (box E2E, 2026-08-22): the recovery sweep picked up a
proposal in pending_triage and began an engine pass — calendar reads, drafting,
the gate — which takes seconds. During that window the single-proposal path
retried, Kory approved, and the offer email was SENT. The sweep's pass then
finished and wrote its result: ``offer_sent -> pending_approval``, re-staging a
draft whose offer was already in the recipient's inbox. The counterpart's
acceptance that followed could not register against pending_approval, so the
pick was dropped (fail-safe, but the offer round was lost).

The entry guard in _advance_proposal cannot see a send that commits mid-pass —
it ran before the race started. The fix is at the FINISH: the completion write
claims ``expect=pending_triage``, the state every engine entry normalizes to,
so a pass whose proposal moved underneath it loses atomically instead of
clobbering the winner.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.scheduler_agent import process_pending_schedules
from app.scheduling.proposal_state import ProposalStatus, record_fact, transition
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}
THREAD = "staging-race-thread"
SENT_DRAFT = "THE DRAFT THAT WAS SENT — a stale engine pass must not rewrite it"


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=14)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 9, 30, tzinfo=MT).isoformat(),
    }


@pytest.fixture
def racing_proposal():
    """pending_triage and genuinely unsent — the race happens mid-pass."""
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] staging race", "Dana <dana@example.com>",
             "dana@example.com", "Can we meet next week?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification)"
            " VALUES (?,?,?)",
            (THREAD, ProposalStatus.PENDING_TRIAGE, "referral_or_intro"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _send_wins_the_race(pid: int):
    """What the winning retry+approve+send path does, from its own connection.

    Walks the same legal transitions as the live winner (the guard trigger
    rejects a raw status write, which is exactly what it is for): a retry
    stages the draft, Kory approves, the email goes out, the fact is stamped.
    """
    with get_lexi_connection() as conn:
        transition(
            conn, pid, to=ProposalStatus.PENDING_APPROVAL,
            expect=ProposalStatus.PENDING_TRIAGE,
            reason="race test: the winning engine pass staged its draft",
            actor="scheduler",
            fields={"proposed_slots": json.dumps([_slot()]), "drafted_reply": SENT_DRAFT},
        )
        transition(
            conn, pid, to=ProposalStatus.OFFER_SENT,
            expect=ProposalStatus.PENDING_APPROVAL,
            reason="race test: Kory approved; offer email dispatched",
            actor="kory",
        )
        record_fact(conn, pid, "offer_sent_at")
        conn.commit()


def _row(pid: int):
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT status, drafted_reply, offer_sent_at FROM proposals WHERE id = ?",
            (pid,),
        ).fetchone()


def test_a_send_mid_pass_beats_the_stale_staging_write(racing_proposal):
    pid = racing_proposal

    def calendar_then_the_send_lands(*args, **kwargs):
        # The engine pass is already past its entry guard; the approve+send
        # commits while it is off reading the calendar.
        _send_wins_the_race(pid)
        return FREE

    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        side_effect=calendar_then_the_send_lands,
    ):
        processed = process_pending_schedules()

    row = _row(pid)
    assert pid not in processed, "a stale engine pass claimed success over a sent offer"
    assert row["status"] == ProposalStatus.OFFER_SENT, (
        f"a sent offer was re-staged to {row['status']!r} — the live 10562 bug"
    )
    assert row["drafted_reply"] == SENT_DRAFT, "the sent draft was rewritten"
    assert row["offer_sent_at"], "the world fact was lost"


def test_the_lost_race_is_recorded_and_not_treated_as_a_failure(racing_proposal):
    """Losing the race is normal operation: an INFO discard, no ERROR row that
    would page anyone, and no urgent-failure escalation to Kory."""
    pid = racing_proposal

    def calendar_then_the_send_lands(*args, **kwargs):
        _send_wins_the_race(pid)
        return FREE

    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        side_effect=calendar_then_the_send_lands,
    ):
        process_pending_schedules()

    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT log_level, message FROM audit_log WHERE reference_id = ?"
            " ORDER BY id", (str(pid),)
        ).fetchall()
    messages = [r["message"] for r in rows]
    assert any("Staging discarded" in m for m in messages), messages
    assert not any(r["log_level"] == "ERROR" for r in rows), (
        "a lost race was recorded as a scheduler failure: "
        f"{[(r['log_level'], r['message'][:80]) for r in rows]}"
    )
