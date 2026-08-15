"""Replay the Teams guidance journey through the REAL engine.

The historical failure mode was never the engine alone — it was the chain:
model tool-call → status gate → guidance persistence → plan merge → engine →
validators → staged offer. It was 'severed in SEVEN independent places' at one
point (live I-2), each severed link invisible to engine-only unit tests. These
tests drive retry_scheduling_with_guidance exactly as the MCP tool does, with
only the calendar read and Teams push stubbed, and assert the staged result
obeys Kory's words end to end.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.inbound_reply import retry_scheduling_with_guidance
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")

THREAD_ID = "test-guidance-replay-thread"
SENDER = "anjanakummetha@gmail.com"
BODY = (
    "Hi Kory, would love to find 30 minutes to connect next week. "
    "Happy to work around your schedule."
)


def _next_monday(after_weeks: int = 0) -> date:
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return monday + timedelta(weeks=after_weeks)


@pytest.fixture
def escalated_proposal():
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender_email, raw_body)"
            " VALUES (?, '[TEST] Intro call', ?, ?)",
            (THREAD_ID, SENDER, BODY),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification)"
            " VALUES (?, 'needs_kory', 'referral_or_intro')",
            (THREAD_ID,),
        )
        pid = cur.lastrowid
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD_ID,))
        conn.commit()


def _fake_calendar(**_kwargs):
    return {"status": "available", "horizon_days": 45, "busy_events": []}


def _run_retry(pid: int, guidance: str):
    with (
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            side_effect=_fake_calendar,
        ),
        patch(
            "app.agents.scheduler_agent.load_scheduling_calendar_context",
            side_effect=_fake_calendar,
            create=True,
        ),
        patch("app.bot.teams_publisher.schedule_teams_approval_push"),
    ):
        return retry_scheduling_with_guidance(pid, guidance)


def _staged(pid: int) -> dict:
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, proposed_slots, drafted_reply, kory_scheduling_guidance"
            " FROM proposals WHERE id = ?",
            (pid,),
        ).fetchone()
    slots = json.loads(row["proposed_slots"]) if row["proposed_slots"] else []
    return {
        "status": row["status"],
        "slots": slots,
        "draft": row["drafted_reply"] or "",
        "guidance": row["kory_scheduling_guidance"] or "",
    }


def test_week_after_guidance_lands_in_that_week(escalated_proposal):
    """The user-story journey: 'they asked for next week, you're booked —
    offer the week of <X> instead' must stage slots in exactly that week."""
    target = _next_monday(after_weeks=1)
    guidance = f"offer the week of {target.strftime('%B')} {target.day} instead"
    result = _run_retry(escalated_proposal, guidance)
    assert result.get("ok"), result
    staged = _staged(escalated_proposal)
    assert staged["status"] == "pending_approval"
    assert staged["guidance"] == guidance
    assert staged["slots"], "no slots staged"
    week_end = target + timedelta(days=6)
    for slot in staged["slots"]:
        d = datetime.fromisoformat(slot["start"]).astimezone(MT).date()
        assert target <= d <= week_end, (slot, target)
    assert staged["draft"], "no draft staged"
    # The draft must offer the staged days — not compose its own.
    for slot in staged["slots"]:
        day = datetime.fromisoformat(slot["start"]).astimezone(MT).strftime("%A")
        assert day in staged["draft"], (day, staged["draft"][:400])


def test_multi_change_guidance_survives_the_whole_chain(escalated_proposal):
    result = _run_retry(
        escalated_proposal,
        "Tuesday or Wednesday only, make it 45 minutes, afternoon",
    )
    assert result.get("ok"), result
    staged = _staged(escalated_proposal)
    assert staged["status"] == "pending_approval"
    assert staged["slots"]
    for slot in staged["slots"]:
        start = datetime.fromisoformat(slot["start"]).astimezone(MT)
        end = datetime.fromisoformat(slot["end"]).astimezone(MT)
        assert start.strftime("%A") in {"Tuesday", "Wednesday"}, slot
        assert (end - start) == timedelta(minutes=45), slot
        assert start.hour >= 12, slot


def test_retry_refused_after_offer_already_sent(escalated_proposal):
    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET status = 'offer_sent' WHERE id = ?",
            (escalated_proposal,),
        )
        conn.commit()
    result = _run_retry(escalated_proposal, "try Friday")
    assert not result.get("ok")
    assert "offer_sent" in str(result.get("error", ""))


def test_remembered_day_rule_shapes_the_retry(escalated_proposal):
    """A rule saved via `remember` must constrain the very next engine run."""
    from app.storage.kory_memory import delete_fact, upsert_fact

    upsert_fact(
        fact_key="email:test-day-rule",
        fact_value="no meetings on Fridays",
        source="test",
    )
    try:
        result = _run_retry(escalated_proposal, "sometime next week works")
        assert result.get("ok"), result
        staged = _staged(escalated_proposal)
        for slot in staged["slots"]:
            day = datetime.fromisoformat(slot["start"]).astimezone(MT).strftime("%A")
            assert day != "Friday", slot
    finally:
        delete_fact(fact="email:test-day-rule")
