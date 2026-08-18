"""When the state machine moves, does Kory actually hear about it?

Every notification path queries the proposal back and filters on the status it
expects — the invite prompt wants pending_invite, the re-offer prompt wants
pending_reoffer, and so on — and returns silently if it does not find it. So a
transition and its notification are coupled, and nothing checked that coupling.

That is a bad failure to have: the counterpart replies, Lexi records it
correctly, and Kory is told nothing. The thread then sits until the 48-hour
stuck sweeper notices, which is a long time to look broken.

This session changed those transitions. These tests drive the REAL transition
and assert the matching notification produces something addressed to Kory, with
only the network send mocked out — the queries, the status filters and the card
building all run for real.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import mark_recipient_reoffer_request, mark_recipient_slot_choice
from app.scheduling.proposal_state import ProposalStatus, record_fact
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "notify-thread"
SUBJECT = "[TEST] Intro call?"
SENDER = "Dana Reyes <dana@example.com>"


def _slot() -> dict[str, str]:
    day = date.today() + timedelta(days=12)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return {
        "start": datetime(day.year, day.month, day.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(day.year, day.month, day.day, 9, 30, tzinfo=MT).isoformat(),
    }


@contextmanager
def _teams_on():
    """conftest suppresses Teams pushes for the whole suite, for good reason.
    These tests are specifically about the push, so they turn it back on and
    stop at the network boundary instead."""
    from app.config import settings

    previous = settings.lexi_teams_enabled
    object.__setattr__(settings, "lexi_teams_enabled", True)
    try:
        with patch.dict("os.environ", {"LEXI_SUPPRESS_TEAMS_PUSH": "false",
                                       "LEXI_FORCE_TEAMS_PUSH": "true"}):
            yield
    finally:
        object.__setattr__(settings, "lexi_teams_enabled", previous)


@contextmanager
def _capture_pushes():
    """Everything runs for real up to the ConnectorClient call."""
    sent: list[str] = []
    cards: list[object] = []

    async def _text(text, *, proposal_id=""):
        sent.append(str(text))

    async def _card(proposal, card):
        cards.append(card)

    with ExitStack() as stack:
        stack.enter_context(_teams_on())
        stack.enter_context(
            patch("app.bot.teams_publisher.push_approval_text_to_teams", side_effect=_text)
        )
        stack.enter_context(
            patch("app.bot.teams_publisher.push_approval_card_to_teams", side_effect=_card)
        )
        yield sent, cards


@pytest.fixture
def offer_sent():
    slot = _slot()
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, conversation_id, subject,"
            " sender, sender_email, raw_body) VALUES (?,?,?,?,?,?)",
            (THREAD, THREAD, SUBJECT, SENDER, "dana@example.com", "can we meet?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots, drafted_reply, recipient_timezone) VALUES (?,?,?,?,?,?)",
            (THREAD, ProposalStatus.OFFER_SENT, "referral_or_intro",
             json.dumps([slot]), "Hi Dana,\n\nA time.\n\nLet's Win,\nKory", "America/Denver"),
        )
        pid = int(cur.lastrowid)
        record_fact(conn, pid, "offer_sent_at")
        conn.execute(
            "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "evt-hold", slot["start"], slot["end"],
             (datetime.now(MT) + timedelta(days=2)).isoformat()),
        )
        conn.commit()
    yield pid, slot
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def test_kory_is_told_when_the_counterpart_picks_a_time(offer_sent):
    """pending_invite. The one notification that unblocks a booking."""
    from app.bot.teams_publisher import push_invite_prompt_for_proposal_id

    pid, slot = offer_sent
    assert mark_recipient_slot_choice(pid, slot, reply_body="Tuesday works").get("ok")

    with _capture_pushes() as (sent, cards):
        asyncio.run(push_invite_prompt_for_proposal_id(pid))

    assert sent, (
        "the counterpart picked a time and Kory was told nothing — the thread "
        "would sit until the 48-hour stuck sweeper noticed"
    )
    blob = " ".join(sent)
    assert "Dana" in blob, blob
    assert str(pid) in blob or "invite" in blob.lower(), blob


def test_kory_is_told_when_the_counterpart_declines(offer_sent):
    """pending_reoffer. Without this nobody knows new times are needed."""
    from app.bot.teams_publisher import push_reoffer_prompt_for_proposal_id

    pid, _slot = offer_sent
    with patch("app.agents.comms_agent.delete_calendar_event"):
        assert mark_recipient_reoffer_request(pid, reply_body="None of those work.").get("ok")

    with _capture_pushes() as (sent, cards):
        asyncio.run(push_reoffer_prompt_for_proposal_id(pid, reply_body="None of those work."))

    assert sent, "the counterpart declined and Kory was told nothing"
    blob = " ".join(sent)
    assert "don't work" in blob.lower() or "more times" in blob.lower(), blob


def test_the_notification_matches_the_state_the_machine_left_it_in(offer_sent):
    """The coupling itself: each push filters on the status its transition sets.

    If a transition ever moves somewhere the notification does not expect, the
    push returns silently. This asserts the pairing rather than trusting it.
    """
    from app.bot.teams_publisher import (
        push_invite_prompt_for_proposal_id,
        push_reoffer_prompt_for_proposal_id,
    )

    pid, slot = offer_sent

    # Wrong pairing: the proposal is in pending_invite, so the RE-OFFER prompt
    # must find nothing — proving the filters are real and not incidental.
    mark_recipient_slot_choice(pid, slot, reply_body="Tuesday works")
    with _capture_pushes() as (sent, _cards):
        asyncio.run(push_reoffer_prompt_for_proposal_id(pid))
    assert not sent, "the re-offer prompt fired for a proposal that was accepted"

    # Right pairing.
    with _capture_pushes() as (sent, _cards):
        asyncio.run(push_invite_prompt_for_proposal_id(pid))
    assert sent


def test_a_push_never_raises_into_the_caller(offer_sent):
    """These are fired from inside transitions. A raise here would roll back a
    state change that already matched reality."""
    from app.bot.teams_publisher import (
        schedule_teams_invite_prompt_push,
        schedule_teams_reoffer_prompt_push,
    )

    pid, slot = offer_sent
    mark_recipient_slot_choice(pid, slot, reply_body="Tuesday works")

    with _teams_on(), patch(
        "app.bot.teams_publisher.push_approval_text_to_teams",
        side_effect=RuntimeError("Teams is down"),
    ):
        schedule_teams_invite_prompt_push(pid)      # must not raise
        schedule_teams_reoffer_prompt_push(pid)     # must not raise


def test_a_proposal_that_moved_on_does_not_get_a_stale_card(offer_sent):
    """Kory taps nothing on a card for a thread that has already progressed."""
    from app.bot.teams_publisher import push_invite_prompt_for_proposal_id
    from app.scheduling.proposal_state import transition

    pid, slot = offer_sent
    mark_recipient_slot_choice(pid, slot, reply_body="Tuesday works")
    with get_lexi_connection() as conn:
        transition(conn, pid, to=ProposalStatus.EXECUTED, reason="Booked before the push ran.")
        conn.commit()

    with _capture_pushes() as (sent, _cards):
        asyncio.run(push_invite_prompt_for_proposal_id(pid))
    assert not sent, "an invite prompt was pushed for a meeting already booked"
