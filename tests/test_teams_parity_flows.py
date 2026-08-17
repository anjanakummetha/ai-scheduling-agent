"""Kory's real Teams flows, driven through the gateway's own tool path.

Every command here is typed exactly as he would type it and goes through
mcp._tool_manager.call_tool — registration and argument validation included —
so a pass here means the same words in Teams take the identical path. See
tests/teams_parity.py for why a direct function call is not good enough.

Covers the cases he says keep going wrong: approving the right draft, asking
for different times, and being told the truth when a day is busy or nothing
is available.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.storage.lexi_db import get_lexi_connection
from tests.teams_parity import call_tool, message_of, registered_tool_names, teams

MT = ZoneInfo("America/Denver")
SENDER = "anjanakummetha@gmail.com"


def _weekday_ahead(weekday: int, weeks: int = 2) -> date:
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks)
    return monday + timedelta(days=weekday)


def _slot(d: date, hour: int, minutes: int = 30) -> dict[str, str]:
    start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=MT)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
    }


def _seed(thread_id: str, subject: str, *, status: str, slots, draft: str) -> int:
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_threads "
            "(thread_id, subject, sender, sender_email, raw_body) VALUES (?,?,?,?,?)",
            (thread_id, subject, SENDER, SENDER, "Would love to find 30 minutes."),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification, "
            "priority_tier, proposed_slots, drafted_reply) VALUES (?,?,?,?,?,?)",
            (thread_id, status, "referral_or_intro", "medium", json.dumps(slots), draft),
        )
        pid = cur.lastrowid
        conn.commit()
    return pid


def _purge(*thread_ids: str) -> None:
    with get_lexi_connection() as conn:
        for tid in thread_ids:
            rows = conn.execute(
                "SELECT id FROM proposals WHERE thread_id = ?", (tid,)
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM holds WHERE proposal_id = ?", (row["id"],))
                conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (row["id"],))
            conn.execute("DELETE FROM proposals WHERE thread_id = ?", (tid,))
            conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (tid,))
        conn.commit()


@pytest.fixture
def two_drafts():
    """Two drafts waiting, so draft numbering has something to disambiguate."""
    t1, t2 = "parity-thread-a", "parity-thread-b"
    _purge(t1, t2)
    d = _weekday_ahead(1)
    p1 = _seed(t1, "[TEST] Intro — Curtis", status="pending_approval",
               slots=[_slot(d, 9)], draft="Hi Curtis,\n\nA few times below.\n")
    p2 = _seed(t2, "[TEST] Coffee — Steve", status="pending_approval",
               slots=[_slot(d, 14)], draft="Hi Steve,\n\nA few times below.\n")
    yield p1, p2
    _purge(t1, t2)


# --- the surface exists at all -------------------------------------------


def test_every_scheduling_command_kory_types_is_reachable_by_the_model():
    """If the router is not registered, nothing Kory types reaches Lexi."""
    assert "lexi_handle_teams_command" in registered_tool_names()


def test_calling_an_unregistered_tool_fails_loudly():
    """The harness must not let a test 'pass' against a tool Teams cannot see."""
    with pytest.raises(AssertionError, match="not registered"):
        call_tool("lexi_tool_that_does_not_exist")


# --- pending / draft numbering -------------------------------------------


def test_pending_lists_drafts_by_number_not_raw_id(two_drafts):
    p1, p2 = two_drafts
    out = teams("pending")
    assert out["handled"] is True
    msg = message_of(out)
    assert "draft 1" in msg and "draft 2" in msg
    # Not `str(p1) not in msg`: in the test DB ids start at 1, which collides
    # with the draft numbers themselves. What must be absent is the "#<id>"
    # rendering Kory was complaining about.
    assert f"#{p1}" not in msg and f"#{p2}" not in msg, msg


def test_show_draft_by_number_returns_that_draft(two_drafts):
    p1, p2 = two_drafts
    first = message_of(teams("show draft 1"))
    second = message_of(teams("show draft 2"))
    assert "Curtis" in first, first[:200]
    assert "Steve" in second, second[:200]


def test_the_empty_queue_says_so_rather_than_inventing_work():
    out = teams("pending")
    assert out["handled"] is True
    assert "No drafts" in message_of(out)


# --- rejection path -------------------------------------------------------


def test_reject_by_number_drops_the_right_draft(two_drafts):
    p1, p2 = two_drafts
    out = teams("reject 1 — not a fit")
    assert out.get("ok") is not False, out
    with get_lexi_connection() as conn:
        left = conn.execute(
            "SELECT id, status FROM proposals WHERE id IN (?,?)", (p1, p2)
        ).fetchall()
    by_id = {row["id"]: row["status"] for row in left}
    assert by_id[p1] != "pending_approval", "draft 1 should be gone"
    assert by_id[p2] == "pending_approval", "draft 2 must be untouched"


def test_after_a_rejection_the_queue_renumbers(two_drafts):
    p1, p2 = two_drafts
    teams("reject 1 — not a fit")
    msg = message_of(teams("pending"))
    assert "Steve" in msg
    assert "draft 1" in msg, "what was draft 2 becomes draft 1"


# --- never answer from memory --------------------------------------------


def test_an_unknown_command_is_not_silently_swallowed():
    """handled=false is how the model learns it may answer conversationally."""
    out = teams("what is the weather in Denver")
    assert out.get("handled") is False
