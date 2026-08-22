"""A decline's words must reach the engine's next round.

Live proposal 10563 (2026-08-22): the counterpart declined with "anything the
following week?", the proposal parked in pending_reoffer, Kory retried — and
Lexi re-offered the EXACT Tuesday slots that had just been declined. The
decline body went only into an audit payload; the engine schedules from
email_threads.raw_body via scheduling_body(), which still ended at the
original ask, so the pinned week-shift parsers never saw the push.

mark_recipient_reoffer_request now folds the decline into the thread body,
newest-on-top, the same shape ingestion writes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import mark_recipient_reoffer_request
from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "decline-body-thread"
ORIGINAL_ASK = "Would Tuesday or Thursday work next week? I'm on Eastern time."
DECLINE = "Neither of those works I'm afraid — anything the following week?"


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=7)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 16, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 17, 0, tzinfo=MT).isoformat(),
    }


@pytest.fixture
def sent_offer():
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] Coffee next week", "Dana <dana@example.com>",
             "dana@example.com", ORIGINAL_ASK),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots) VALUES (?,?,?,?)",
            (THREAD, ProposalStatus.OFFER_SENT, "referral_or_intro",
             json.dumps([_slot()])),
        )
        pid = int(cur.lastrowid)
        record_fact(conn, pid, "offer_sent_at")
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _thread_body() -> str:
    with get_lexi_connection() as conn:
        return str(
            conn.execute(
                "SELECT raw_body FROM email_threads WHERE thread_id = ?", (THREAD,)
            ).fetchone()["raw_body"]
        )


def test_the_decline_lands_in_the_body_the_engine_reads(sent_offer):
    result = mark_recipient_reoffer_request(sent_offer, reply_body=DECLINE)
    assert result.get("ok") is True, result

    body = _thread_body()
    assert body.startswith(DECLINE), (
        "the decline is not the newest message in the thread body — the "
        "window parser will re-derive the declined week (live 10563)"
    )
    assert ORIGINAL_ASK in body, "the original ask was lost from the thread"
    assert "[Prior messages in this email chain]" in body


def test_a_redelivered_decline_is_not_stacked_twice(sent_offer):
    mark_recipient_reoffer_request(sent_offer, reply_body=DECLINE)
    # Webhooks redeliver; the same decline arriving again must not stack.
    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?",
            (ProposalStatus.OFFER_SENT, sent_offer),
        )
        conn.commit()
    mark_recipient_reoffer_request(sent_offer, reply_body=DECLINE)
    assert _thread_body().count(DECLINE) == 1


def test_the_merged_body_shifts_the_scheduling_window(sent_offer):
    """The point of persisting the decline: the next plan must move a week."""
    from app.scheduling.scheduling_plan import build_scheduling_plan

    before = build_scheduling_plan(
        subject="[TEST] Coffee next week", body=_thread_body(),
        intent="referral_or_intro", use_llm=False,
    )
    mark_recipient_reoffer_request(sent_offer, reply_body=DECLINE)
    after = build_scheduling_plan(
        subject="[TEST] Coffee next week", body=_thread_body(),
        intent="referral_or_intro", use_llm=False,
    )
    assert after.window.start > before.window.start, (
        f"window did not move: before={before.window.start} after={after.window.start}"
    )
    assert after.window.start - before.window.start >= timedelta(days=5), (
        f"push was less than a week: {before.window.start} -> {after.window.start}"
    )


def test_the_retry_never_reoffers_the_declined_week(sent_offer):
    """End-to-end through the real retry: the staged slots must leave the week
    the counterpart declined. Stated days ('Tuesday or Thursday') are parsed
    from the whole thread; without the window filter they re-anchor to the
    nearest Tuesday — inside the declined week — and get honored by the
    on-date path (live 10563: Aug 25 offered twice)."""
    from unittest.mock import patch

    from app.agents.inbound_reply import retry_scheduling_with_guidance

    mark_recipient_reoffer_request(sent_offer, reply_body=DECLINE)

    # The declined week's Tuesday is busy until late afternoon — the live
    # calendar shape. Stale Aug-25-style candidates then fail validation and
    # fall to the on-date path, whose gate call carries no plan and so never
    # window-checks; without the candidate filter, the declined Tuesday's
    # open afternoon is offered right back (live 10563's exact output).
    today = date.today()
    next_monday = today + timedelta(days=(7 - today.weekday()))
    declined_tue = next_monday + timedelta(days=1)
    busy = [
        {
            "subject": f"block {h}",
            "start": {"dateTime": datetime(declined_tue.year, declined_tue.month,
                                           declined_tue.day, h, 0, tzinfo=MT).isoformat()},
            "end": {"dateTime": datetime(declined_tue.year, declined_tue.month,
                                         declined_tue.day, h + 2, 0, tzinfo=MT).isoformat()},
        }
        for h in (8, 10, 12, 14)
    ]
    ctx = {"status": "available", "horizon_days": 45, "busy_events": busy}
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=ctx,
    ), patch("app.bot.teams_publisher.schedule_teams_approval_push"):
        result = retry_scheduling_with_guidance(
            sent_offer, "mornings if possible, otherwise whatever works that week"
        )

    assert result.get("ok") is True, result
    with get_lexi_connection() as conn:
        raw = conn.execute(
            "SELECT proposed_slots FROM proposals WHERE id = ?", (sent_offer,)
        ).fetchone()["proposed_slots"]
    slots = json.loads(raw)
    assert slots, "no slots staged"

    today = date.today()
    next_monday = today + timedelta(days=(7 - today.weekday()))
    declined_week_end = next_monday + timedelta(days=6)  # the asked "next week"
    for slot in slots:
        slot_day = datetime.fromisoformat(slot["start"]).date()
        assert slot_day > declined_week_end, (
            f"slot {slot['start']} sits in the declined week "
            f"(week of {next_monday}) — the live 10563 re-offer bug"
        )
