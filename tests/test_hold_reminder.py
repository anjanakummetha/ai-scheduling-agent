"""Hold reminder staging behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.scheduling.hold_reminder import (
    HOLD_REMINDER_PREFIX,
    compose_hold_reminder_draft,
    is_hold_reminder_proposal,
    process_due_hold_reminders,
)


def test_compose_hold_reminder_includes_slots() -> None:
    draft = compose_hold_reminder_draft(
        sender="Jane Doe <jane@example.com>",
        subject="Intro call",
        slots=[{"start": "2026-07-01T18:00:00+00:00", "end": "2026-07-01T18:30:00+00:00"}],
    )
    assert "Jane" in draft
    assert "circling back" in draft.lower()
    assert "Let's Win" in draft


def test_is_hold_reminder_proposal() -> None:
    assert is_hold_reminder_proposal({"scheduling_note": f"{HOLD_REMINDER_PREFIX}: test"})
    assert not is_hold_reminder_proposal({"scheduling_note": "other"})


@patch("app.scheduling.hold_reminder._notify_kory_hold_reminder")
def test_process_due_hold_reminders_stages_draft(mock_notify, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")

    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        conn.execute(
            """
            INSERT INTO email_threads (thread_id, subject, sender, raw_body)
            VALUES ('t1', 'Coffee?', 'bob@example.com', 'Can we meet?')
            """
        )
        conn.execute(
            """
            INSERT INTO proposals (
                thread_id, status, proposed_slots, intent_classification
            ) VALUES ('t1', 'offer_sent', ?, 'coffee')
            """,
            ('[{"start":"2026-07-01T18:00:00+00:00","end":"2026-07-01T18:30:00+00:00"}]',),
        )
        proposal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at, created_at)
            VALUES (?, 'evt-1', '2026-07-01T18:00:00+00:00', '2026-07-01T18:30:00+00:00', ?, ?)
            """,
            (proposal_id, expires, old),
        )
        conn.commit()

    staged = process_due_hold_reminders()
    assert len(staged) == 1
    assert staged[0]["proposal_id"] == proposal_id
    mock_notify.assert_called_once_with(proposal_id)

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, scheduling_note, drafted_reply FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    assert row["status"] == "pending_approval"
    assert str(row["scheduling_note"]).startswith(HOLD_REMINDER_PREFIX)
    assert "circling back" in str(row["drafted_reply"]).lower()
