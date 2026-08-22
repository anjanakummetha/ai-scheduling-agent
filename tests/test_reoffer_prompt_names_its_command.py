"""Every proactive prompt must name the exact reply that acts on it.

Proactive pushes are posted straight to Teams and never enter the gateway
model's session. When the re-offer prompt asked "send more times?" without
naming a command, Kory-side "yes" reached the model with no context at all —
it replied "Got it — let me know what you need!" and nothing happened (live
proposal 10563, 2026-08-22). A prompt that invites a bare yes/no invites
exactly the reply the model cannot act on; the prompt must carry its command.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import app.bot.teams_publisher as publisher
from app.scheduling.proposal_state import ProposalStatus
from app.storage.lexi_db import get_lexi_connection

THREAD = "reoffer-prompt-thread"


@pytest.fixture
def reoffer_proposal():
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] Coffee next week", "Dana <dana@example.com>",
             "dana@example.com", "Neither works — the following week?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification)"
            " VALUES (?,?,?)",
            (THREAD, ProposalStatus.PENDING_REOFFER, "referral_or_intro"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def test_the_reoffer_prompt_names_the_retry_command(reoffer_proposal):
    pid = reoffer_proposal
    sent: list[str] = []

    async def capture(text, **kwargs):
        sent.append(text)

    with patch.object(publisher, "push_approval_text_to_teams", side_effect=capture), \
         patch.object(publisher, "push_approval_card_to_teams", new=AsyncMock()), \
         patch.object(publisher, "_mark_teams_push_sent"):
        asyncio.run(publisher.push_reoffer_prompt_for_proposal_id(pid))

    assert sent, "no Teams text was pushed"
    text = sent[0]
    assert f"retry scheduling for #{pid}" in text, text
    assert f"reject #{pid}" in text, text
