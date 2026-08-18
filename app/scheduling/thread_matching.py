"""Deciding which proposal an inbound email belongs to, and who sent it.

Both questions were answered three times each, slightly differently, and the
differences were bugs rather than nuance:

* **Which proposal?** ``lexi_thread_followup``, ``offer_reply`` and
  ``orchestrator`` each had their own resolver. One of them ordered by
  ``p.id DESC``, so on a conversation carrying both a sent offer and a newer
  unsent draft, a counterpart's "that works" was recorded against the draft.
  The real offer stayed untouched, the holds stayed on the calendar, and the
  meeting was never booked. That was fixed in one resolver; the others kept the
  old ordering.

* **Who sent it?** ``_is_kory_sender`` existed twice. One version counted
  Lexi's own mailbox as internal and the other did not, so a copy of Lexi's own
  outbound mail could be processed as if it were the counterpart replying.

* **Same subject?** Four subject normalizers, plus a SQL ``replace(subject,
  'Re: ', '')`` that strips "Re: " from anywhere in the string rather than the
  front.

One implementation each, here. The ordering that decides which proposal wins is
derived from the state machine, so it cannot drift from it again.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Sequence

from app.scheduling.proposal_state import (
    LEXI_INVOLVED,
    OFFER_OUTSTANDING_STATUSES,
    ProposalStatus,
)

# --------------------------------------------------------------------------
# Subject normalisation
# --------------------------------------------------------------------------

# Mail clients localise and stack these: "RE: FW: Re: Fwd: intro call".
_REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|wg|rv|tr)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)


def normalize_thread_subject(subject: str) -> str:
    """A subject reduced to what makes two messages the same thread.

    Strips reply/forward prefixes from the FRONT only, repeatedly. Doing it
    anywhere in the string (as one SQL comparison did) silently merges
    "Re: intro" with a genuinely different "Notes re: intro".
    """
    text = (subject or "").strip()
    while True:
        stripped = _REPLY_PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split()).lower()


def subjects_match(a: str, b: str) -> bool:
    left, right = normalize_thread_subject(a), normalize_thread_subject(b)
    return bool(left) and left == right


# --------------------------------------------------------------------------
# Sender identity
# --------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

# Mail from inside the house is never a counterpart's reply to an offer.
INTERNAL_DOMAINS: tuple[str, ...] = ("@iconicfounders.com", "@ifg.vc")


def extract_email(value: str | None) -> str | None:
    """The address inside "Kory Mitchell <kory@example.com>", lower-cased."""
    text = (value or "").strip()
    if not text:
        return None
    match = _EMAIL.search(text)
    if match:
        return match.group(0).lower()
    return text.lower() if "@" in text else None


def same_person(a: str | None, b: str | None) -> bool:
    left, right = extract_email(a), extract_email(b)
    return bool(left and right and left == right)


def is_internal_sender(sender: str | None) -> bool:
    """Kory, Lexi's own mailbox, or anyone at the firm.

    Deliberately includes Lexi's mailbox: a copy of her own outbound mail
    landing back in a polled folder must not be processed as the counterpart
    replying to it.
    """
    from app.config import settings

    address = extract_email(sender) or (sender or "").strip().lower()
    if not address:
        return False
    known = {e.lower() for e in settings.kory_sender_emails}
    lexi = (settings.lexi_mailbox_email or "").strip().lower()
    if lexi:
        known.add(lexi)
    if address in known:
        return True
    return any(domain in address for domain in INTERNAL_DOMAINS)


# --------------------------------------------------------------------------
# Which proposal does this reply belong to?
# --------------------------------------------------------------------------

def _status_rank_sql(column: str = "p.status") -> str:
    """SQL ordering that puts an OUTSTANDING OFFER ahead of anything else.

    A reply answers the offer that was actually sent, not a newer draft still
    waiting on Kory. Generated from the state machine so the two cannot drift.
    """
    ranked: list[str] = [
        ProposalStatus.OFFER_SENT,
        ProposalStatus.PENDING_INVITE,
        ProposalStatus.PENDING_REOFFER,
    ]
    # Anything else outstanding that the table gains later still sorts ahead of
    # the unsent statuses rather than falling into the default bucket.
    ranked += [s for s in sorted(OFFER_OUTSTANDING_STATUSES) if s not in ranked]
    cases = "\n            ".join(
        f"WHEN '{status}' THEN {index}" for index, status in enumerate(ranked)
    )
    return f"CASE {column}\n            {cases}\n            ELSE {len(ranked)}\n        END"


_PROPOSAL_COLUMNS = """
    p.id AS proposal_id,
    p.status,
    p.proposed_slots,
    p.recipient_timezone,
    p.intent_classification,
    p.is_delegation,
    p.offer_sent_at,
    e.sender,
    e.sender_email,
    e.subject,
    e.conversation_id
"""


def find_proposal_for_inbound(
    conn: sqlite3.Connection,
    *,
    conversation_id: str = "",
    subject: str = "",
    statuses: Iterable[str] | None = None,
    thread_id: str = "",
) -> dict[str, Any] | None:
    """The proposal an inbound message is answering, or None.

    Matching runs strongest-signal-first: the Outlook conversation id, then the
    thread id, then a normalised subject. Within each, an outstanding offer wins
    over a newer draft — see :func:`_status_rank_sql`.

    Subject matching is done in Python rather than SQL: SQL's ``replace`` cannot
    anchor a prefix, and a LIKE over raw subjects matched threads that merely
    contained the words.
    """
    wanted = frozenset(statuses) if statuses is not None else LEXI_INVOLVED
    if not wanted:
        return None
    placeholders = ",".join("?" * len(wanted))
    ordered = tuple(sorted(wanted))
    order_by = f"ORDER BY {_status_rank_sql()}, p.id DESC"

    if conversation_id.strip():
        row = conn.execute(
            f"""
            SELECT {_PROPOSAL_COLUMNS}
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status IN ({placeholders})
              AND e.conversation_id = ?
            {order_by}
            LIMIT 1
            """,
            (*ordered, conversation_id.strip()),
        ).fetchone()
        if row:
            return dict(row)

    if thread_id.strip():
        row = conn.execute(
            f"""
            SELECT {_PROPOSAL_COLUMNS}
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status IN ({placeholders})
              AND p.thread_id = ?
            {order_by}
            LIMIT 1
            """,
            (*ordered, thread_id.strip()),
        ).fetchone()
        if row:
            return dict(row)

    target = normalize_thread_subject(subject)
    if not target:
        return None
    rows = conn.execute(
        f"""
        SELECT {_PROPOSAL_COLUMNS}
        FROM proposals AS p
        INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
        WHERE p.status IN ({placeholders})
        {order_by}
        LIMIT 200
        """,
        ordered,
    ).fetchall()
    for row in rows:
        if normalize_thread_subject(str(row["subject"] or "")) == target:
            return dict(row)
    return None


def find_proposal_id_for_thread(
    conn: sqlite3.Connection,
    *,
    conversation_id: str = "",
    subject: str = "",
) -> int | None:
    """Any proposal on this thread, whatever its status. Used by the delegation
    follow-up path, which reopens threads Lexi has already closed out."""
    found = find_proposal_for_inbound(
        conn,
        conversation_id=conversation_id,
        subject=subject,
        statuses=_all_statuses(),
    )
    return int(found["proposal_id"]) if found else None


def _all_statuses() -> Sequence[str]:
    from app.scheduling.proposal_state import ALL_STATUSES

    return tuple(sorted(ALL_STATUSES))
