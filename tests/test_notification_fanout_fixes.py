"""Fixes for the 2026-08-13/14 notification defects: one proposal = one card,
word-boundary previews, org names never rendered as people."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import app.agents.lexi_thread_followup as ltf
import app.jobs.hold_lifecycle as hl


# --- release collapse -------------------------------------------------------


def test_release_notify_is_one_message_listing_all_slots():
    row = {
        "subject": "Check in",
        "sender": "heidi.heckler@iconicfounders.com",
        "proposal_id": 9187,
        "intent_classification": "internal_sync",
    }
    slots = [
        "2026-08-24T10:00:00-06:00",
        "2026-08-26T11:00:00-06:00",
        "2026-08-28T09:00:00-06:00",
    ]
    with patch("app.safety.outbound_guard.teams_push_allowed", return_value=True):
        with patch("app.bot.teams_publisher.push_approval_text_to_teams") as mock_push:
            hl._maybe_notify_holds_released(row, slots)
    assert mock_push.call_count == 1
    text = mock_push.call_args[0][0]
    assert "3 held slots" in text
    assert text.count("August 24") == 1
    assert "August 26" in text and "August 28" in text
    assert "retry scheduling #9187" in text


def test_release_closes_stale_pending_hold_reminder():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY, thread_id TEXT, status TEXT,
            intent_classification TEXT, scheduling_note TEXT, updated_at TEXT);
        CREATE TABLE holds (
            id INTEGER PRIMARY KEY, proposal_id INTEGER, event_id TEXT,
            slot_start TEXT, expires_at TEXT);
        CREATE TABLE email_threads (thread_id TEXT, subject TEXT, sender TEXT);
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, step_name TEXT,
            reference_id TEXT, log_level TEXT, message TEXT, payload TEXT,
            timestamp TEXT DEFAULT (datetime('now')));
        INSERT INTO email_threads VALUES ('t1', 'Check in', 'heidi.heckler@iconicfounders.com');
        INSERT INTO proposals VALUES
            (9187, 't1', 'pending_approval', 'internal_sync', 'HOLD_REMINDER: staged', '');
        INSERT INTO holds VALUES
            (1, 9187, 'evt-a', '2026-08-24T10:00:00-06:00', '2026-08-01T00:00:00+00:00'),
            (2, 9187, 'evt-b', '2026-08-26T11:00:00-06:00', '2026-08-01T00:00:00+00:00');
        """
    )
    conn.commit()
    with (
        patch.object(hl, "get_lexi_connection", return_value=conn),
        patch.object(hl, "delete_calendar_event"),
        patch.object(hl, "_maybe_notify_holds_released") as notify,
        patch.object(hl.kory_rules, "HOLD_RULES", {"re_remind_on_release": False}),
    ):
        released = hl._release_expired_holds()
    assert released == 2
    status = conn.execute("SELECT status FROM proposals WHERE id=9187").fetchone()[0]
    assert status == "rejected"
    # One notify for the proposal, carrying both slots.
    assert notify.call_count == 1
    assert len(notify.call_args[0][1]) == 2


# --- follow-up ping cooldown ------------------------------------------------


def test_thread_update_pings_collapse_within_cooldown():
    ltf._last_fyi_ping.clear()
    with (
        patch.object(ltf, "settings") as mock_settings,
        patch("app.bot.teams_publisher.schedule_teams_scheduling_guidance_push") as push,
    ):
        mock_settings.lexi_teams_enabled = True
        ltf._notify_kory_followup(3395, summary="one", kind="thread_update")
        ltf._notify_kory_followup(3395, summary="two", kind="thread_update")
        ltf._notify_kory_followup(3395, summary="three", kind="thread_update")
    assert push.call_count == 1


def test_decision_pings_are_never_suppressed():
    ltf._last_fyi_ping.clear()
    with (
        patch.object(ltf, "settings") as mock_settings,
        patch("app.bot.teams_publisher.schedule_teams_scheduling_guidance_push") as push,
    ):
        mock_settings.lexi_teams_enabled = True
        ltf._notify_kory_followup(3395, summary="cancel!", kind="cancel_request")
        ltf._notify_kory_followup(3395, summary="cancel again", kind="cancel_request")
    assert push.call_count == 2


def test_different_threads_do_not_share_cooldown():
    ltf._last_fyi_ping.clear()
    with (
        patch.object(ltf, "settings") as mock_settings,
        patch("app.bot.teams_publisher.schedule_teams_scheduling_guidance_push") as push,
    ):
        mock_settings.lexi_teams_enabled = True
        ltf._notify_kory_followup(1, summary="a", kind="thread_update")
        ltf._notify_kory_followup(2, summary="b", kind="thread_update")
    assert push.call_count == 2


# --- preview truncation -----------------------------------------------------


def test_preview_cuts_on_word_boundary_with_ellipsis():
    body = (
        "Here is what Copilot said about their ownership and revenue.   "
        "Hopefully, this is a fruitful conversation for you.   I am sure the "
        "two of you will have plenty to discuss about the acquisition."
    )
    preview = ltf._body_preview(body)
    assert preview.endswith("…")
    assert not preview.rstrip("…").endswith(" ")
    # Never a mid-word chop like "I a"
    last_word = preview.rstrip("…").split()[-1]
    assert last_word in body


def test_preview_joins_hard_wrapped_lines():
    body = "Here is what Copilot said\nabout their ownership and revenue.\n"
    assert ltf._body_preview(body) == (
        "Here is what Copilot said about their ownership and revenue."
    )


def test_preview_still_skips_greeting():
    assert ltf._body_preview("Hi Lexi,\nCan we do Tuesday?") == "Can we do Tuesday?"


# --- org names are not people ----------------------------------------------


def test_signature_miner_rejects_company_footer():
    from app.integrations.outlook_email import _clean_name_candidate

    assert _clean_name_candidate("Iconic Founders Group") is None
    assert _clean_name_candidate("H & F Exteriors") is None
    assert _clean_name_candidate("Heidi Heckler") == "Heidi Heckler"


def test_polluted_profile_row_is_not_rendered(monkeypatch):
    from app.storage import recipient_profiles as rp

    monkeypatch.setattr(
        rp,
        "get_recipient_profile",
        lambda email: {"display_name": "Iconic Founders Group"},
    )
    assert rp.display_name_for_email("heidi.heckler@iconicfounders.com") == (
        "Heidi Heckler"
    )
