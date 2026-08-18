"""The proposal state machine — the chokepoint every status change goes through.

Before this existed, Lexi had roughly twenty places that wrote
``proposals.status`` and the guards lived on individual paths. The batch
scheduler was guarded and the single-proposal one was not, so a sent offer
silently reverted to "unsent" and approving it emailed the same person twice.

These tests pin the three properties that make that class of bug impossible:

  1. an illegal transition is refused and recorded, never silently applied;
  2. concurrent writers cannot both win — the claim is atomic;
  3. what actually happened in the world (an email was sent) is recorded
     separately from where the workflow thinks it is, and cannot be rewound.

Plus a guard-rail that fails if a new raw status write appears anywhere in
``app/``, because a chokepoint only helps while everything goes through it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from app.scheduling.proposal_state import (
    ALL_STATUSES,
    LEGAL_TRANSITIONS,
    OFFER_IS_OUT,
    SCHEDULABLE,
    TERMINAL,
    ProposalStatus,
    is_legal,
    offer_already_sent,
    offer_is_outstanding,
    record_fact,
    successors,
    transition,
)
from app.storage.lexi_db import get_lexi_connection

THREAD = "state-machine-thread"


@pytest.fixture
def proposal():
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, sender_email,"
            " raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] state machine", "x@example.com", "x@example.com", "hi"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status) VALUES (?, ?)",
            (THREAD, ProposalStatus.PENDING_APPROVAL),
        )
        pid = cur.lastrowid
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _status(pid: int) -> str:
    with get_lexi_connection() as conn:
        return str(conn.execute(
            "SELECT status FROM proposals WHERE id = ?", (pid,)
        ).fetchone()["status"])


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


def test_every_status_is_classified_by_the_transition_table():
    """A status nobody declared successors for is a status nobody thought about."""
    missing = ALL_STATUSES - set(LEGAL_TRANSITIONS)
    assert not missing, f"undeclared in LEGAL_TRANSITIONS: {sorted(missing)}"


def test_no_transition_points_at_a_status_that_does_not_exist():
    for source, targets in LEGAL_TRANSITIONS.items():
        unknown = targets - ALL_STATUSES
        assert not unknown, f"{source} -> {sorted(unknown)} is not a real status"


def test_terminal_statuses_never_reopen_into_a_live_offer():
    """`rejected` and friends must not lead back to a state that can send mail."""
    for status in TERMINAL:
        assert not (successors(status) & OFFER_IS_OUT), (
            f"{status} can reach {sorted(successors(status) & OFFER_IS_OUT)}"
        )


def test_the_engine_can_never_stage_a_first_offer_over_a_live_one():
    assert not (SCHEDULABLE & {ProposalStatus.OFFER_SENT, ProposalStatus.PENDING_INVITE,
                               ProposalStatus.EXECUTED})
    assert not (SCHEDULABLE & TERMINAL)


def test_self_transitions_are_always_legal():
    """An idempotent retry writing the status it already holds is not an error."""
    for status in ALL_STATUSES:
        assert is_legal(status, status)


# ---------------------------------------------------------------------------
# transition()
# ---------------------------------------------------------------------------


def test_a_legal_move_is_applied_and_explained(proposal):
    with get_lexi_connection() as conn:
        out = transition(
            conn,
            proposal,
            to=ProposalStatus.OFFER_SENT,
            expect=ProposalStatus.PENDING_APPROVAL,
            reason="Kory approved; offer dispatched.",
            actor="kory",
        )
        conn.commit()
    assert out.claimed and out.from_status == ProposalStatus.PENDING_APPROVAL
    assert _status(proposal) == ProposalStatus.OFFER_SENT

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT message, payload FROM audit_log WHERE reference_id = ?"
            " AND step_name = 'proposal_transition' ORDER BY id DESC LIMIT 1",
            (str(proposal),),
        ).fetchone()
    assert "pending_approval -> offer_sent" in row["message"]
    assert "Kory approved" in row["message"], "the WHY has to survive into the log"


def test_an_illegal_move_is_refused_and_recorded(proposal):
    with get_lexi_connection() as conn:
        transition(conn, proposal, to=ProposalStatus.REJECTED, reason="Kory rejected it.")
        out = transition(
            conn,
            proposal,
            to=ProposalStatus.OFFER_SENT,
            reason="Something tries to resurrect a rejected proposal.",
        )
        conn.commit()
    assert out.claimed is False
    assert "Illegal transition" in out.refusal
    assert _status(proposal) == ProposalStatus.REJECTED

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT log_level, message FROM audit_log WHERE reference_id = ?"
            " AND step_name = 'proposal_transition_refused' ORDER BY id DESC LIMIT 1",
            (str(proposal),),
        ).fetchone()
    assert row and row["log_level"] == "ERROR", "an illegal move must be loud"


def test_expect_makes_the_claim_atomic(proposal):
    """Two approvals racing: exactly one may win.

    This is what stops a card tap and a typed `approve` — or a tool-timeout
    retry — from both sending the same offer.
    """
    with get_lexi_connection() as first, get_lexi_connection() as second:
        a = transition(
            first, proposal, to=ProposalStatus.OFFER_SENT,
            expect=ProposalStatus.PENDING_APPROVAL, reason="Card tap.",
        )
        first.commit()
        b = transition(
            second, proposal, to=ProposalStatus.OFFER_SENT,
            expect=ProposalStatus.PENDING_APPROVAL, reason="Typed approve.",
        )
        second.commit()
    assert a.claimed is True
    assert b.claimed is False, "the second approval must lose, not send again"
    assert "Another path moved this proposal first" in b.refusal


def test_companion_fields_move_in_the_same_statement(proposal):
    """A row must never be readable as "approved and ready" carrying a stale draft."""
    with get_lexi_connection() as conn:
        transition(
            conn, proposal, to=ProposalStatus.PENDING_TRIAGE,
            reason="Kory gave guidance.",
            fields={"drafted_reply": None, "kory_scheduling_guidance": "try the week after"},
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, drafted_reply, kory_scheduling_guidance FROM proposals"
            " WHERE id = ?", (proposal,)
        ).fetchone()
    assert row["status"] == ProposalStatus.PENDING_TRIAGE
    assert row["drafted_reply"] is None
    assert row["kory_scheduling_guidance"] == "try the week after"


def test_coalesce_fields_refine_rather_than_erase(proposal):
    with get_lexi_connection() as conn:
        transition(conn, proposal, to=ProposalStatus.PENDING_APPROVAL,
                   reason="stage", fields={"recipient_timezone": "America/New_York"})
        transition(conn, proposal, to=ProposalStatus.PENDING_APPROVAL,
                   reason="a pass that could not determine the zone",
                   coalesce_fields={"recipient_timezone": None})
        conn.commit()
        row = conn.execute(
            "SELECT recipient_timezone FROM proposals WHERE id = ?", (proposal,)
        ).fetchone()
    assert row["recipient_timezone"] == "America/New_York"


def test_a_transition_must_say_why(proposal):
    """An unexplained status change is precisely what made these bugs invisible."""
    with get_lexi_connection() as conn:
        with pytest.raises(ValueError, match="reason"):
            transition(conn, proposal, to=ProposalStatus.REJECTED, reason="  ")


def test_an_unknown_status_is_rejected_before_it_reaches_the_database(proposal):
    with get_lexi_connection() as conn:
        with pytest.raises(ValueError, match="not a known proposal status"):
            transition(conn, proposal, to="pending_aproval", reason="typo")


def test_a_field_that_is_not_a_column_is_refused(proposal):
    with get_lexi_connection() as conn:
        with pytest.raises(ValueError, match="not a column"):
            transition(conn, proposal, to=ProposalStatus.REJECTED,
                       reason="x", fields={"drop_table": "1"})


# ---------------------------------------------------------------------------
# World facts
# ---------------------------------------------------------------------------


def test_the_world_fact_outranks_the_workflow_position(proposal):
    """The root cause, stated as a property.

    Whatever the status says, if an offer email actually went out then an
    offer is outstanding and nothing may re-stage this proposal.
    """
    with get_lexi_connection() as conn:
        record_fact(conn, proposal, "offer_sent_at")
        transition(conn, proposal, to=ProposalStatus.PENDING_TRIAGE,
                   reason="Something rolls the workflow back behind a sent offer.")
        conn.commit()
        assert _status(proposal) == ProposalStatus.PENDING_TRIAGE
        assert offer_already_sent(conn, proposal) is True
        assert offer_is_outstanding(conn, proposal) is True


def test_a_declined_offer_is_not_outstanding(proposal):
    """pending_reoffer is the one place re-staging is the whole point."""
    with get_lexi_connection() as conn:
        transition(conn, proposal, to=ProposalStatus.OFFER_SENT, reason="sent")
        record_fact(conn, proposal, "offer_sent_at")
        transition(conn, proposal, to=ProposalStatus.PENDING_REOFFER,
                   reason="They declined every time; holds released.")
        conn.commit()
        assert offer_already_sent(conn, proposal) is True
        assert offer_is_outstanding(conn, proposal) is False


def test_rows_older_than_the_fact_columns_still_read_correctly(proposal):
    """Backfill safety: a proposal that reached offer_sent before these columns
    existed must still report its offer as sent."""
    with get_lexi_connection() as conn:
        transition(conn, proposal, to=ProposalStatus.OFFER_SENT, reason="sent")
        conn.commit()
        assert conn.execute(
            "SELECT offer_sent_at FROM proposals WHERE id = ?", (proposal,)
        ).fetchone()["offer_sent_at"] is None
        assert offer_already_sent(conn, proposal) is True


# ---------------------------------------------------------------------------
# The guard-rail: a chokepoint only helps while everything goes through it
# ---------------------------------------------------------------------------

_UPDATE_PROPOSALS = re.compile(r"UPDATE\s+proposals\b", re.IGNORECASE)
_ASSIGNS_STATUS = re.compile(r"\bstatus\s*=", re.IGNORECASE)


def _set_clauses(text: str) -> list[tuple[int, str]]:
    """The SET clause of each `UPDATE proposals` statement, with its offset.

    Bounded at the WHERE so it cannot run into the next statement — and so a
    `WHERE id = ? AND status = ?` guard (which READS the status rather than
    writing it) is correctly ignored.
    """
    out: list[tuple[int, str]] = []
    for match in _UPDATE_PROPOSALS.finditer(text):
        window = text[match.end() : match.end() + 600]
        where = re.search(r"\bWHERE\b", window, re.IGNORECASE)
        out.append((match.start(), window[: where.start()] if where else window))
    return out


def test_no_module_writes_proposal_status_behind_the_chokepoints_back():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    allowed = {app_dir / "scheduling" / "proposal_state.py"}
    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if path in allowed:
            continue
        text = path.read_text()
        for offset, set_clause in _set_clauses(text):
            if _ASSIGNS_STATUS.search(set_clause):
                line = text[:offset].count("\n") + 1
                offenders.append(f"{path.relative_to(app_dir.parent)}:{line}")
    assert not offenders, (
        "These write proposals.status directly. Route them through "
        "app.scheduling.proposal_state.transition() so the move is checked, "
        "claimed atomically, and explained in the audit log:\n  "
        + "\n  ".join(offenders)
    )


def test_the_database_refuses_an_illegal_move_even_without_the_chokepoint(proposal):
    """The backstop. Triggers are generated from the same LEGAL_TRANSITIONS, so
    a maintenance script or a hand-run UPDATE cannot corrupt state quietly."""
    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?",
            (ProposalStatus.REJECTED, proposal),
        )
        conn.commit()
        with pytest.raises(sqlite3.Error, match="illegal proposal status transition"):
            conn.execute(
                "UPDATE proposals SET status = ? WHERE id = ?",
                (ProposalStatus.OFFER_SENT, proposal),
            )


def test_the_database_refuses_a_status_that_does_not_exist(proposal):
    with get_lexi_connection() as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "UPDATE proposals SET status = 'totally_made_up' WHERE id = ?",
                (proposal,),
            )
