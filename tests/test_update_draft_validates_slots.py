"""update_proposal_draft is now a validating write (the 9187 fix): edited
times are checked against the live calendar and synced into proposed_slots."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app.agents.inbound_reply as ir

_MT = ZoneInfo("America/Denver")

# Generated from today, not pinned. The pinned "Tuesday, August 18" stopped
# being a Tuesday — and stopped being this year — once the calendar passed it,
# which silently inverted the refusal these tests exist to prove.


def _weekday_two_weeks_out(weekday: int) -> date:
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=14)
    return monday + timedelta(days=weekday)


def _mt(d: date, hour: int, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_MT).isoformat()


_D1 = _weekday_two_weeks_out(1)   # Tuesday
_D2 = _weekday_two_weeks_out(8)   # the following Tuesday
_STAGED_DAY = _weekday_two_weeks_out(0)  # Monday — what is already staged

_DRAFT = (
    "Hi Heidi,\n\nHere are a few times:\n\n"
    f"• {_D1:%A}, {_D1:%B} {_D1.day} at 1:00–1:30 PM MT\n"
    f"• {_D2:%A}, {_D2:%B} {_D2.day} at 9:00–9:30 AM MT\n\n"
    "Let me know!\n"
)

_ALEJANDRA = {
    "subject": "Coffee: Alejandra Harvey <> Kory Mitchell | 9 am (copy)",
    "start": {"dateTime": _mt(_D2, 9)},
    "end": {"dateTime": _mt(_D2, 10)},
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
        (json.dumps([{"start": _mt(_STAGED_DAY, 10), "end": _mt(_STAGED_DAY, 10, 30)}]),),
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
    assert _STAGED_DAY.isoformat() in row["proposed_slots"]


def test_edit_with_free_times_updates_draft_and_syncs_slots():
    conn = _db()
    p1, p2 = _patched(conn, [])
    with p1, p2:
        out = ir.update_proposal_draft(9187, _DRAFT)
    assert out["ok"] is True, out
    assert len(out["staged_slots"]) == 2
    assert "no holds exist yet" in out["note"].lower() or "No holds exist yet" in out["note"]
    row = conn.execute("SELECT drafted_reply, proposed_slots FROM proposals WHERE id=9187").fetchone()
    assert f"{_D1:%B} {_D1.day}" in row["drafted_reply"]
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
    assert _STAGED_DAY.isoformat() in row["proposed_slots"]
