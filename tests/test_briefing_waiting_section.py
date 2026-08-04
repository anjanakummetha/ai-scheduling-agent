"""Decision 2026-08-04: the 24h Teams nudge is replaced by a Waiting-on-you
section in the daily briefing email — Teams is for decisions, the brief is
for reminders."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.jobs import kory_briefings as kb


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE email_threads (thread_id TEXT PRIMARY KEY, subject TEXT, sender TEXT)")
    conn.execute(
        "CREATE TABLE proposals (id INTEGER PRIMARY KEY, thread_id TEXT, status TEXT, created_at TEXT)"
    )
    for pid, thread, subject, sender, status, age_hours in rows:
        created = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT OR IGNORE INTO email_threads VALUES (?,?,?)", (thread, subject, sender))
        conn.execute("INSERT INTO proposals VALUES (?,?,?,?)", (pid, thread, status, created))
    conn.commit()
    return conn


def test_aged_items_render_with_ids_and_labels():
    conn = _db([
        (11, "t1", "Coffee with <Anjana>", "Anjana Kummetha", "pending_approval", 30),
        (12, "t2", "Lunch ask", "Someone Else", "needs_kory", 48),
    ])
    with patch.object(kb, "get_lexi_connection", return_value=conn):
        html = kb.build_waiting_on_you_html()
    assert "#11" in html and "#12" in html
    assert "awaiting your approval" in html
    assert "needs your guidance" in html
    assert "&lt;Anjana&gt;" in html  # escaped
    assert "approve #N" in html


def test_fresh_items_excluded_and_empty_returns_blank():
    conn = _db([(21, "t1", "New ask", "A", "pending_approval", 2)])
    with patch.object(kb, "get_lexi_connection", return_value=conn):
        assert kb.build_waiting_on_you_html() == ""


def test_teams_nudge_disabled_by_default():
    assert kb._TEAMS_NUDGE_ENABLED is False
    assert kb.process_kory_24h_reminders() == []
