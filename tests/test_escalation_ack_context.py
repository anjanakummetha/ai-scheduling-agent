"""Bare "YES" replies to escalations must resolve to something actionable (D6).

Escalations are proactive pushes, so Kory's reply arrives without the
escalation in the agent's context — a bare YES got "what would you like to
approve?". Two-part fix: escalation copy always ends with #N-anchored reply
options, and the command router answers bare confirmations by naming the
open escalation instead of shrugging.
"""

from __future__ import annotations

from app.bot.teams_text import parse_teams_command
from app.scheduling.kory_escalation import _with_actionable_footer
from app.storage.lexi_db import get_lexi_connection
from app.teams.commands import handle_teams_command


def test_footer_added_when_summary_lacks_proposal_number():
    out = _with_actionable_footer("Mornings are packed that week.", 4242)
    assert "reject #4242" in out
    assert "guidance" in out


def test_footer_not_duplicated_when_number_present():
    out = _with_actionable_footer("Say reject #4242 to drop it.", 4242)
    assert out.count("#4242") == 1


def test_bare_confirmations_parse_as_ack():
    for text in ("YES", "yes", "ok", "Okay, close it", "yep", "sounds good"):
        cmd = parse_teams_command(text)
        assert cmd and cmd["action"] == "bare_ack", text


def test_confirmations_with_numbers_or_content_do_not_match_ack():
    assert (parse_teams_command("approve #123") or {}).get("action") != "bare_ack"
    assert parse_teams_command("yes try next week") is None  # conversational — Hermes handles


import pytest


@pytest.fixture
def quiet_escalations():
    """Park any pre-existing escalated rows so tests see only their own."""
    with get_lexi_connection() as conn:
        rows = [r["id"] for r in conn.execute(
            "SELECT id FROM proposals WHERE status IN ('needs_kory', 'needs_scheduling_guidance')"
        ).fetchall()]
        conn.execute(
            "UPDATE proposals SET status='x_d6_parked' WHERE status IN ('needs_kory', 'needs_scheduling_guidance')"
        )
        conn.commit()
    yield
    with get_lexi_connection() as conn:
        for rid in rows:
            conn.execute("UPDATE proposals SET status='needs_kory' WHERE id=? AND status='x_d6_parked'", (rid,))
        conn.execute("UPDATE proposals SET status='needs_kory' WHERE status='x_d6_parked'")
        conn.commit()


def _make_escalated(subject):
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO email_threads (thread_id, subject, sender_email)"
            " VALUES (?, ?, 'anjanakummetha@gmail.com')",
            (f"test-d6-{subject}", subject),
        )
        pid = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification)"
            " VALUES (?, 'needs_kory', 'meeting')",
            (f"test-d6-{subject}",),
        ).lastrowid
        conn.commit()
    return pid


def _cleanup(pids, subjects):
    with get_lexi_connection() as conn:
        for pid in pids:
            conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        for s in subjects:
            conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (f"test-d6-{s}",))
        conn.commit()


def test_bare_yes_names_the_single_open_escalation(quiet_escalations):
    pid = _make_escalated("[TEST] D6 single")
    try:
        out = handle_teams_command("YES")
        assert out["handled"] is True
        assert f"#{pid}" in out["message"]
        assert f"reject #{pid}" in out["message"]
    finally:
        _cleanup([pid], ["[TEST] D6 single"])


def test_bare_yes_lists_when_multiple_escalations(quiet_escalations):
    p1 = _make_escalated("[TEST] D6 first")
    p2 = _make_escalated("[TEST] D6 second")
    try:
        out = handle_teams_command("ok")
        assert out["handled"] is True
        assert f"#{p1}" in out["message"] and f"#{p2}" in out["message"]
        assert "Which one" in out["message"]
    finally:
        _cleanup([p1, p2], ["[TEST] D6 first", "[TEST] D6 second"])


def test_bare_yes_with_no_escalations_stays_conversational(quiet_escalations):
    out = handle_teams_command("yes")
    assert out["handled"] is False
