"""A send that did not happen must never be recorded as one.

Everything downstream of "the email went out" trusts that answer absolutely: the
write-once offer_sent_at fact is stamped, holds go on Kory's calendar, the
proposal moves to offer_sent, and the counterpart is assumed to be holding
times. If the send did not actually happen, all of it is a lie that cannot be
walked back — the fact column is deliberately write-once.

Two ways it could be told that lie:

  * send_draft's answer was discarded. It raises on an explicit refusal, but a
    response carrying neither a success flag nor a log id returned quietly, and
    the caller reported the offer as sent.
  * the Lexi-voice and sandbox paths returned (False, None) — failed, with
    nothing to say. No error text for Kory, and the escalation that would have
    told him was conditioned on there being an error, so it stayed silent. From
    the chat window that is indistinguishable from Lexi ignoring him.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import execute_lexi_approval
from app.scheduling.proposal_state import ProposalStatus
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "silent-send-thread"
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=11)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 9, 30, tzinfo=MT).isoformat(),
    }


@pytest.fixture
def staged():
    slot = _slot()
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] silent send", "Dana <dana@example.com>",
             "dana@example.com", "can we meet?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, drafted_reply, send_channel, voice_mode)"
            " VALUES (?,?,?,?,?,?,?)",
            (THREAD, ProposalStatus.PENDING_APPROVAL, "referral_or_intro",
             json.dumps([slot]), "Hi Dana,\n\nA time.\n\nLet's Win,\nKory", "kory", "kory"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
    yield pid, slot
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


@contextmanager
def _real_send_path():
    """Take the production branch rather than the sandbox loopback.

    The suite runs in sandbox write mode, where every send is redirected through
    send_pilot_reply_for_proposal. That branch is worth testing in its own right
    (below), but it hides the two paths Kory's production traffic actually uses.
    settings is a frozen dataclass shared by reference, hence object.__setattr__.
    """
    from app.config import settings

    previous = settings.sandbox_email_loopback
    object.__setattr__(settings, "sandbox_email_loopback", False)
    try:
        yield
    finally:
        object.__setattr__(settings, "sandbox_email_loopback", previous)


def _approve(pid: int, slot: dict[str, str], *, real_send_path: bool = True, **send_patches):
    with ExitStack() as stack:
        if real_send_path:
            stack.enter_context(_real_send_path())
        stack.enter_context(
            patch(
                "app.scheduling.calendar_context.load_scheduling_calendar_context",
                return_value=FREE,
            )
        )
        holds = stack.enter_context(
            patch("app.integrations.hold_placement.place_offered_holds", return_value=0)
        )
        escalate = stack.enter_context(
            patch("app.scheduling.kory_escalation.escalate_to_kory", return_value={})
        )
        for target, kwargs in send_patches.items():
            stack.enter_context(patch(target, **kwargs))
        result = execute_lexi_approval(
            pid, "approved", slot["start"], "kory", decision_source="test"
        )
    return result, holds, escalate


def _state(pid: int):
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT status, offer_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()


def test_an_unconfirmed_send_is_not_recorded_as_sent(staged):
    """Outlook answered, but confirmed nothing. That is not a send."""
    pid, slot = staged
    result, holds, escalate = _approve(
        pid, slot,
        **{
            "app.agents.comms_agent.create_draft_reply": {"return_value": ("draft-1", None)},
            # Neither a raise nor a log id — the quiet case.
            "app.agents.comms_agent.send_draft": {"return_value": None},
        },
    )

    row = _state(pid)
    assert result.email_sent is False
    assert row["status"] == ProposalStatus.PENDING_APPROVAL, (
        "an unconfirmed send moved the proposal to offer_sent"
    )
    assert row["offer_sent_at"] is None, (
        "a send that never happened stamped the write-once world fact"
    )
    assert holds.called is False, "holds were placed for an offer nobody received"
    assert result.errors, "the failure must say something Kory can act on"


def test_a_confirmed_send_still_works(staged):
    """The guard must not block a real send."""
    pid, slot = staged
    result, holds, _ = _approve(
        pid, slot,
        **{
            "app.agents.comms_agent.create_draft_reply": {"return_value": ("draft-1", None)},
            "app.agents.comms_agent.send_draft": {"return_value": "log-42"},
        },
    )
    assert result.email_sent is True, result.errors
    assert _state(pid)["status"] == ProposalStatus.OFFER_SENT
    assert _state(pid)["offer_sent_at"], "a real send must stamp the world fact"


def test_a_send_that_fails_with_nothing_to_say_still_reaches_kory(staged):
    """The silent case. It used to leave no error, so the escalation — which
    fired off result.errors — never ran, and Kory saw nothing at all."""
    pid, slot = staged
    result, _holds, escalate = _approve(
        pid, slot,
        **{
            "app.agents.comms_agent.create_draft_reply": {"return_value": ("draft-1", None)},
            "app.agents.comms_agent.send_draft": {"return_value": None},
        },
    )
    assert escalate.called, "a failed send must always be escalated to Kory"
    assert result.ok is False


def test_the_sandbox_loopback_path_reports_its_failure_too(staged):
    """The branch this suite actually runs in."""
    pid, slot = staged
    result, _holds, escalate = _approve(
        pid, slot, real_send_path=False,
        **{"app.agents.comms_agent.send_pilot_reply_for_proposal":
           {"return_value": (None, None)}},
    )
    assert result.email_sent is False
    assert result.errors, "the sandbox path failed with nothing to say"
    assert _state(pid)["offer_sent_at"] is None
    assert escalate.called


def test_the_lexi_voice_path_reports_its_failure_too(staged):
    pid, slot = staged
    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET send_channel='lexi', voice_mode='lexi' WHERE id=?", (pid,)
        )
        conn.commit()

    result, _holds, escalate = _approve(
        pid, slot,
        **{"app.agents.comms_agent.send_reply_in_thread": {"return_value": (None, None)}},
    )
    assert result.email_sent is False
    assert result.errors, "the Lexi-voice path failed with nothing to say"
    assert _state(pid)["offer_sent_at"] is None
    assert escalate.called


# ---------------------------------------------------------------------------
# The invite half
# ---------------------------------------------------------------------------


@pytest.fixture
def picked(staged):
    """The counterpart has chosen a time and Kory is about to send the invite."""
    pid, slot = staged
    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET status = ?, recipient_selected_slot = ? WHERE id = ?",
            (ProposalStatus.OFFER_SENT, json.dumps(slot), pid),
        )
        conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?",
            (ProposalStatus.PENDING_INVITE, pid),
        )
        conn.execute(
            "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "evt-hold", slot["start"], slot["end"],
             (datetime.now(MT) + timedelta(days=2)).isoformat()),
        )
        conn.commit()
    return pid, slot


def _send_invite(pid: int, slot: dict[str, str], confirmed: str | None):
    from app.agents.comms_agent import execute_lexi_approval

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.agents.comms_agent._confirm_time_conflict", return_value=None)
        )
        stack.enter_context(
            patch(
                "app.agents.comms_agent._confirm_selected_hold",
                return_value=(confirmed, [] if confirmed else ["Outlook rejected the event."]),
            )
        )
        escalate = stack.enter_context(
            patch("app.scheduling.kory_escalation.escalate_to_kory", return_value={})
        )
        result = execute_lexi_approval(
            pid, "approved", slot["start"], "kory",
            decision_source="test", execution_phase="send_invite",
        )
    return result, escalate


def _holds(pid: int) -> int:
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE proposal_id = ?", (pid,)
        ).fetchone()["n"]


def test_a_failed_invite_does_not_mark_the_meeting_booked(picked):
    """The worst version of the record disagreeing with the world.

    The calendar write fails, so no meeting exists. Marking the proposal
    executed said "booked", dropped it out of every queue, and left no way back
    — executed cannot legally return to pending_invite. Kory would need DB
    surgery, and the counterpart is expecting a meeting nobody holds.
    """
    pid, slot = picked
    result, escalate = _send_invite(pid, slot, confirmed=None)

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, invite_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()

    assert result.ok is False
    assert row["status"] == ProposalStatus.PENDING_INVITE, (
        "a failed invite marked the proposal executed"
    )
    assert row["invite_sent_at"] is None, "a meeting that does not exist was recorded as booked"
    assert escalate.called, "Kory was never told the invite failed"


def test_a_failed_invite_keeps_the_holds(picked):
    """The holds are the only thing still protecting that time."""
    pid, slot = picked
    assert _holds(pid) == 1
    _send_invite(pid, slot, confirmed=None)
    assert _holds(pid) == 1, "the held time was handed away after a failed invite"


def test_a_failed_invite_can_be_retried(picked):
    """Because it stayed in pending_invite, `send invite #N` still works."""
    pid, slot = picked
    _send_invite(pid, slot, confirmed=None)
    result, _ = _send_invite(pid, slot, confirmed="evt-booked")

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, invite_sent_at FROM proposals WHERE id = ?", (pid,)
        ).fetchone()
    assert result.ok is True, result.errors
    assert row["status"] == ProposalStatus.EXECUTED
    assert row["invite_sent_at"], "the successful retry did not record the booking"


def test_a_successful_invite_still_releases_the_other_holds(picked):
    """The guard must not leave stale holds behind on the happy path."""
    pid, slot = picked
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "evt-other", slot["start"], slot["end"],
             (datetime.now(MT) + timedelta(days=2)).isoformat()),
        )
        conn.commit()
    assert _holds(pid) == 2

    with patch("app.agents.comms_agent.delete_calendar_event"):
        result, _ = _send_invite(pid, slot, confirmed="evt-hold")
    assert result.ok is True, result.errors
    assert _holds(pid) == 1, "the unused hold was left on Kory's calendar"
