"""Tier-2 pre-handover audit fixes (2026-08-15): window shift (B3), all-day
blocking (B4), send successful:false (B8), stuck sweeper (C1), atomic approval
claim (D3), cross-proposal reservation (B7), WAL (D1), forward removed (A4)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# --- B3: fetch_events_chunked keeps the window aware (no +6h shift) ---------


def test_fetch_events_chunked_does_not_shift_window():
    import app.integrations.named_calendars as nc

    captured: list[tuple[str, str]] = []

    def _capture(resolved, start_iso, end_iso, role="read"):
        captured.append((start_iso, end_iso))
        return [], None

    with (
        patch.object(nc, "conflict_calendar_names", return_value=["Kory Master Calendar (ALL)"]),
        patch.object(nc, "resolve_calendar_name", return_value={"id": "c", "name": "Kory Master Calendar (ALL)"}),
        patch.object(nc, "get_calendar_events_for_resolved", side_effect=_capture),
    ):
        nc.fetch_events_chunked("2026-08-15T13:57:00+00:00", "2026-08-16T13:57:00+00:00")

    # The start passed downstream must carry a UTC offset (aware), not a naive
    # value that _convert_iso_timezone would misread as Denver.
    start_iso = captured[0][0]
    parsed = datetime.fromisoformat(start_iso)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 13  # unchanged, not shifted to 19


# --- B4: all-day unavailability blocks; location markers don't --------------


@pytest.mark.parametrize(
    "event,blocks",
    [
        ({"isAllDay": True, "showAs": "oof", "subject": "Kory OOO"}, True),
        ({"isAllDay": True, "showAs": "busy", "subject": "PTO — vacation"}, True),
        ({"isAllDay": True, "showAs": "busy", "subject": "Board Meeting — Canopy"}, True),
        ({"isAllDay": True, "showAs": "busy", "subject": "Flight to Des Moines"}, True),
        ({"isAllDay": True, "showAs": "busy", "subject": "Kory in Chicago"}, False),
        ({"isAllDay": True, "showAs": "busy", "subject": "Aspen"}, False),
        ({"isAllDay": True, "showAs": "free", "subject": "PTO"}, False),  # free never blocks
    ],
)
def test_all_day_blocking(event, blocks):
    from app.integrations.outlook_calendar import is_blocking_event

    assert is_blocking_event(event) is blocks


def test_timed_meeting_still_blocks():
    from app.integrations.outlook_calendar import is_blocking_event

    ev = {
        "showAs": "busy",
        "subject": "Kory <> Investor",
        "start": {"dateTime": "2026-09-02T10:00:00", "timeZone": "America/Denver"},
        "end": {"dateTime": "2026-09-02T10:30:00", "timeZone": "America/Denver"},
    }
    assert is_blocking_event(ev) is True


# --- B8: send paths surface successful:false --------------------------------


def test_send_draft_raises_on_successful_false():
    import app.integrations.outlook_email as oe

    with (
        patch.object(oe, "settings") as ms,
        patch("app.integrations.composio_client.execute_tool", return_value={"successful": False, "log_id": None}),
    ):
        ms.lexi_dry_run = False
        ms.lexi_write_mode = "kory"
        ms.sandbox_email_loopback = False
        with pytest.raises(RuntimeError):
            oe.send_draft("draft-123", send_channel="kory")


# --- D3: atomic approval claim (unit-level rowcount semantics) --------------


def test_atomic_claim_matches_only_expected_status():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE proposals (id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO proposals VALUES (1, 'pending_approval', '2026-08-01')")
    conn.commit()
    # First claim on the expected status succeeds.
    n1 = conn.execute(
        "UPDATE proposals SET updated_at=datetime('now') WHERE id=? AND status=?",
        (1, "pending_approval"),
    ).rowcount
    assert n1 == 1
    conn.execute("UPDATE proposals SET status='offer_sent' WHERE id=1")
    # A second claimant reading the stale 'pending_approval' matches nothing.
    n2 = conn.execute(
        "UPDATE proposals SET updated_at=datetime('now') WHERE id=? AND status=?",
        (1, "pending_approval"),
    ).rowcount
    assert n2 == 0


# --- B7: cross-proposal reservation -----------------------------------------


def test_slot_reserved_by_other_detects_overlap():
    import app.agents.comms_agent as ca

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE proposals (id INTEGER PRIMARY KEY, status TEXT, proposed_slots TEXT);
        CREATE TABLE holds (proposal_id INTEGER, slot_start TEXT, slot_end TEXT, expires_at TEXT);
        INSERT INTO proposals VALUES
          (100, 'offer_sent', '[{"start":"2026-09-02T09:00:00-06:00","end":"2026-09-02T09:30:00-06:00"}]');
        """
    )
    conn.commit()
    my_slots = [{"start": "2026-09-02T09:00:00-06:00", "end": "2026-09-02T09:30:00-06:00"}]
    with patch.object(ca, "get_lexi_connection", return_value=conn):
        err = ca._slot_reserved_by_other(200, my_slots)
    assert err is not None and "already offered" in err


def test_slot_reserved_by_other_ignores_self_and_nonoverlap():
    import app.agents.comms_agent as ca

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE proposals (id INTEGER PRIMARY KEY, status TEXT, proposed_slots TEXT);
        CREATE TABLE holds (proposal_id INTEGER, slot_start TEXT, slot_end TEXT, expires_at TEXT);
        INSERT INTO proposals VALUES
          (200, 'offer_sent', '[{"start":"2026-09-02T09:00:00-06:00","end":"2026-09-02T09:30:00-06:00"}]'),
          (300, 'offer_sent', '[{"start":"2026-09-03T14:00:00-06:00","end":"2026-09-03T14:30:00-06:00"}]');
        """
    )
    conn.commit()
    my_slots = [{"start": "2026-09-02T09:00:00-06:00", "end": "2026-09-02T09:30:00-06:00"}]
    with patch.object(ca, "get_lexi_connection", return_value=conn):
        err = ca._slot_reserved_by_other(200, my_slots)  # 200 is self
    assert err is None


# --- C1: stuck sweeper ------------------------------------------------------


def test_stuck_sweep_nudges_aged_and_dedupes(monkeypatch):
    import app.jobs.stuck_proposals as sp

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE proposals (id INTEGER PRIMARY KEY, thread_id TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE email_threads (thread_id TEXT, subject TEXT, sender TEXT);
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, step_name TEXT,
            reference_id TEXT, log_level TEXT, message TEXT, payload TEXT,
            timestamp TEXT DEFAULT (datetime('now')));
        INSERT INTO email_threads VALUES ('t1','Intro','a@x.com');
        INSERT INTO proposals VALUES (1,'t1','pending_invite', datetime('now','-5 days'));
        INSERT INTO proposals VALUES (2,'t1','pending_approval', datetime('now','-1 hour'));
        """
    )
    conn.commit()
    monkeypatch.setenv("LEXI_STUCK_PROPOSAL_SWEEP", "true")
    with (
        patch.object(sp, "get_lexi_connection", return_value=conn),
        patch("app.safety.outbound_guard.teams_push_allowed", return_value=True),
        patch.object(sp, "_notify_stuck") as notify,
    ):
        first = sp.sweep_stuck_proposals()
        second = sp.sweep_stuck_proposals()  # deduped
    assert [n["proposal_id"] for n in first] == [1]  # aged one only, not the 1h-old
    assert notify.call_count == 1
    assert second == []  # already nudged within the renudge window


# --- A4: forward primitive removed ------------------------------------------


def test_forward_message_not_in_scheduling_allowlist():
    from app.integrations.outlook_actions import SCHEDULING_ALLOW_SLUGS

    assert "OUTLOOK_FORWARD_MESSAGE" not in SCHEDULING_ALLOW_SLUGS


# --- D1: WAL enabled --------------------------------------------------------


def test_connection_is_wal():
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
