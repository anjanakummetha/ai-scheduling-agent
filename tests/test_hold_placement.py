"""Tests for strict hold placement."""

from unittest.mock import patch

import pytest

from app.integrations.hold_placement import HoldPlacementError, place_offered_holds


def test_place_offered_holds_requires_all_slots():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE holds (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER,
            event_id TEXT,
            slot_start TEXT,
            slot_end TEXT,
            expires_at TEXT
        )
        """
    )
    slots = [
        {"start": "2026-06-17T16:00:00-04:00", "end": "2026-06-17T16:30:00-04:00"},
        {"start": "2026-06-18T17:00:00-04:00", "end": "2026-06-18T17:30:00-04:00"},
    ]
    with patch("app.integrations.hold_placement.settings") as mock_settings:
        mock_settings.lexi_dry_run = True
        count = place_offered_holds(
            conn,
            proposal_id=1,
            slots=slots,
            intent_classification="virtual_30",
            meeting_subject="Test meeting",
        )
    assert count == 2
    rows = conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0]
    assert rows == 2


def test_place_offered_holds_raises_on_conflict():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE holds (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER,
            event_id TEXT,
            slot_start TEXT,
            slot_end TEXT,
            expires_at TEXT
        )
        """
    )
    slots = [
        {"start": "2026-06-17T16:00:00-04:00", "end": "2026-06-17T16:30:00-04:00"},
        {"start": "2026-06-18T17:00:00-04:00", "end": "2026-06-18T17:30:00-04:00"},
    ]

    def fake_hold(*, action, calendar_name=None):
        if "option 2" in action.get("title", ""):
            return {"ok": False, "error": "conflict", "conflicting_events": ["busy"]}
        return {"ok": True, "event_id": "evt-1"}

    with patch("app.integrations.hold_placement.settings") as mock_settings:
        mock_settings.lexi_dry_run = False
        with patch("app.integrations.hold_placement.place_tentative_hold", side_effect=fake_hold):
            with pytest.raises(HoldPlacementError):
                place_offered_holds(
                    conn,
                    proposal_id=1,
                    slots=slots,
                    intent_classification="virtual_30",
                )


def _holds_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE holds (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER,
            event_id TEXT,
            slot_start TEXT,
            slot_end TEXT,
            expires_at TEXT
        )
        """
    )
    return conn


def test_partial_run_resumes_only_missing_slots():
    """LT-D1: hold step died after placing 2/3 — retry must place ONLY the missing one."""
    conn = _holds_conn()
    slots = [
        {"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"},
        {"start": "2026-08-10T15:30:00-06:00", "end": "2026-08-10T16:00:00-06:00"},
        {"start": "2026-08-17T08:30:00-06:00", "end": "2026-08-17T09:00:00-06:00"},
    ]
    for slot in slots[1:]:
        conn.execute(
            "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)"
            " VALUES (1, 'evt-existing', ?, ?, '2026-08-08T00:00:00+00:00')",
            (slot["start"], slot["end"]),
        )

    created = []

    def fake_hold(*, action, calendar_name=None):
        created.append(action["start"])
        return {"ok": True, "event_id": f"evt-new-{len(created)}"}

    with patch("app.integrations.hold_placement.settings") as mock_settings:
        mock_settings.lexi_dry_run = False
        with patch("app.integrations.hold_placement.place_tentative_hold", side_effect=fake_hold):
            count = place_offered_holds(
                conn, proposal_id=1, slots=slots, intent_classification="virtual_30"
            )
    assert count == 3
    assert created == ["2026-08-10T10:00:00-06:00"]
    assert conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 3


def test_adopts_own_orphaned_hold_event():
    """LT-D1: a crash after event creation orphans the calendar hold; the retry
    sees it as a 'conflict' and must adopt it, not fail forever."""
    from app.scheduling.invite_builder import build_hold_action

    conn = _holds_conn()
    slots = [{"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"}]
    orphan_title = build_hold_action(
        slot=slots[0],
        meeting_subject="[TEST] Intro chat next week? — LT-D1",
        intent="virtual_30",
        option_index=1,
        sender="Anjana <anjanakummetha@gmail.com>",
    )["title"]
    if not orphan_title.upper().startswith("HOLD:"):
        orphan_title = f"HOLD: {orphan_title}"

    def fake_hold(*, action, calendar_name=None):
        return {
            "ok": False,
            "error": "conflict",
            "conflicting_events": [
                {
                    "id": "evt-orphan-123",
                    "subject": orphan_title,
                    "start": {"dateTime": "2026-08-10T10:00:00", "timeZone": "America/Denver"},
                    "end": {"dateTime": "2026-08-10T10:30:00", "timeZone": "America/Denver"},
                }
            ],
        }

    with patch("app.integrations.hold_placement.settings") as mock_settings:
        mock_settings.lexi_dry_run = False
        with patch("app.integrations.hold_placement.place_tentative_hold", side_effect=fake_hold):
            count = place_offered_holds(
                conn,
                proposal_id=1,
                slots=slots,
                intent_classification="virtual_30",
                meeting_subject="[TEST] Intro chat next week? — LT-D1",
                sender="Anjana <anjanakummetha@gmail.com>",
            )
    assert count == 1
    row = conn.execute("SELECT event_id FROM holds").fetchone()
    assert row[0] == "evt-orphan-123"


def test_real_conflict_still_raises():
    """A genuine busy event (different subject) must still block the hold."""
    conn = _holds_conn()
    slots = [{"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"}]

    def fake_hold(*, action, calendar_name=None):
        return {
            "ok": False,
            "error": "conflict",
            "conflicting_events": [
                {
                    "id": "evt-real-mtg",
                    "subject": "WOB",
                    "start": {"dateTime": "2026-08-10T10:00:00", "timeZone": "America/Denver"},
                    "end": {"dateTime": "2026-08-10T10:30:00", "timeZone": "America/Denver"},
                }
            ],
        }

    with patch("app.integrations.hold_placement.settings") as mock_settings:
        mock_settings.lexi_dry_run = False
        with patch("app.integrations.hold_placement.place_tentative_hold", side_effect=fake_hold):
            with pytest.raises(HoldPlacementError):
                place_offered_holds(
                    conn, proposal_id=1, slots=slots, intent_classification="virtual_30"
                )
    assert conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0
