"""E-8: typed `cancel meeting #N` cancels a booked meeting — before this,
Kory had no way to say yes to a recipient's cancellation request."""

from __future__ import annotations

from unittest.mock import patch

from app.bot.teams_text import parse_teams_command


def test_parse_cancel_meeting_variants():
    for text in (
        "cancel meeting #6835",
        "cancel the meeting 6835",
        "cancel invite #6835",
        "cancel #6835 — she asked to call it off",
        "cancel for #6835",
    ):
        out = parse_teams_command(text)
        assert out is not None and out["action"] == "cancel_meeting", text
        assert out["proposal_id"] == 6835

    reason = parse_teams_command("cancel meeting #6835 — recipient request")
    assert reason["reason"] == "recipient request"
    # reject must keep its own action
    assert parse_teams_command("reject #6835 — nope")["action"] == "reject"


def test_cancel_command_routes_to_executor():
    from app.teams import commands

    with patch(
        "app.agents.comms_agent.cancel_booked_meeting",
        return_value={
            "ok": True,
            "proposal_id": 6835,
            "status": "cancelled",
            "subject": "[TEST] Intro chat next week? — LT-D1",
            "sender": "anjana@example.com",
        },
    ) as cancel:
        out = commands.handle_teams_command("cancel meeting #6835 — test complete")
    assert out["ok"] is True
    cancel.assert_called_once()
    assert cancel.call_args.kwargs["reason"] == "test complete"
    assert "cancelled" in out["message"].lower()
    assert "cancellation notice" in out["message"]


def test_cancel_refuses_unbooked_proposal():
    from app.teams import commands

    with patch(
        "app.agents.comms_agent.cancel_booked_meeting",
        return_value={"ok": False, "error": "Proposal 42 has no booked meeting to cancel (status=offer_sent)."},
    ):
        out = commands.handle_teams_command("cancel meeting #42")
    assert out["ok"] is False
    assert "no booked meeting" in out["message"]


def test_cancel_executor_deletes_event_and_marks_cancelled():
    from app.agents import comms_agent as ca
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, raw_body) "
            "VALUES ('lt-e8-thread', '[TEST] E8 cancel', 'anjana@example.com', 'body')"
        )
        conn.execute("DELETE FROM proposals WHERE id = 99002")
        conn.execute(
            "INSERT INTO proposals (id, thread_id, status, proposed_slots, invite_event_id) "
            "VALUES (99002, 'lt-e8-thread', 'executed', '[]', 'evt-booked')"
        )
        conn.commit()

    deleted: list[str] = []
    try:
        with patch(
            "app.integrations.outlook_calendar.delete_calendar_event",
            side_effect=lambda eid: deleted.append(eid) or None,
        ):
            out = ca.cancel_booked_meeting(99002, reason="test", authorized_by="kory")
        assert out["ok"] is True
        assert deleted == ["evt-booked"]
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT status, invite_event_id FROM proposals WHERE id = 99002"
            ).fetchone()
        assert row["status"] == "cancelled"
        assert row["invite_event_id"] is None
    finally:
        with get_lexi_connection() as conn:
            conn.execute("DELETE FROM approvals WHERE proposal_id = 99002")
            conn.execute("DELETE FROM audit_log WHERE reference_id = '99002'")
            conn.execute("DELETE FROM proposals WHERE id = 99002")
            conn.execute("DELETE FROM email_threads WHERE thread_id = 'lt-e8-thread'")
            conn.commit()
