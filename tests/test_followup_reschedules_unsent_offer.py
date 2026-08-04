"""Live C-5: a sender follow-up on an UNSENT offer only pinged Kory, leaving a
stale draft one approve-tap from sending an answer that ignored their reply."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from app.agents import lexi_thread_followup as ltf


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE email_threads (thread_id TEXT PRIMARY KEY, raw_body TEXT)"
    )
    conn.execute(
        "CREATE TABLE proposals (id INTEGER PRIMARY KEY, thread_id TEXT, status TEXT, "
        "drafted_reply TEXT, proposed_slots TEXT, teams_approval_notified_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO email_threads VALUES ('t1', 'Tuesday afternoon or Wednesday next week?')"
    )
    conn.execute(
        "INSERT INTO proposals (id, thread_id, status, drafted_reply, proposed_slots) "
        "VALUES (7, 't1', 'pending_approval', 'old draft', '[]')"
    )
    conn.commit()
    return conn


def test_followup_merges_body_and_reschedules(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(ltf, "get_lexi_connection", lambda: conn, raising=False)
    with (
        patch("app.storage.lexi_db.get_lexi_connection", return_value=conn),
        patch("app.agents.scheduler_agent.process_proposal_schedule", return_value=True) as sched,
        patch("app.bot.teams_publisher.schedule_teams_approval_push") as push,
    ):
        out = ltf._reschedule_unsent_offer(
            {"proposal_id": 7}, followup_body="Actually — how about sometime Thursday instead?"
        )
    assert out is not None and out["rescheduled"] is True
    sched.assert_called_once_with(7)
    push.assert_called_once()
    row = conn.execute("SELECT raw_body FROM email_threads WHERE thread_id='t1'").fetchone()
    assert "[Sender follow-up]: Actually — how about sometime Thursday instead?" in row["raw_body"]
    prop = conn.execute("SELECT status, drafted_reply FROM proposals WHERE id=7").fetchone()
    # process_proposal_schedule is mocked, so status stays as reset by the helper.
    assert prop["status"] == "pending_triage"
    assert prop["drafted_reply"] is None


def test_failed_reschedule_falls_back_to_notify(monkeypatch):
    conn = _mem_db()
    with (
        patch("app.storage.lexi_db.get_lexi_connection", return_value=conn),
        patch("app.agents.scheduler_agent.process_proposal_schedule", return_value=False),
    ):
        out = ltf._reschedule_unsent_offer({"proposal_id": 7}, followup_body="Thursday?")
    assert out is None  # caller falls through to the Kory ping


def test_empty_followup_is_ignored():
    assert ltf._reschedule_unsent_offer({"proposal_id": 7}, followup_body="  ") is None
