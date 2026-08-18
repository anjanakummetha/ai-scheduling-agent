"""A follow-up send must not place a second set of holds — decided by what
happened, not by a string prefix in a note.

Whether an approval was a first offer or a hold reminder used to be decided by
`scheduling_note.startswith("HOLD_REMINDER")`. That controlled two things at
once: hold placement, and whether the pre-send slot gate ran at all. So the
reminder path traded a false refusal (our own holds looking like conflicts) for
no validation whatsoever.

Now the send asks `offer_already_sent` — a write-once column — and always runs
the gate, which discounts our own holds. Hold placement is called unconditionally
because it is per-slot idempotent: every staged slot that already has a live hold
is skipped.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import execute_lexi_approval
from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "followup-idempotent-thread"


def _weekday_slot(offset_days: int = 14) -> dict[str, str]:
    day = date.today() + timedelta(days=offset_days)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 10, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 10, 30, tzinfo=MT).isoformat(),
    }


@pytest.fixture
def sent_offer_with_holds():
    """A proposal whose offer went out, with a live hold on its one slot, now
    re-staged as a reminder draft awaiting Kory."""
    slot = _weekday_slot()
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, sender_email,"
            " raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] follow-up", "Dana <dana@example.com>",
             "dana@example.com", "can we talk?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, drafted_reply, scheduling_note)"
            " VALUES (?,?,?,?,?,?)",
            (THREAD, ProposalStatus.OFFER_SENT, "referral_or_intro",
             json.dumps([slot]), "Just circling back — does that time still work?",
             "HOLD_REMINDER: No reply after hold period."),
        )
        pid = int(cur.lastrowid)
        record_fact(conn, pid, "offer_sent_at")
        conn.execute(
            "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "evt-existing-hold", slot["start"], slot["end"],
             (datetime.now(MT) + timedelta(days=2)).isoformat()),
        )
        # Re-staged as a reminder awaiting approval, exactly as the hold-reminder
        # job leaves it.
        conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?",
            (ProposalStatus.PENDING_APPROVAL, pid),
        )
        conn.commit()
    yield pid, slot
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _approve(pid: int, slot: dict[str, str], busy: list[dict]):
    calendar = {"status": "available", "horizon_days": 45, "busy_events": busy}
    with (
        patch("app.agents.comms_agent._send_drafted_reply", return_value=(True, None)),
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value=calendar,
        ),
        patch("app.integrations.hold_placement.place_offered_holds") as place,
    ):
        place.side_effect = lambda conn, **kw: 0
        result = execute_lexi_approval(
            pid, "approved", slot["start"], "kory", decision_source="test"
        )
    return result, place


def test_a_follow_up_send_places_no_new_holds(sent_offer_with_holds):
    pid, slot = sent_offer_with_holds
    result, place = _approve(pid, slot, busy=[])

    assert result.ok, result.errors
    assert result.status == ProposalStatus.OFFER_SENT
    # place_offered_holds is idempotent, but on a fully-held proposal the caller
    # short-circuits before reaching it at all.
    assert place.call_count == 0, "a follow-up must not re-place holds"
    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE proposal_id = ?", (pid,)
        ).fetchone()
    assert rows["n"] == 1, "the counterpart must not get a second set of holds"


def test_our_own_hold_does_not_make_the_gate_refuse_the_follow_up(sent_offer_with_holds):
    """The gate now RUNS on a follow-up. It must not trip over our own hold.

    We hold what we offer, so a HOLD event sits at exactly the time the reminder
    re-quotes. Counting it as a conflict would refuse Lexi's own follow-up as
    "already booked" — which is why this path used to skip validation entirely.
    """
    pid, slot = sent_offer_with_holds
    own_hold = {
        "id": "evt-existing-hold",
        "subject": "HOLD: intro call",
        "start": {"dateTime": slot["start"]},
        "end": {"dateTime": slot["end"]},
    }
    result, _ = _approve(pid, slot, busy=[own_hold])

    assert result.ok, result.errors
    assert not any("no longer free" in e for e in (result.errors or []))


def test_a_genuine_conflict_still_refuses_a_follow_up(sent_offer_with_holds):
    """Discounting our own holds must not disable the check."""
    pid, slot = sent_offer_with_holds
    someone_elses = {
        "id": "evt-real-meeting",
        "subject": "Board call",
        "start": {"dateTime": slot["start"]},
        "end": {"dateTime": slot["end"]},
    }
    result, _ = _approve(pid, slot, busy=[someone_elses])

    assert result.ok is False
    assert any("no longer free" in e for e in (result.errors or [])), result.errors


def test_the_offer_timestamp_is_not_refreshed_by_a_follow_up(sent_offer_with_holds):
    """offer_sent_at records when the counterpart FIRST heard from us."""
    pid, slot = sent_offer_with_holds
    with get_lexi_connection() as conn:
        before = conn.execute(
            "SELECT offer_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()["offer_sent_at"]

    _approve(pid, slot, busy=[])

    with get_lexi_connection() as conn:
        after = conn.execute(
            "SELECT offer_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()["offer_sent_at"]
    assert after == before, "a reminder must not make a stale offer look fresh"


def test_a_real_meeting_sitting_exactly_on_a_held_time_is_still_a_conflict(
    sent_offer_with_holds,
):
    """Discounting our own holds must key off identity, not just the clock.

    A real meeting can land on exactly the interval we are holding — the
    counterpart sends a competing invite, or Kory accepts one. Matching purely
    on the interval would wave it through, which is the single thing this gate
    exists to catch.
    """
    pid, slot = sent_offer_with_holds
    impostor = {
        "id": "evt-not-ours",
        "subject": "Board call",           # not titled HOLD:
        "start": {"dateTime": slot["start"]},
        "end": {"dateTime": slot["end"]},
    }
    result, _ = _approve(pid, slot, busy=[impostor])
    assert result.ok is False
    assert any("no longer free" in e for e in (result.errors or [])), result.errors


def test_an_orphaned_hold_from_a_crashed_run_is_still_recognised_as_ours(
    sent_offer_with_holds,
):
    """A run that died before its database row landed leaves a HOLD: event we
    have no id for. It is still ours, and must not block the send."""
    pid, slot = sent_offer_with_holds
    orphan = {
        "id": "evt-id-we-never-recorded",
        "subject": "HOLD: Intro call w/ Dana",
        "start": {"dateTime": slot["start"]},
        "end": {"dateTime": slot["end"]},
    }
    result, _ = _approve(pid, slot, busy=[orphan])
    assert result.ok, result.errors
