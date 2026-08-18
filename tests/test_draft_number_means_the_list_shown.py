"""`approve draft 1` must mean the draft that WAS draft 1 when Kory read the list.

Draft numbers were resolved by re-reading the pending queue at the moment the
command was parsed. Anything that changed the queue in between changed what the
number meant, silently:

  * a draft clears — approved from a card, auto-executed, rejected, holds
    expired — and everything shifts up one;
  * a new HIGH priority proposal is staged (the orchestrator polls continuously
    and the queue sorts by priority first), pushing every number down one.

Either way `approve draft 1` sends a real email to a real person while Kory
believes he sent a different one. Same defect as a status disagreeing with the
world: the reference he used and the reference Lexi resolved were two different
things.
"""

from __future__ import annotations

import pytest

from app.bot.draft_numbering import record_pending_snapshot, resolve_draft_number
from app.bot.teams_text import format_pending_list
from app.scheduling.proposal_state import ProposalStatus, transition
from app.storage.lexi_db import get_lexi_connection

THREAD_PREFIX = "draftnum"


@pytest.fixture
def queue():
    """Three proposals awaiting approval, in list order."""
    ids: list[int] = []
    with get_lexi_connection() as conn:
        for index, who in enumerate(("Dana", "Rob", "Priya")):
            thread = f"{THREAD_PREFIX}-{index}"
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
                 f"Draft for {who}"),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    yield ids
    with get_lexi_connection() as conn:
        for pid in ids:
            conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute(
            "DELETE FROM email_threads WHERE thread_id LIKE ?", (f"{THREAD_PREFIX}-%",)
        )
        conn.execute("DELETE FROM teams_list_snapshots")
        conn.commit()


def test_a_number_still_names_the_same_draft_after_the_queue_shifts(queue):
    """The wrong-send. Kory reads the list, Dana's draft clears, he types
    `approve draft 1` meaning Dana — and must not send Rob's."""
    dana, rob, _priya = queue
    record_pending_snapshot(queue)

    with get_lexi_connection() as conn:
        transition(conn, dana, to=ProposalStatus.OFFER_SENT,
                   reason="Approved from a card while Kory was reading the list.")
        conn.commit()

    live = [rob, _priya]                       # what the queue looks like NOW
    reference = resolve_draft_number(1, live_queue_ids=live)

    assert reference.proposal_id is None, "draft 1 must not silently become Rob"
    assert reference.ok is False
    assert "Dana" in reference.problem, reference.problem
    assert "no longer waiting to send" in reference.problem


def test_a_number_is_unaffected_by_a_new_arrival_jumping_the_queue(queue):
    """A high-priority proposal staged after the listing pushes numbers down."""
    dana, rob, priya = queue
    record_pending_snapshot(queue)

    urgent = 999_001                            # jumped to the front of the queue
    reference = resolve_draft_number(1, live_queue_ids=[urgent, dana, rob, priya])

    assert reference.proposal_id == dana, "draft 1 is still the draft he was shown"


def test_an_unchanged_queue_resolves_exactly_as_before(queue):
    dana, rob, priya = queue
    record_pending_snapshot(queue)
    for position, expected in enumerate((dana, rob, priya), start=1):
        assert resolve_draft_number(position, live_queue_ids=queue).proposal_id == expected


def test_rendering_the_list_is_what_gives_the_numbers_meaning(queue):
    """Recording lives inside the renderer, so a new way of showing the list
    cannot forget it and quietly revert to positional resolution."""
    from app.agents.comms_agent import get_lexi_pending_queue

    items = [i for i in get_lexi_pending_queue() if i.proposal_id in queue]
    format_pending_list(items)

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT proposal_ids FROM teams_list_snapshots WHERE key = 'teams_pending_list'"
        ).fetchone()
    assert row is not None, "rendering the list must record its order"


def test_a_number_past_the_snapshot_falls_back_to_the_live_queue(queue):
    """He never listed, or listed a shorter list. The number then genuinely does
    refer to the current queue, which is the best available meaning."""
    dana, rob, priya = queue
    record_pending_snapshot([dana])
    assert resolve_draft_number(2, live_queue_ids=queue).proposal_id == rob


def test_a_snapshot_from_a_vanished_world_falls_back_rather_than_refusing(queue):
    """A rebuilt database or a swept test row. Refusing would be theatre — we
    cannot tell Kory what he meant, so resolve against the list that exists."""
    record_pending_snapshot([999_998, 999_999])
    assert resolve_draft_number(1, live_queue_ids=queue).proposal_id == queue[0]


def test_a_raw_proposal_id_still_resolves_to_itself(queue):
    """A Teams message from last week quoting #9871 must still work."""
    assert resolve_draft_number(9871, live_queue_ids=queue).proposal_id == 9871
