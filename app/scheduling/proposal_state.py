"""The proposal state machine — the ONE place a proposal's status changes.

Why this module exists
----------------------
Every scheduling defect found in the two days before this was written had the
same shape: **Lexi's record of what happened disagreed with what actually
happened.** A sent offer that said it was unsent, so approving it emailed the
same person twice. A reply recorded against a draft nobody sent. Her own hold
blocking the slot she was holding.

The scheduling engine was never the problem. The problem was that the proposal
lifecycle had roughly twenty entry points and the guards lived on individual
paths rather than at a chokepoint: the batch scheduler was guarded and the
single-proposal one was not; the send gate validated times and draft
composition did not. Every fix was another door, which is why closing one
revealed the next.

The model
---------
Two layers, deliberately separated, because conflating them is what caused the
worst bug:

1. **Workflow position** — ``proposals.status``. Reversible. A proposal can go
   back to ``pending_triage`` to be re-scheduled. Every change goes through
   :func:`transition`, which checks the move against :data:`LEGAL_TRANSITIONS`,
   claims it atomically, and writes an audit row.

2. **World facts** — monotonic columns recording irreversible things that
   happened OUTSIDE the database: ``offer_sent_at`` (an email is in someone's
   inbox), ``invite_sent_at`` (a calendar invite was dispatched). Set once by
   :func:`record_fact`, never cleared by ordinary flow.

The root-cause bug was workflow position contradicting a world fact: status
said ``pending_approval`` while an offer email sat in the recipient's inbox.
Code about to do something irreversible must consult the FACT, not the status
— see :func:`offer_already_sent`. A status can legitimately move backwards; an
email cannot be un-sent.

Using it
--------
    from app.scheduling.proposal_state import ProposalStatus, transition

    outcome = transition(
        conn,
        proposal_id,
        to=ProposalStatus.OFFER_SENT,
        expect={ProposalStatus.PENDING_APPROVAL, ProposalStatus.NEEDS_KORY},
        reason="Kory approved the offer; email dispatched.",
        actor="kory",
    )
    if not outcome.claimed:
        ...  # somebody else got there first, or the move was illegal

``expect`` is the atomic claim: the UPDATE carries ``AND status IN (...)``, so
it serializes on SQLite's write lock and a concurrent approval loses instead of
double-sending. Omit it only when any legal predecessor is acceptable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)


class ProposalStatus:
    """Every status a proposal can hold.

    Plain ``str`` constants rather than an ``Enum``: the codebase compares and
    interpolates statuses as strings in hundreds of places, and a ``str``/Enum
    mixin renders as ``ProposalStatus.OFFER_SENT`` inside an f-string, which
    would quietly corrupt every operator-facing error message.
    """

    # --- pre-offer -----------------------------------------------------------
    AWAITING_REPLY_PROMPT = "awaiting_reply_prompt"
    """Triaged inbound mail; nobody has decided whether Kory replies."""

    PENDING_TRIAGE = "pending_triage"
    """Queued for the scheduling engine."""

    PENDING_APPROVAL = "pending_approval"
    """Draft and slots staged, waiting on Kory."""

    # --- the offer is out ----------------------------------------------------
    OFFER_SENT = "offer_sent"
    """Offer email dispatched; holds sit on the calendar."""

    PENDING_INVITE = "pending_invite"
    """Counterpart picked one of the offered slots; Kory must send the invite."""

    PENDING_REOFFER = "pending_reoffer"
    """Counterpart rejected every offered slot; new times needed."""

    EXECUTED = "executed"
    """Invite dispatched; the meeting is booked."""

    # --- blocked, waiting on a human -----------------------------------------
    NEEDS_SCHEDULING_GUIDANCE = "needs_scheduling_guidance"
    """The engine found nothing inside the rules; Kory must say what to relax."""

    NEEDS_KORY = "needs_kory"
    """Escalated — a failure, or an urgent ask no rule-abiding time fits."""

    # --- finished ------------------------------------------------------------
    NO_REPLY_NEEDED = "no_reply_needed"
    """Auto-skipped as unimportant, or Kory declined to reply."""

    REJECTED = "rejected"
    """Kory rejected the draft, or the holds expired unanswered."""

    CANCELLED = "cancelled"
    """A booked meeting was cancelled."""


ALL_STATUSES: frozenset[str] = frozenset(
    value
    for name, value in vars(ProposalStatus).items()
    if not name.startswith("_") and isinstance(value, str)
)


# --------------------------------------------------------------------------
# Semantic groups.
#
# These replace the ad-hoc frozensets that had drifted apart across modules
# (TERMINAL_OR_SENT_STATUSES in scheduler_agent, LEXI_INVOLVED_STATUSES in
# lexi_thread_followup, _CHAT_DRAFTABLE_STATUSES in inbound_reply). Each is
# derived from one question about the world, so a new status has exactly one
# place to be classified.
# --------------------------------------------------------------------------

TERMINAL: frozenset[str] = frozenset(
    {
        ProposalStatus.REJECTED,
        ProposalStatus.NO_REPLY_NEEDED,
        ProposalStatus.CANCELLED,
    }
)
"""Nothing further happens without a human explicitly reopening the thread."""

OFFER_IS_OUT: frozenset[str] = frozenset(
    {
        ProposalStatus.OFFER_SENT,
        ProposalStatus.PENDING_INVITE,
        ProposalStatus.PENDING_REOFFER,
        ProposalStatus.EXECUTED,
    }
)
"""An offer email has left the building. Re-staging a draft here rewrites
history: the recipient is holding times we would silently replace."""

AWAITING_KORY: frozenset[str] = frozenset(
    {
        ProposalStatus.PENDING_APPROVAL,
        ProposalStatus.PENDING_INVITE,
        ProposalStatus.PENDING_REOFFER,
        ProposalStatus.NEEDS_KORY,
        ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
    }
)
"""Shows up in Kory's Teams queue — something is blocked on him."""

LEXI_INVOLVED: frozenset[str] = frozenset(
    {
        ProposalStatus.OFFER_SENT,
        ProposalStatus.PENDING_INVITE,
        ProposalStatus.PENDING_REOFFER,
        ProposalStatus.PENDING_APPROVAL,
        ProposalStatus.EXECUTED,
        ProposalStatus.AWAITING_REPLY_PROMPT,
    }
)
"""Lexi is on this thread, so an inbound reply should route to it rather than
being triaged as cold mail."""

CHAT_DRAFTABLE: frozenset[str] = frozenset(
    {
        ProposalStatus.AWAITING_REPLY_PROMPT,
        ProposalStatus.NO_REPLY_NEEDED,
        ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
    }
)
"""Kory can ask for a fresh draft from chat. Deliberately excludes everything
in :data:`OFFER_IS_OUT` — see :func:`offer_already_sent`."""

SCHEDULABLE: frozenset[str] = frozenset(
    {
        ProposalStatus.PENDING_TRIAGE,
        ProposalStatus.AWAITING_REPLY_PROMPT,
        ProposalStatus.PENDING_REOFFER,
        ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
        ProposalStatus.NEEDS_KORY,
    }
)
"""The scheduling engine may stage a first offer from these. Anything in
:data:`OFFER_IS_OUT` or :data:`TERMINAL` is excluded on purpose."""


# --------------------------------------------------------------------------
# The transition table. Declared once; enforced by transition() in Python and
# by a generated SQLite trigger for anything that bypasses it.
#
# Self-transitions (X -> X) are always permitted and treated as no-ops: an
# idempotent retry writing the status it already holds is not an error.
# --------------------------------------------------------------------------

LEGAL_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ProposalStatus.AWAITING_REPLY_PROMPT: frozenset(
        {
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.NO_REPLY_NEEDED,
            ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.PENDING_TRIAGE: frozenset(
        {
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.AWAITING_REPLY_PROMPT,
            ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.NO_REPLY_NEEDED,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.PENDING_APPROVAL: frozenset(
        {
            ProposalStatus.OFFER_SENT,
            # Outbound delegation books directly from an approved draft.
            ProposalStatus.EXECUTED,
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.AWAITING_REPLY_PROMPT,
            ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.NO_REPLY_NEEDED,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.OFFER_SENT: frozenset(
        {
            ProposalStatus.PENDING_INVITE,
            ProposalStatus.PENDING_REOFFER,
            ProposalStatus.EXECUTED,
            # A hold reminder or a counterpart-driven reschedule stages a NEW
            # draft on a thread whose offer already went out. Legal as a
            # workflow move — but the send path must not treat it as a first
            # offer, which is why hold placement keys off live holds and the
            # send keys off offer_sent_at, not off the status.
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.CANCELLED,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.PENDING_INVITE: frozenset(
        {
            ProposalStatus.EXECUTED,
            ProposalStatus.OFFER_SENT,
            ProposalStatus.PENDING_REOFFER,
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.CANCELLED,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.PENDING_REOFFER: frozenset(
        {
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.OFFER_SENT,
            ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.EXECUTED: frozenset(
        {
            ProposalStatus.CANCELLED,
            # "Can we move Wednesday?" on a booked meeting re-enters scheduling.
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.NEEDS_KORY,
        }
    ),
    ProposalStatus.NEEDS_SCHEDULING_GUIDANCE: frozenset(
        {
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.AWAITING_REPLY_PROMPT,
            ProposalStatus.NEEDS_KORY,
            ProposalStatus.NO_REPLY_NEEDED,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.NEEDS_KORY: frozenset(
        {
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.PENDING_APPROVAL,
            # An escalation raised by a FAILED send keeps its draft, so Kory's
            # "approve #N" retry sends from here.
            ProposalStatus.OFFER_SENT,
            ProposalStatus.EXECUTED,
            ProposalStatus.NEEDS_SCHEDULING_GUIDANCE,
            ProposalStatus.NO_REPLY_NEEDED,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.NO_REPLY_NEEDED: frozenset(
        {
            # Kory can always change his mind and ask for a draft after all.
            ProposalStatus.AWAITING_REPLY_PROMPT,
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.PENDING_APPROVAL,
            ProposalStatus.REJECTED,
        }
    ),
    ProposalStatus.CANCELLED: frozenset(
        {
            # "Actually, let's find another time" after a cancellation.
            ProposalStatus.PENDING_TRIAGE,
            ProposalStatus.AWAITING_REPLY_PROMPT,
        }
    ),
    ProposalStatus.REJECTED: frozenset(),
}


def is_legal(from_status: str, to_status: str) -> bool:
    """Is this move allowed? Self-transitions always are."""
    if from_status == to_status:
        return True
    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


def successors(from_status: str) -> frozenset[str]:
    return LEGAL_TRANSITIONS.get(from_status, frozenset())


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """What happened. ``claimed`` is the only thing callers should branch on."""

    claimed: bool
    proposal_id: int
    from_status: str
    to_status: str
    reason: str = ""
    refusal: str = ""

    @property
    def no_op(self) -> bool:
        return self.claimed and self.from_status == self.to_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "proposal_id": self.proposal_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "refusal": self.refusal,
        }


# --------------------------------------------------------------------------
# World facts — monotonic records of irreversible side effects
# --------------------------------------------------------------------------

FACT_OFFER_SENT_AT = "offer_sent_at"
FACT_INVITE_SENT_AT = "invite_sent_at"

MONOTONIC_FACTS: frozenset[str] = frozenset({FACT_OFFER_SENT_AT, FACT_INVITE_SENT_AT})


def record_fact(conn: sqlite3.Connection, proposal_id: int, fact: str) -> bool:
    """Stamp an irreversible world fact. Returns True if this call set it.

    Write-once by construction (``WHERE <fact> IS NULL``), so a retry that
    re-sends nothing cannot move the timestamp forward and make a two-day-old
    offer look fresh.
    """
    if fact not in MONOTONIC_FACTS:
        raise ValueError(f"{fact!r} is not a monotonic proposal fact.")
    if not _column_exists(conn, fact):
        return False
    cur = conn.execute(
        f"UPDATE proposals SET {fact} = datetime('now') "
        f"WHERE id = ? AND {fact} IS NULL",
        (proposal_id,),
    )
    return cur.rowcount == 1


def offer_already_sent(conn: sqlite3.Connection, proposal_id: int) -> bool:
    """Has an offer email for this proposal actually reached someone's inbox?

    Ask this — never the status — before doing anything that assumes the
    counterpart has not heard from us. Status is a workflow position and may
    legitimately move backwards; an email cannot be un-sent.
    """
    if not _column_exists(conn, FACT_OFFER_SENT_AT):
        # Pre-migration database: fall back to the workflow position, which is
        # what the code did before the fact columns existed.
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return bool(row) and str(row[0]) in OFFER_IS_OUT
    row = conn.execute(
        f"SELECT {FACT_OFFER_SENT_AT} AS sent_at, status FROM proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if not row:
        return False
    if row[0]:
        return True
    # Rows that predate the fact column still carry the truth in their status.
    return str(row[1] or "") in OFFER_IS_OUT


OFFER_OUTSTANDING_STATUSES: frozenset[str] = frozenset(
    {
        ProposalStatus.OFFER_SENT,
        ProposalStatus.PENDING_INVITE,
        ProposalStatus.EXECUTED,
    }
)
"""An offer is in the counterpart's inbox and still stands: they have not
declined it, and we are still holding the times.

Deliberately excludes ``pending_reoffer``. There the counterpart said none of
the times work and the holds were released, so staging fresh times is the whole
point — that is what ``begin_reoffer_schedule`` does."""


def offer_is_outstanding(conn: sqlite3.Connection, proposal_id: int) -> bool:
    """Is there a live offer we would strand by re-staging this proposal?

    This is the question to ask before rewriting a draft, and it is stricter
    than "was an offer ever sent". The difference is ``pending_reoffer``: an
    offer went out, they turned it down, the holds came off. Nothing is
    outstanding and new times are exactly what is wanted.

    A pre-offer status carrying an ``offer_sent_at`` fact is the dangerous case
    and the reason this function exists: something rolled the workflow position
    backwards behind an offer that really did go out. Trust the fact — an email
    cannot be un-sent, and re-staging here is what emailed the same person a
    second offer with a second set of holds.
    """
    row = conn.execute(
        "SELECT status FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return False
    status = str(row[0] or "")
    if status in OFFER_OUTSTANDING_STATUSES:
        return True
    if status == ProposalStatus.PENDING_REOFFER or status in TERMINAL:
        return False
    return offer_already_sent(conn, proposal_id)


def proposal_columns(conn: sqlite3.Connection) -> set[str]:
    """Columns of `proposals` on THIS connection.

    Deliberately uncached. Caching by ``id(conn)`` looks obvious and is wrong:
    CPython reuses the address of a closed connection, so a later connection
    inherits an earlier one's schema — which in a test suite full of in-memory
    databases with cut-down schemas means real columns are reported missing.
    ``PRAGMA table_info`` reads SQLite's in-memory schema in about 5µs, which is
    nothing next to the UPDATE it guards.
    """
    return {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}


def _column_exists(conn: sqlite3.Connection, column: str) -> bool:
    return column in proposal_columns(conn)


# --------------------------------------------------------------------------
# The chokepoint
# --------------------------------------------------------------------------


def transition(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    to: str,
    reason: str,
    expect: Iterable[str] | str | None = None,
    actor: str = "system",
    fields: Mapping[str, Any] | None = None,
    coalesce_fields: Mapping[str, Any] | None = None,
    step_name: str = "proposal_transition",
) -> TransitionResult:
    """Move a proposal's status. The only supported way to do so.

    Args:
        to: target status; must be a member of :data:`ALL_STATUSES`.
        reason: human-readable why, recorded in the audit log. Required —
            an unexplained status change is what made these bugs invisible.
        expect: the status(es) the caller believes the proposal is in. When
            given, the UPDATE carries ``AND status IN (...)``, making this an
            atomic compare-and-set: a concurrent writer that already moved the
            row causes this call to return ``claimed=False`` rather than
            clobbering it. Omit only when any legal predecessor will do.
        fields: companion columns written in the SAME statement as the status,
            so the row can never be observed with a new status and a stale
            draft. Column names are validated against the live schema.
        coalesce_fields: like ``fields``, but a ``None`` value leaves the
            existing column untouched (``col = COALESCE(?, col)``). For values
            that are refinements rather than replacements — a freshly resolved
            recipient timezone should not erase a stored one.
        actor: who caused this ("kory", "recipient", "scheduler", "system").

    Returns:
        A :class:`TransitionResult`. Callers branch on ``.claimed``.
    """
    if to not in ALL_STATUSES:
        raise ValueError(f"{to!r} is not a known proposal status.")
    if not str(reason or "").strip():
        raise ValueError("transition() requires a reason; silent status changes are the bug.")

    row = conn.execute(
        "SELECT status FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return _refuse(
            conn,
            proposal_id,
            from_status="",
            to=to,
            reason=reason,
            refusal=f"Proposal {proposal_id} does not exist.",
            actor=actor,
            step_name=step_name,
        )

    current = str(row[0] or "")

    expected: set[str] | None = None
    if expect is not None:
        expected = {expect} if isinstance(expect, str) else set(expect)
        if current not in expected:
            return _refuse(
                conn,
                proposal_id,
                from_status=current,
                to=to,
                reason=reason,
                refusal=(
                    f"Expected status in {sorted(expected)} but found {current!r}. "
                    "Another path moved this proposal first."
                ),
                actor=actor,
                step_name=step_name,
                level="INFO",
            )

    if not is_legal(current, to):
        return _refuse(
            conn,
            proposal_id,
            from_status=current,
            to=to,
            reason=reason,
            refusal=(
                f"Illegal transition {current!r} -> {to!r}. "
                f"Legal from {current!r}: {sorted(successors(current)) or ['(terminal)']}."
            ),
            actor=actor,
            step_name=step_name,
            level="ERROR",
        )

    assignments = ["status = ?"]
    params: list[Any] = [to]
    known = proposal_columns(conn) if (fields or coalesce_fields) else set()
    for column, value in (fields or {}).items():
        if column == "status":
            raise ValueError("Pass the target status as `to`, not in `fields`.")
        if column not in known:
            raise ValueError(f"{column!r} is not a column of proposals.")
        assignments.append(f"{column} = ?")
        params.append(value)
    for column, value in (coalesce_fields or {}).items():
        if column == "status":
            raise ValueError("Pass the target status as `to`, not in `coalesce_fields`.")
        if column not in known:
            raise ValueError(f"{column!r} is not a column of proposals.")
        assignments.append(f"{column} = COALESCE(?, {column})")
        params.append(value)
    assignments.append("updated_at = datetime('now')")

    sql = f"UPDATE proposals SET {', '.join(assignments)} WHERE id = ? AND status = ?"
    params.extend([proposal_id, current])
    claimed = conn.execute(sql, params).rowcount == 1

    if not claimed:
        # Lost the race: between the SELECT above and this UPDATE another
        # connection committed a different status. Not an error — this is the
        # mechanism that stops two approvals both sending.
        return _refuse(
            conn,
            proposal_id,
            from_status=current,
            to=to,
            reason=reason,
            refusal=(
                "Another writer changed this proposal between the read and the "
                "write; no change applied."
            ),
            actor=actor,
            step_name=step_name,
            level="INFO",
        )

    if current != to:
        _audit(
            conn,
            proposal_id,
            step_name=step_name,
            level="INFO",
            message=f"{current} -> {to}: {reason}",
            payload={
                "from_status": current,
                "to_status": to,
                "actor": actor,
                "reason": reason,
                "fields": sorted(
                    list(fields or {}) + list(coalesce_fields or {})
                ),
            },
        )
    return TransitionResult(
        claimed=True,
        proposal_id=proposal_id,
        from_status=current,
        to_status=to,
        reason=reason,
    )


def transition_standalone(proposal_id: int, **kwargs: Any) -> TransitionResult:
    """:func:`transition` on its own connection, committed. For callers that
    are not already inside a transaction."""
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        outcome = transition(conn, proposal_id, **kwargs)
        conn.commit()
        return outcome


def _refuse(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    from_status: str,
    to: str,
    reason: str,
    refusal: str,
    actor: str,
    step_name: str,
    level: str = "WARNING",
) -> TransitionResult:
    logger.log(
        logging.ERROR if level == "ERROR" else logging.INFO,
        "Refused proposal %s transition %s -> %s: %s",
        proposal_id,
        from_status or "(missing)",
        to,
        refusal,
    )
    _audit(
        conn,
        proposal_id,
        step_name=f"{step_name}_refused",
        level=level,
        message=f"Refused {from_status or '(missing)'} -> {to}: {refusal}",
        payload={
            "from_status": from_status,
            "to_status": to,
            "actor": actor,
            "reason": reason,
            "refusal": refusal,
        },
    )
    return TransitionResult(
        claimed=False,
        proposal_id=proposal_id,
        from_status=from_status,
        to_status=to,
        reason=reason,
        refusal=refusal,
    )


def _audit(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    step_name: str,
    level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (step_name, str(proposal_id), level, message, json.dumps(payload, default=str)),
        )
    except sqlite3.Error:
        # An audit write must never be the reason a legitimate transition fails.
        logger.exception("Could not write transition audit row for proposal %s.", proposal_id)


# --------------------------------------------------------------------------
# Database-level backstop
#
# transition() is the chokepoint, but a chokepoint only helps if everything
# goes through it. These triggers are generated from the SAME declarations
# above, so there is still exactly one source of truth, and they catch anything
# that bypasses the function: a maintenance script, a hand-run UPDATE, or a new
# code path written by someone who has not read this module.
#
# Three invariants, in increasing order of how much they constrain callers:
#
#   1. status must be a known value              (typo protection)
#   2. world facts are monotonic                 (never cleared, never rewound)
#   3. status transitions follow LEGAL_TRANSITIONS
#
# 1 and 2 are true by construction and carry no regression risk. 3 is the one
# that would bite if this table were ever incomplete, so it can be dropped
# without a code deploy:
#
#     .venv/bin/python -m scripts.init_lexi_db --no-transition-guard
# --------------------------------------------------------------------------

TRIGGER_STATUS_KNOWN = "trg_proposals_status_known"
TRIGGER_FACTS_MONOTONIC = "trg_proposals_facts_monotonic"
TRIGGER_LEGAL_TRANSITION = "trg_proposals_legal_transition"

ALL_GUARD_TRIGGERS = (
    TRIGGER_STATUS_KNOWN,
    TRIGGER_FACTS_MONOTONIC,
    TRIGGER_LEGAL_TRANSITION,
)


def _sql_str_list(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


def status_known_trigger_sql() -> str:
    """Reject a status that is not one of the declared twelve."""
    known = _sql_str_list(ALL_STATUSES)
    return f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGER_STATUS_KNOWN}
BEFORE UPDATE OF status ON proposals
FOR EACH ROW
WHEN NEW.status NOT IN ({known})
BEGIN
    SELECT RAISE(ABORT, 'lexi: unknown proposal status — see app/scheduling/proposal_state.py');
END;
"""


def facts_monotonic_trigger_sql() -> str:
    """World facts record things that happened outside the database. They are
    written once and never rewound; that is the whole reason the rest of the
    system is allowed to trust them over the status."""
    return f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGER_FACTS_MONOTONIC}
BEFORE UPDATE ON proposals
FOR EACH ROW
WHEN (OLD.{FACT_OFFER_SENT_AT} IS NOT NULL
      AND (NEW.{FACT_OFFER_SENT_AT} IS NULL
           OR NEW.{FACT_OFFER_SENT_AT} <> OLD.{FACT_OFFER_SENT_AT}))
  OR (OLD.{FACT_INVITE_SENT_AT} IS NOT NULL
      AND (NEW.{FACT_INVITE_SENT_AT} IS NULL
           OR NEW.{FACT_INVITE_SENT_AT} <> OLD.{FACT_INVITE_SENT_AT}))
BEGIN
    SELECT RAISE(ABORT, 'lexi: a sent offer or invite cannot be un-sent — proposal world facts are write-once');
END;
"""


def legal_transition_trigger_sql() -> str:
    """Reject any status change not declared in :data:`LEGAL_TRANSITIONS`."""
    clauses = []
    for source in sorted(LEGAL_TRANSITIONS):
        targets = LEGAL_TRANSITIONS[source]
        if not targets:
            continue
        clauses.append(
            f"(OLD.status = '{source}' AND NEW.status IN ({_sql_str_list(targets)}))"
        )
    allowed = "\n       OR ".join(clauses)
    return f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGER_LEGAL_TRANSITION}
BEFORE UPDATE OF status ON proposals
FOR EACH ROW
WHEN OLD.status <> NEW.status
 AND NOT ({allowed})
BEGIN
    SELECT RAISE(ABORT, 'lexi: illegal proposal status transition — declare it in LEGAL_TRANSITIONS or route through transition()');
END;
"""


def install_guard_triggers(
    conn: sqlite3.Connection, *, enforce_transitions: bool = True
) -> None:
    """(Re)create the guard triggers. Safe to call on every startup."""
    for name in ALL_GUARD_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(status_known_trigger_sql())
    if _column_exists(conn, FACT_OFFER_SENT_AT) and _column_exists(conn, FACT_INVITE_SENT_AT):
        conn.execute(facts_monotonic_trigger_sql())
    if enforce_transitions:
        conn.execute(legal_transition_trigger_sql())


def drop_guard_triggers(conn: sqlite3.Connection) -> None:
    for name in ALL_GUARD_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
