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
