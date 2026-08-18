"""One resolver for "which proposal is this reply answering", and one answer to
"who sent it".

Both questions used to be answered three times each with quietly different
rules, and every difference was a defect:

* a resolver ordering by ``p.id DESC`` recorded a counterpart's "that works"
  against a newer unsent draft, leaving the real offer untouched and the meeting
  unbooked;
* one ``_is_kory_sender`` counted Lexi's mailbox as internal and the other did
  not, so a copy of her own outbound mail could be processed as a reply to it;
* a SQL ``replace(subject, 'Re: ', '')`` stripped the prefix from anywhere in
  the string.
"""

from __future__ import annotations

import json

import pytest

from app.scheduling.proposal_state import ProposalStatus
from app.scheduling.thread_matching import (
    extract_email,
    find_proposal_for_inbound,
    is_internal_sender,
    normalize_thread_subject,
    same_person,
    subjects_match,
)
from app.storage.lexi_db import get_lexi_connection

CONV = "tm-conversation"
SUBJECT = "Quick intro call"


@pytest.fixture
def threads():
    made: list[int] = []

    def _make(status: str, *, subject: str = SUBJECT, conversation: str = CONV) -> int:
        thread_id = f"tm-{status}-{len(made)}"
        with get_lexi_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
                " sender_email, conversation_id, raw_body) VALUES (?,?,?,?,?,?)",
                (thread_id, subject, "Dana <dana@example.com>", "dana@example.com",
                 conversation, "can we talk?"),
            )
            cur = conn.execute(
                "INSERT INTO proposals (thread_id, status, proposed_slots) VALUES (?,?,?)",
                (thread_id, status, json.dumps([{"start": "2026-09-03T09:00:00-06:00"}])),
            )
            made.append(int(cur.lastrowid))
            conn.commit()
        return made[-1]

    yield _make
    with get_lexi_connection() as conn:
        for pid in made:
            conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id LIKE 'tm-%'")
        conn.commit()


# ---------------------------------------------------------------------------
# Subject normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Re: Quick intro call",
        "RE:  Quick Intro Call",
        "Fwd: Re: Quick intro call",
        "FW: Quick intro call",
        "RE[2]: Quick intro call",
        "AW: Quick intro call",          # German clients
        "   Re:Re: Quick intro call  ",
    ],
)
def test_reply_prefixes_are_stripped_however_they_stack(raw: str):
    assert normalize_thread_subject(raw) == "quick intro call"


def test_a_prefix_is_only_stripped_from_the_front():
    """The old SQL replace() removed "Re: " from anywhere in the subject, which
    merged genuinely different threads."""
    assert normalize_thread_subject("Notes re: the intro call") == "notes re: the intro call"
    assert not subjects_match("Re: intro call", "Notes re: intro call")


# ---------------------------------------------------------------------------
# Sender identity
# ---------------------------------------------------------------------------


def test_lexis_own_mailbox_is_never_a_counterpart():
    """settings is a frozen dataclass shared by reference across modules, so this
    goes through object.__setattr__ the way the `live_writes` fixture does."""
    from app.config import settings

    previous = settings.lexi_mailbox_email
    object.__setattr__(settings, "lexi_mailbox_email", "lexi.assistant@example.com")
    try:
        # Not on an internal domain, so only the mailbox setting can catch it —
        # which is exactly the case the lexi_thread_followup copy was missing.
        assert is_internal_sender("Lexi <lexi.assistant@example.com>") is True
    finally:
        object.__setattr__(settings, "lexi_mailbox_email", previous)


def test_an_outside_sender_is_not_internal():
    assert is_internal_sender("Dana <dana@example.com>") is False


def test_email_extraction_handles_display_names():
    assert extract_email("Kory Mitchell <Kory.Mitchell@Iconicfounders.com>") == (
        "kory.mitchell@iconicfounders.com"
    )
    assert extract_email("") is None
    assert same_person("Dana <dana@example.com>", "DANA@EXAMPLE.COM") is True


# ---------------------------------------------------------------------------
# Which proposal does the reply answer?
# ---------------------------------------------------------------------------


def test_a_reply_answers_the_sent_offer_not_a_newer_draft(threads):
    """The defect this module exists for.

    A conversation carrying a sent offer AND a newer draft awaiting Kory: the
    counterpart's "that works" answers the offer that reached them. Taking the
    newest row recorded the acceptance against the draft, left the real offer in
    offer_sent with its holds, and never booked the meeting.
    """
    sent = threads(ProposalStatus.OFFER_SENT)
    newer_draft = threads(ProposalStatus.PENDING_APPROVAL)
    assert newer_draft > sent, "the draft must be the newer row for this to mean anything"

    with get_lexi_connection() as conn:
        found = find_proposal_for_inbound(conn, conversation_id=CONV, subject="Re: " + SUBJECT)
    assert found and found["proposal_id"] == sent


def test_ordering_prefers_an_outstanding_offer_over_a_declined_one(threads):
    threads(ProposalStatus.PENDING_REOFFER)
    sent = threads(ProposalStatus.OFFER_SENT)
    with get_lexi_connection() as conn:
        found = find_proposal_for_inbound(conn, conversation_id=CONV, subject=SUBJECT)
    assert found and found["proposal_id"] == sent


def test_the_conversation_id_beats_a_subject_that_merely_looks_similar(threads):
    other = threads(ProposalStatus.OFFER_SENT, conversation="different-conversation")
    mine = threads(ProposalStatus.OFFER_SENT)
    with get_lexi_connection() as conn:
        found = find_proposal_for_inbound(conn, conversation_id=CONV, subject=SUBJECT)
    assert found and found["proposal_id"] == mine != other


def test_subject_matching_is_the_fallback_when_there_is_no_conversation_id(threads):
    sent = threads(ProposalStatus.OFFER_SENT)
    with get_lexi_connection() as conn:
        found = find_proposal_for_inbound(conn, conversation_id="", subject="RE: quick INTRO call")
    assert found and found["proposal_id"] == sent


def test_a_status_filter_is_honoured(threads):
    threads(ProposalStatus.PENDING_APPROVAL)
    with get_lexi_connection() as conn:
        found = find_proposal_for_inbound(
            conn, conversation_id=CONV, subject=SUBJECT,
            statuses={ProposalStatus.OFFER_SENT},
        )
    assert found is None


def test_an_unrelated_subject_matches_nothing(threads):
    threads(ProposalStatus.OFFER_SENT)
    with get_lexi_connection() as conn:
        found = find_proposal_for_inbound(
            conn, conversation_id="", subject="Invoice #4021 is overdue"
        )
    assert found is None
