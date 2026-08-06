"""E-6: a conflict appearing between offer and confirm must not double-book.

The calendar is authoritative at the moment of booking, not the moment of
offering. Something can land on the slot while the counterpart is deciding, so
the slot is re-checked at confirm time.
"""

from __future__ import annotations

from unittest.mock import patch

from app.agents import comms_agent as ca

SLOT = {"start": "2026-08-12T10:00:00-06:00", "end": "2026-08-12T10:30:00-06:00"}


def _proposal(holds=None):
    return {"id": 1, "holds": holds or []}


def test_clear_slot_confirms():
    with patch("app.integrations.outlook_calendar.has_conflict", return_value=(False, [], None)):
        assert ca._confirm_time_conflict(_proposal(), SLOT) is None


def test_conflict_blocks_the_confirm():
    intruder = {
        "id": "evt-intruder",
        "subject": "Board call",
        "start": {"dateTime": "2026-08-12T10:15:00-06:00"},
    }
    with patch(
        "app.integrations.outlook_calendar.has_conflict",
        return_value=(True, [intruder], None),
    ):
        clash = ca._confirm_time_conflict(_proposal(), SLOT)

    assert clash is not None
    assert "no longer free" in clash["error"]
    # Kory is told plainly that nothing was booked, and asked what to do.
    assert "have **not** sent the invite" in clash["kory_message"]
    assert "Board call" in clash["kory_message"]
    assert clash["conflicting_events"] == [intruder]


def test_lexis_own_holds_are_not_counted_as_conflicts():
    """The holds exist to protect this very slot — counting them would make every
    confirmation look like it conflicts with itself."""
    captured: dict[str, object] = {}

    def fake_has_conflict(action, *, ignore_event_ids=None):
        captured["ignored"] = ignore_event_ids
        return False, [], None

    proposal = _proposal(
        holds=[
            {"event_id": "evt-hold-1"},
            {"event_id": "evt-hold-2"},
            {"event_id": ""},  # unplaced hold — nothing to ignore
        ]
    )
    with patch("app.integrations.outlook_calendar.has_conflict", side_effect=fake_has_conflict):
        assert ca._confirm_time_conflict(proposal, SLOT) is None

    assert captured["ignored"] == ["evt-hold-1", "evt-hold-2"]


def test_unreadable_calendar_fails_closed():
    """'I could not look' is not evidence the slot is free."""
    with patch(
        "app.integrations.outlook_calendar.has_conflict",
        side_effect=RuntimeError("Graph 503"),
    ):
        clash = ca._confirm_time_conflict(_proposal(), SLOT)

    assert clash is not None
    assert "Could not re-check the calendar" in clash["error"]
    assert "haven't sent the invite" in clash["kory_message"]


def test_conflict_leaves_the_holds_in_place():
    """The end-to-end property: on a clash, no invite is created and — critically —
    no holds are released. The holds are the only thing still protecting the slot
    while Kory decides."""
    from app.storage.lexi_db import get_lexi_connection
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db()
    with get_lexi_connection() as conn:
        # Child before parent: INSERT OR REPLACE deletes and reinserts the thread
        # row, so a proposal left over from a previous run would break its foreign
        # key mid-statement.
        conn.execute("DELETE FROM proposals WHERE id = 99006")
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, raw_body) "
            "VALUES ('e6-thread', '[TEST] E6 conflict', 'anjana@example.com', 'body')"
        )
        conn.execute(
            "INSERT INTO proposals (id, thread_id, status, proposed_slots, "
            "recipient_selected_slot) VALUES (99006, 'e6-thread', 'pending_invite', '[]', ?)",
            ('{"start": "2026-08-12T10:00:00-06:00", "end": "2026-08-12T10:30:00-06:00"}',),
        )
        conn.commit()

    clash = {
        "error": "Slot is no longer free — 1 conflicting event(s).",
        "kory_message": "Something landed on your calendar.",
        "conflicting_events": [{"id": "evt-intruder"}],
    }

    with (
        patch.object(ca, "_confirm_time_conflict", return_value=clash),
        patch.object(ca, "_confirm_selected_hold") as mock_confirm,
        patch.object(ca, "_release_unused_holds") as mock_release,
    ):
        result = ca.execute_lexi_approval(
            99006, "approved", "", "kory", execution_phase="send_invite"
        )

    assert result.ok is False
    assert result.calendar_event_id is None
    assert result.holds_confirmed == 0
    # Neither of these may run: one would book over the clash, the other would
    # hand the slot away while Kory is still deciding.
    mock_confirm.assert_not_called()
    mock_release.assert_not_called()
