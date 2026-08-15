"""update_proposal_draft is now a validating write (the 9187 fix): edited
times are checked against the live calendar and synced into proposed_slots."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import app.agents.inbound_reply as ir

_DRAFT = (
    "Hi Heidi,\n\nHere are a few times:\n\n"
    "• Tuesday, August 18 at 1:00–1:30 PM MT\n"
    "• Tuesday, August 25 at 9:00–9:30 AM MT\n\n"
    "Let me know!\n"
)

_ALEJANDRA = {
    "subject": "Coffee: Alejandra Harvey <> Kory Mitchell | 9 am (copy)",
    "start": {"dateTime": "2026-08-25T09:00:00-06:00"},
    "end": {"dateTime": "2026-08-25T10:00:00-06:00"},
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY, thread_id TEXT, status TEXT,
            intent_classification TEXT, priority_tier TEXT, justification TEXT,
            voice_mode TEXT, send_channel TEXT, is_delegation INTEGER,
            drafted_reply TEXT, scheduling_note TEXT,
            kory_scheduling_guidance TEXT, proposed_slots TEXT, updated_at TEXT);
        CREATE TABLE email_threads (thread_id TEXT, subject TEXT, sender TEXT, raw_body TEXT);
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, step_name TEXT,
            reference_id TEXT, log_level TEXT, message TEXT, payload TEXT,
            timestamp TEXT DEFAULT (datetime('now')));
        INSERT INTO email_threads VALUES
            ('t1', 'Check in', 'heidi.heckler@iconicfounders.com', 'outbound context');
        """
    )
    conn.execute(
        "INSERT INTO proposals (id, thread_id, status, intent_classification, "
        "drafted_reply, proposed_slots) VALUES (9187, 't1', 'pending_approval', "
        "'internal_sync', 'old draft', ?)",
        (json.dumps([{"start": "2026-08-24T10:00:00-06:00", "end": "2026-08-24T10:30:00-06:00"}]),),
    )
    conn.commit()
    return conn


def _patched(conn, busy):
    return (
        patch.object(ir, "get_lexi_connection", return_value=conn),
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value={"status": "available", "busy_events": busy},
        ),
    )


def test_edit_with_booked_time_is_refused_and_nothing_changes():
    conn = _db()
    p1, p2 = _patched(conn, [_ALEJANDRA])
    with p1, p2:
        out = ir.update_proposal_draft(9187, _DRAFT)
    assert out["ok"] is False
    assert any("Alejandra Harvey" in c for c in out["conflicts"])
    row = conn.execute("SELECT drafted_reply, proposed_slots FROM proposals WHERE id=9187").fetchone()
    assert row["drafted_reply"] == "old draft"
    assert "2026-08-24" in row["proposed_slots"]


def test_edit_with_free_times_updates_draft_and_syncs_slots():
    conn = _db()
    p1, p2 = _patched(conn, [])
    with p1, p2:
        out = ir.update_proposal_draft(9187, _DRAFT)
    assert out["ok"] is True, out
    assert len(out["staged_slots"]) == 2
    assert "no holds exist yet" in out["note"].lower() or "No holds exist yet" in out["note"]
    row = conn.execute("SELECT drafted_reply, proposed_slots FROM proposals WHERE id=9187").fetchone()
    assert "August 18" in row["drafted_reply"]
    slots = json.loads(row["proposed_slots"])
    assert len(slots) == 2
    # Slots now match the draft — holds will land on the offered times.
    audit = conn.execute("SELECT step_name FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert audit["step_name"] == "draft_slots_synced"


def test_textual_edit_without_times_keeps_slots():
    conn = _db()
    p1, p2 = _patched(conn, [])
    with p1, p2:
        out = ir.update_proposal_draft(9187, "Hi Heidi,\n\nP.S. congrats on the award!\n")
    assert out["ok"] is True
    row = conn.execute("SELECT proposed_slots FROM proposals WHERE id=9187").fetchone()
    assert "2026-08-24" in row["proposed_slots"]
