"""Live E-7: a reply on an EXECUTED (booked) thread was dropped in total
silence — the generic followup handler's notify list excluded 'executed'."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from app.agents import lexi_thread_followup as ltf

RESCHEDULE_BODY = (
    "I'm so sorry — something came up on my end for Monday. "
    "Could we find a different time that week?"
)


def _mem_db(status: str = "executed"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE email_threads (thread_id TEXT PRIMARY KEY, raw_body TEXT)"
    )
    conn.execute(
        "CREATE TABLE proposals (id INTEGER PRIMARY KEY, thread_id TEXT, status TEXT, "
        "drafted_reply TEXT, proposed_slots TEXT, recipient_selected_slot TEXT, "
        "intent_classification TEXT, "
        "teams_approval_notified_at TEXT, invite_event_id TEXT, updated_at TEXT)"
    )
    # Transitions write an audit row; without this table the state module
    # degrades to a log line and the test cannot see WHY a status moved.
    conn.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, step_name TEXT, "
        "reference_id TEXT, log_level TEXT, message TEXT, payload TEXT, "
        "timestamp TEXT DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO email_threads VALUES ('t1', 'Could we set up a 30-minute intro call?')")
    conn.execute(
        "INSERT INTO proposals (id, thread_id, status, recipient_selected_slot, invite_event_id) "
        "VALUES (6835, 't1', ?, '{\"start\": \"x\"}', 'evt-original-invite')",
        (status,),
    )
    conn.commit()
    return conn


def _proposal(status: str = "executed"):
    return {
        "proposal_id": 6835,
        "status": status,
        "is_delegation": 1,
        "sender": "anjana@example.com",
        "subject": "[TEST] Intro chat next week? — LT-D1",
    }


def test_reschedule_reply_on_booked_meeting_regenerates_offer():
    conn = _mem_db()
    with (
        patch.object(ltf, "get_lexi_connection", lambda: conn),
        patch(
            "app.agents.scheduler_agent.process_proposal_schedule", return_value=True
        ) as sched,
        patch("app.bot.teams_publisher.schedule_teams_approval_push") as push,
    ):
        out = ltf._handle_generic_lexi_followup(
            {}, _proposal(), body=RESCHEDULE_BODY
        )
    assert out is not None and out.get("rescheduled") is True
    # reoffer=True is the load-bearing part. Opening a new round on a thread
    # that already has a booked meeting is legitimate, but it must be asked for
    # explicitly — the default refuses, which is what stops a retry or a
    # redelivered webhook from silently re-drafting a live offer.
    sched.assert_called_once_with(6835, reoffer=True)
    push.assert_called_once()
    prop = conn.execute(
        "SELECT status, recipient_selected_slot, invite_event_id FROM proposals WHERE id=6835"
    ).fetchone()
    assert prop["status"] == "pending_triage"
    assert prop["recipient_selected_slot"] is None  # stale pick must not survive
    assert prop["invite_event_id"] == "evt-original-invite"  # meeting untouched
    assert "current invite stays" in out["message"]
    audited = conn.execute(
        "SELECT message FROM audit_log WHERE reference_id = '6835' "
        "AND step_name = 'proposal_transition'"
    ).fetchone()
    assert audited and "executed -> pending_triage" in audited["message"], (
        "every status change must say who moved it and why"
    )


def test_cancel_reply_pings_kory_and_touches_nothing():
    conn = _mem_db()
    notified = {}

    def fake_notify(proposal_id, *, summary, kind):
        notified["kind"] = kind
        notified["summary"] = summary

    with (
        patch.object(ltf, "get_lexi_connection", lambda: conn),
        patch.object(ltf, "_notify_kory_followup", side_effect=fake_notify),
    ):
        out = ltf._handle_generic_lexi_followup(
            {}, _proposal(), body="Let's cancel the call — I no longer need it."
        )
    assert out is not None and out["ok"] is True
    assert notified["kind"] == "cancel_request"
    assert "NOT touched the calendar" in notified["summary"]
    prop = conn.execute("SELECT status FROM proposals WHERE id=6835").fetchone()
    assert prop["status"] == "executed"


def test_cant_make_it_reads_as_reschedule_not_cancel():
    body = "I can't make Monday anymore — cancel that slot and find another time?"
    assert ltf._RESCHEDULE_RE.search(body)
    conn = _mem_db()
    with (
        patch.object(ltf, "get_lexi_connection", lambda: conn),
        patch(
            "app.agents.scheduler_agent.process_proposal_schedule", return_value=True
        ),
        patch("app.bot.teams_publisher.schedule_teams_approval_push"),
    ):
        out = ltf._handle_generic_lexi_followup({}, _proposal(), body=body)
    assert out is not None and out.get("rescheduled") is True


def test_generic_reply_on_booked_meeting_pings_kory():
    conn = _mem_db()
    notified = {}
    with (
        patch.object(ltf, "get_lexi_connection", lambda: conn),
        patch.object(
            ltf,
            "_notify_kory_followup",
            side_effect=lambda pid, *, summary, kind: notified.update(kind=kind),
        ),
    ):
        out = ltf._handle_generic_lexi_followup(
            {}, _proposal(), body="Looking forward to it — should I prepare anything?"
        )
    assert out is not None and out["action"] == "lexi_thread_followup"
    assert notified["kind"] == "thread_update"


def test_reschedule_dispatch_removes_previous_invite():
    """Second invite dispatch must delete the ORIGINAL meeting (move semantics)
    and record the new event id on the proposal."""
    from unittest.mock import patch as _patch

    from app.agents import comms_agent as ca
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, raw_body) "
            "VALUES ('lt-e7-thread', '[TEST] E7 reschedule', 'anjana@example.com', 'body')"
        )
        conn.execute("DELETE FROM proposals WHERE id = 99001")
        conn.execute(
            "INSERT INTO proposals (id, thread_id, status, proposed_slots, "
            "recipient_selected_slot, invite_event_id) VALUES (99001, 'lt-e7-thread', "
            "'pending_invite', '[]', "
            "'{\"start\": \"2026-08-12T10:00:00-06:00\", \"end\": \"2026-08-12T10:30:00-06:00\"}', "
            "'evt-old-invite')"
        )
        conn.commit()

    deleted: list[str] = []

    def fake_delete(event_id):
        deleted.append(event_id)
        return None

    try:
        with (
            _patch.object(ca, "_confirm_selected_hold", return_value=("evt-new-invite", [])),
            _patch.object(ca, "_release_unused_holds", return_value=0),
            # The confirm-time conflict re-check fails closed, so without a
            # calendar it refuses to book. This test is about reschedule
            # semantics, so give it a clear calendar.
            _patch.object(ca, "_confirm_time_conflict", return_value=None),
            _patch(
                "app.integrations.outlook_calendar.delete_calendar_event",
                side_effect=fake_delete,
            ),
        ):
            result = ca.execute_lexi_approval(
                99001, "approved", "", "kory", execution_phase="send_invite"
            )
        assert result.ok is True
        assert result.calendar_event_id == "evt-new-invite"
        assert deleted == ["evt-old-invite"]
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT status, invite_event_id FROM proposals WHERE id = 99001"
            ).fetchone()
        assert row["status"] == "executed"
        assert row["invite_event_id"] == "evt-new-invite"
    finally:
        with get_lexi_connection() as conn:
            conn.execute("DELETE FROM approvals WHERE proposal_id = 99001")
            conn.execute("DELETE FROM audit_log WHERE reference_id = '99001'")
            conn.execute("DELETE FROM proposals WHERE id = 99001")
            conn.execute("DELETE FROM email_threads WHERE thread_id = 'lt-e7-thread'")
            conn.commit()


def test_cancel_with_weekday_beats_inbound_time_path():
    """Live E-8: 'cancel our Wednesday call' was hijacked by the inbound
    time-suggestion detector because it mentions a weekday. On an executed
    thread, cancel/reschedule wording must be routed FIRST."""
    conn = _mem_db()
    notified = {}
    raw = {
        "conversation_id": "conv-1",
        "sender": "anjana@example.com",
        "subject": "Re: [TEST] Intro chat next week? — LT-D1",
        "raw_body": "I need to cancel our Wednesday call — the intro is no longer needed.",
    }
    with (
        patch.object(ltf, "get_lexi_connection", lambda: conn),
        patch.object(ltf, "_find_lexi_involved_proposal", return_value=_proposal()),
        patch.object(
            ltf,
            "_try_inbound_time_suggestion",
            side_effect=AssertionError("inbound-time path must not run"),
        ),
        patch.object(
            ltf,
            "_notify_kory_followup",
            side_effect=lambda pid, *, summary, kind: notified.update(kind=kind),
        ),
    ):
        out = ltf.try_handle_lexi_thread_followup(raw)
    assert out is not None and out["ok"] is True
    assert notified["kind"] == "cancel_request"


def test_failed_regeneration_still_pings_kory():
    conn = _mem_db()
    notified = {}
    with (
        patch.object(ltf, "get_lexi_connection", lambda: conn),
        patch(
            "app.agents.scheduler_agent.process_proposal_schedule", return_value=False
        ),
        patch.object(
            ltf,
            "_notify_kory_followup",
            side_effect=lambda pid, *, summary, kind: notified.update(kind=kind),
        ),
    ):
        out = ltf._handle_generic_lexi_followup({}, _proposal(), body=RESCHEDULE_BODY)
    assert out is not None
    assert notified["kind"] == "reschedule_failed"


class TestExtraAttendeesFromReply:
    """Live H-10: 'include my colleague — her email is X' was silently dropped
    from the invite."""

    def test_extracts_colleague_and_skips_internal_and_quoted(self):
        from app.agents.comms_agent import _extra_attendees_from_reply

        body = (
            "Monday, August 24 at 12:00 PM ET works for me. Could you also "
            "include my colleague? Her email is anjana.kummetha@iconicfounders.com.\n\n"
            "On Wed, Aug 5, 2026 Lexi Knightly <lexi@iconicfounders.com> wrote:\n"
            "> quoted-person@elsewhere.com said hi\n"
        )
        assert _extra_attendees_from_reply(body) == [
            "anjana.kummetha@iconicfounders.com"
        ]

    def test_no_addresses_means_no_extras(self):
        from app.agents.comms_agent import _extra_attendees_from_reply

        assert _extra_attendees_from_reply("Monday works, thanks!") == []

    def test_slot_choice_stores_extras_and_parse_carries_them(self):
        from app.agents import comms_agent as ca
        from app.storage.lexi_db import get_lexi_connection

        with get_lexi_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, raw_body) "
                "VALUES ('lt-h10', '[TEST] H10', 'Anjana <anjanakummetha@gmail.com>', 'body')"
            )
            conn.execute("DELETE FROM proposals WHERE id = 99004")
            conn.execute(
                "INSERT INTO proposals (id, thread_id, status, proposed_slots) "
                "VALUES (99004, 'lt-h10', 'offer_sent', '[]')"
            )
            conn.commit()
        try:
            out = ca.mark_recipient_slot_choice(
                99004,
                {"start": "2026-08-24T10:00:00-06:00", "end": "2026-08-24T10:30:00-06:00"},
                reply_body=(
                    "Monday, August 24 at 12:00 PM ET works. Include my colleague: "
                    "anjana.kummetha@iconicfounders.com please!"
                ),
            )
            assert out["ok"] is True
            with get_lexi_connection() as conn:
                raw = conn.execute(
                    "SELECT recipient_selected_slot FROM proposals WHERE id = 99004"
                ).fetchone()[0]
            slot = ca._parse_recipient_selected_slot({"recipient_selected_slot": raw})
            assert slot["extra_attendees"] == ["anjana.kummetha@iconicfounders.com"]
        finally:
            with get_lexi_connection() as conn:
                conn.execute("DELETE FROM audit_log WHERE reference_id = '99004'")
                conn.execute("DELETE FROM proposals WHERE id = 99004")
                conn.execute("DELETE FROM email_threads WHERE thread_id = 'lt-h10'")
                conn.commit()
