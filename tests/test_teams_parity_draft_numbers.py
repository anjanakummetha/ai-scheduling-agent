"""The draft-number fix, driven through the Teams gateway's own entry point.

tests/teams_parity.py routes through mcp._tool_manager.call_tool, so what passes
here is what happens when Kory types the same words in Teams.
"""

from __future__ import annotations

import pytest

from app.scheduling.proposal_state import ProposalStatus, transition
from app.storage.lexi_db import get_lexi_connection
from tests.teams_parity import message_of, teams

PREFIX = "parity-draftnum"
PEOPLE = ("Dana", "Rob", "Priya")


@pytest.fixture
def three_drafts():
    ids: list[int] = []
    with get_lexi_connection() as conn:
        for who in PEOPLE:
            thread = f"{PREFIX}-{who}"
            conn.execute(
                "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
                " sender_email, raw_body) VALUES (?,?,?,?,?)",
                (thread, f"[TEST] {who} intro", f"{who} <{who.lower()}@example.com>",
                 f"{who.lower()}@example.com", "can we meet?"),
            )
            cur = conn.execute(
                "INSERT INTO proposals (thread_id, status, intent_classification,"
                " drafted_reply) VALUES (?,?,?,?)",
                (thread, ProposalStatus.PENDING_APPROVAL, "referral_or_intro",
                 f"Hi {who},\n\nHappy to find a time.\n\nLet's Win,\nKory"),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    yield dict(zip(PEOPLE, ids))
    with get_lexi_connection() as conn:
        for pid in ids:
            conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (pid,))
            conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id LIKE ?", (f"{PREFIX}-%",))
        conn.execute("DELETE FROM teams_list_snapshots")
        conn.commit()


def _status(pid: int) -> str:
    with get_lexi_connection() as conn:
        return str(conn.execute(
            "SELECT status FROM proposals WHERE id = ?", (pid,)
        ).fetchone()["status"])


def test_typing_approve_draft_1_after_the_queue_shifts_refuses_by_name(three_drafts):
    """The wrong-send, end to end, in Kory's own words."""
    listing = message_of(teams("pending"))
    assert "Dana" in listing and "draft 1" in listing, listing

    # Dana's draft clears while he is reading — a card tap, an auto-execute, an
    # expiry. Everything below it shifts up.
    with get_lexi_connection() as conn:
        transition(
            conn, three_drafts["Dana"], to=ProposalStatus.OFFER_SENT,
            reason="Approved from an Adaptive Card while the list was on screen.",
        )
        conn.commit()

    reply = message_of(teams("approve draft 1"))

    assert _status(three_drafts["Rob"]) == ProposalStatus.PENDING_APPROVAL, (
        "Rob's offer was sent because the number shifted under Kory"
    )
    assert "Dana" in reply, reply
    assert "no longer waiting to send" in reply, reply
    assert "pending" in reply.lower(), "the reply must say how to recover"


def test_reject_is_protected_the_same_way(three_drafts):
    teams("pending")
    with get_lexi_connection() as conn:
        transition(conn, three_drafts["Dana"], to=ProposalStatus.REJECTED,
                   reason="Kory rejected it from a card.")
        conn.commit()

    reply = message_of(teams("reject draft 1 — not a fit"))
    assert _status(three_drafts["Rob"]) == ProposalStatus.PENDING_APPROVAL, (
        "Rob's draft was rejected in Dana's place"
    )
    assert "Dana" in reply, reply


def test_an_unchanged_queue_still_approves_the_draft_he_named(three_drafts):
    """The guard must not get in the way of the normal path."""
    teams("pending")
    reply = message_of(teams("show draft 2"))
    assert "Rob" in reply, reply
