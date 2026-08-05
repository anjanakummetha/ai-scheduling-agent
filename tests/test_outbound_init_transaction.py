"""Live O-2b defect chain: lexi_start_scheduling timed out at the gateway.

The calendar fetch (40-80s+ cold) ran INSIDE an open SQLite write transaction,
starving every other writer for its duration, and the 5-minute context-cache
TTL meant nearly every real command paid that cold fetch. The slot search now
runs before the transaction opens, the TTL is 30 minutes, and Lexi's own
calendar writes invalidate the cache.
"""

from __future__ import annotations

import sqlite3


def _fresh_db(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE email_threads (thread_id TEXT PRIMARY KEY, subject TEXT, "
        "sender TEXT, received_at TEXT, raw_body TEXT)"
    )
    conn.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "step_name TEXT, reference_id TEXT, log_level TEXT, message TEXT, "
        "payload TEXT, timestamp TEXT DEFAULT (datetime('now')))"
    )
    conn.commit()
    conn.close()
    return db


def test_failed_slot_search_leaves_no_thread_row(tmp_path, monkeypatch):
    """The slot search runs before the transaction — a calendar failure must
    audit the error without ever inserting the outbound thread."""
    from app.agents import outbound_agent as oa

    db = _fresh_db(tmp_path)

    def _conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(oa, "get_lexi_connection", _conn)

    def _boom(**kwargs):
        raise RuntimeError("composio down")

    monkeypatch.setattr(oa, "_load_calendar_context", _boom)

    result = oa.initiate_outbound_scheduling(
        recipient_email="anjanakummetha@gmail.com",
        subject="Availability to Connect",
        meeting_intent="meeting",
        duration_minutes=60,
        authorized_by="kory",
        require_ceo_signoff=True,
    )
    assert result["ok"] is False
    assert any("composio down" in e for e in result["errors"])

    with _conn() as check:
        threads = check.execute("SELECT COUNT(*) c FROM email_threads").fetchone()["c"]
        audits = check.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE step_name='outbound_delegation_init' "
            "AND log_level='ERROR'"
        ).fetchone()["c"]
    assert threads == 0
    assert audits == 1


def test_calendar_write_invalidates_context_cache():
    from app.integrations.outlook_calendar import _invalidate_scheduling_cache
    from app.scheduling import calendar_context as cc

    cc._context_cache["45:2026080501"] = (0.0, {"status": "available"})
    _invalidate_scheduling_cache()
    assert cc._context_cache == {}


def test_context_cache_ttl_is_thirty_minutes():
    from app.scheduling import calendar_context as cc

    assert cc._CONTEXT_CACHE_TTL_SEC == 1800.0
