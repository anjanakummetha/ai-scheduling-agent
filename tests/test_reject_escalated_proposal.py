"""Escalated proposals must be rejectable from chat (live defect D5).

"Just drop it" after an escalation had no path: needs_kory/-guidance
statuses were outside the rejectable set, so closing one meant DB surgery
(proposal 7999 in RUN 15; the 08-06 cleanup hit the same wall on #7041).
"""

from __future__ import annotations

import pytest

from app.agents.comms_agent import execute_lexi_approval
from app.scheduling.proposal_state import transition
from app.storage.lexi_db import get_lexi_connection


@pytest.fixture
def escalated_proposal():
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO email_threads (thread_id, subject, sender_email)"
            " VALUES ('test-d5-thread', '[TEST] D5 escalated', 'anjanakummetha@gmail.com')"
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification)"
            " VALUES ('test-d5-thread', 'needs_kory', 'meeting')"
        )
        pid = cur.lastrowid
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = 'test-d5-thread'")
        conn.commit()


def test_needs_kory_proposal_can_be_rejected(escalated_proposal):
    result = execute_lexi_approval(
        escalated_proposal,
        "rejected",
        "",
        "kory",
        decision_source="hermes_teams_text",
        modification_notes="[TEST] just drop it",
    )
    assert result.ok is True, result.errors
    assert result.status == "rejected"
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?", (escalated_proposal,)
        ).fetchone()
        assert row["status"] == "rejected"
        decision = conn.execute(
            "SELECT decision, decision_source FROM approvals WHERE proposal_id = ?",
            (escalated_proposal,),
        ).fetchone()
        assert decision["decision"] == "rejected"


def test_needs_guidance_proposal_can_be_rejected(escalated_proposal):
    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET status='needs_scheduling_guidance' WHERE id = ?",
            (escalated_proposal,),
        )
        conn.commit()
    result = execute_lexi_approval(
        escalated_proposal, "rejected", "", "kory",
        decision_source="hermes_teams_text",
    )
    assert result.ok is True, result.errors


def test_terminal_statuses_still_refuse_rejection(escalated_proposal):
    # Reach `cancelled` the way the product does — book the meeting, then cancel
    # it. Jumping straight there with an UPDATE is exactly the kind of write the
    # state-machine guard now refuses, and a fixture that cannot happen in
    # production proves nothing about production.
    with get_lexi_connection() as conn:
        for status in ("executed", "cancelled"):
            transition(
                conn,
                escalated_proposal,
                to=status,
                reason="Test fixture: book then cancel to reach a terminal status.",
                actor="test",
            )
        conn.commit()
    result = execute_lexi_approval(
        escalated_proposal, "rejected", "", "kory",
        decision_source="hermes_teams_text",
    )
    assert result.ok is False
    assert any("cannot be rejected" in e for e in result.errors)
