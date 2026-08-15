"""Replay Kory's real delegation move end-to-end below the model.

The primary live flow: a prospect emails Kory, Kory replies "Looping in Lexi
to find us a time" with lexi@ CC'd. The reply lands in ingestion; the pipeline
must (1) detect the delegation, (2) point the thread at the OUTSIDE
counterpart — not Kory, not Lexi, (3) stage a Lexi-voice offer whose slots
came from the engine, and (4) end pending_approval so Kory gets the card.
Only the calendar read, the LLM, and the Teams push are stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.orchestrator import handle_inbound_stream
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD_ID = "test-delegation-replay-msg-1"
CONVERSATION_ID = "test-delegation-replay-conv"


def _kory_reply_email() -> dict:
    return {
        "message_id": THREAD_ID,
        "thread_id": THREAD_ID,
        "conversation_id": CONVERSATION_ID,
        "subject": "RE: [TEST] Intro: Anjana (Acme) <> Kory Mitchell",
        "sender": "kory.mitchell@iconicfounders.com",
        "sender_email": "kory.mitchell@iconicfounders.com",
        "raw_body": (
            "Anjana — great to connect. Looping in my assistant Lexi — "
            "she'll help us find time for a 30-minute intro next week.\n\n"
            "Let's Win,\nKory"
        ),
        "body": (
            "Anjana — great to connect. Looping in my assistant Lexi — "
            "she'll help us find time for a 30-minute intro next week."
        ),
        "to_recipients": [
            {"emailAddress": {"address": "anjanakummetha@gmail.com", "name": "Anjana Kummetha"}},
        ],
        "cc_recipients": [
            {"emailAddress": {"address": "lexi@iconicfounders.com", "name": "Lexi Knightly"}},
        ],
        "received_at": datetime.now(tz=MT).isoformat(),
    }


@pytest.fixture
def clean_thread():
    yield
    with get_lexi_connection() as conn:
        conn.execute(
            "DELETE FROM holds WHERE proposal_id IN "
            "(SELECT id FROM proposals WHERE thread_id = ?)",
            (THREAD_ID,),
        )
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD_ID,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD_ID,))
        conn.commit()


def _fake_calendar(**_kwargs):
    return {"status": "available", "horizon_days": 45, "busy_events": []}


def test_kory_delegation_reply_stages_lexi_offer(clean_thread, monkeypatch):
    monkeypatch.setenv("LEXI_MAILBOX_EMAIL", "lexi@iconicfounders.com")
    with (
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            side_effect=_fake_calendar,
        ),
        patch("app.bot.teams_publisher.schedule_teams_approval_push"),
        patch("app.bot.teams_publisher.schedule_teams_reply_prompt_push"),
    ):
        result = handle_inbound_stream(_kory_reply_email())

    assert result, "orchestrator returned nothing"

    with get_lexi_connection() as conn:
        prop = conn.execute(
            "SELECT p.*, e.sender_email AS thread_sender FROM proposals p "
            "JOIN email_threads e ON e.thread_id = p.thread_id "
            "WHERE p.thread_id = ?",
            (THREAD_ID,),
        ).fetchone()

    assert prop is not None, f"no proposal created: {result}"
    prop = dict(prop)

    # (1) recognized as delegation, in Lexi's voice on the Lexi channel
    assert prop["is_delegation"] == 1
    assert prop["voice_mode"] == "lexi"
    assert prop["send_channel"] == "lexi"

    # (2) the thread now points at the counterpart — the greeting target
    assert prop["thread_sender"] == "anjanakummetha@gmail.com"

    # (3) staged offer: engine slots + a draft that offers those exact days
    assert prop["status"] == "pending_approval", (prop["status"], result)
    slots = json.loads(prop["proposed_slots"] or "[]")
    assert len(slots) >= 2, "delegation offer needs the 2-option pattern"
    draft = prop["drafted_reply"] or ""
    assert draft
    for slot in slots:
        day = datetime.fromisoformat(slot["start"]).astimezone(MT).strftime("%A")
        assert day in draft, (day, draft[:400])
    # Slots honor the sender's stated "next week" or later — never today.
    today = datetime.now(tz=MT).date()
    for slot in slots:
        assert datetime.fromisoformat(slot["start"]).astimezone(MT).date() > today

    # (4) Lexi voice, not Kory's sign-off
    assert "Let's Win" not in draft


def test_second_ingestion_of_same_thread_does_not_duplicate(clean_thread, monkeypatch):
    monkeypatch.setenv("LEXI_MAILBOX_EMAIL", "lexi@iconicfounders.com")
    with (
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            side_effect=_fake_calendar,
        ),
        patch("app.bot.teams_publisher.schedule_teams_approval_push"),
        patch("app.bot.teams_publisher.schedule_teams_reply_prompt_push"),
    ):
        handle_inbound_stream(_kory_reply_email())
        handle_inbound_stream(_kory_reply_email())  # Sent Items + Lexi inbox copy

    with get_lexi_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE thread_id = ?", (THREAD_ID,)
        ).fetchone()["n"]
    assert count == 1, f"duplicate proposals for one delegated thread: {count}"
