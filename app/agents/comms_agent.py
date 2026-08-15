"""Lexi Phase 4: Teams-facing approval queue and Composio execution dispatch."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import sqlite3
import traceback
from typing import Any, Literal

from app.integrations.outlook_calendar import create_calendar_event, delete_calendar_event
from app.integrations.outlook_email import (
    create_draft_reply,
    send_draft,
    send_outbound_email,
    send_pilot_reply_for_proposal,
    send_reply_in_thread,
)
from app.config import settings
from app.storage.lexi_db import get_lexi_connection
from app.utils.teams_cards import generate_approval_card

PENDING_APPROVAL = "pending_approval"
STATUS_OFFER_SENT = "offer_sent"
STATUS_PENDING_INVITE = "pending_invite"
STATUS_PENDING_REOFFER = "pending_reoffer"
STATUS_EXECUTED = "executed"
STATUS_REJECTED = "rejected"

DecisionType = Literal["approved", "modified", "rejected"]
MOCK_HOLD_PREFIX = "hold-pending-"


@dataclass(frozen=True)
class LexiQueueItem:
    proposal_id: int
    thread_id: str
    subject: str | None
    sender: str | None
    raw_body: str | None
    intent_classification: str | None
    priority_tier: str | None
    proposed_slots: list[dict[str, Any]]
    drafted_reply: str | None
    confidence_score: float | None
    justification: str | None
    voice_mode: str | None
    holds: list[dict[str, Any]]
    approval_card: dict[str, Any]
    # Gate warnings (e.g. "no availability in the requested week — offering the
    # following week"). The card renders these in Attention color; the text-only
    # path must carry them too or Kory approves without seeing the caveat.
    scheduling_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def teams_summary_line(self) -> str:
        slot_preview = ""
        if self.proposed_slots:
            first = self.proposed_slots[0]
            slot_preview = f" | first slot {first.get('start', '?')}"
        return (
            f"[{self.proposal_id}] {self.subject or '(no subject)'} | "
            f"from {self.sender or 'unknown'} | {self.intent_classification or 'unknown'} | "
            f"priority={self.priority_tier or 'medium'}{slot_preview}"
        )


@dataclass
class ExecutionResult:
    ok: bool
    proposal_id: int
    status: str
    decision: str
    calendar_event_id: str | None = None
    email_sent: bool = False
    holds_released: int = 0
    holds_confirmed: int = 0
    # Human-readable times of the holds actually placed — the reply Kory sees
    # must claim exactly these, never inferred ones (live 2026-08-11: "all
    # three holds confirmed" was said about times that had no holds).
    holds_placed_times: list[str] | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def execute_lexi_invite(
    proposal_id: int,
    selected_slot: str,
    authorized_by: str,
    *,
    decision_source: str = "teams_card",
    modification_notes: str | None = None,
) -> ExecutionResult:
    """Send Outlook invite after recipient picked a slot and Kory approved."""
    return execute_lexi_approval(
        proposal_id=proposal_id,
        decision="approved",
        selected_slot=selected_slot,
        authorized_by=authorized_by,
        decision_source=decision_source,
        modification_notes=modification_notes,
        execution_phase="send_invite",
    )


def cancel_booked_meeting(
    proposal_id: int,
    *,
    reason: str = "",
    authorized_by: str = "kory",
    decision_source: str = "hermes_teams_text",
) -> dict[str, Any]:
    """Cancel an executed proposal's booked meeting on Kory's explicit command.

    Deletes the tracked invite event (Outlook sends the attendee a cancellation
    notice) and marks the proposal cancelled. Never guesses at untracked
    events — without invite_event_id it refuses and says so.
    """
    with get_lexi_connection() as conn:
        proposal = _fetch_proposal_bundle(conn, proposal_id)
        if not proposal:
            return {"ok": False, "error": f"Proposal {proposal_id} was not found."}
        status = str(proposal.get("status") or "")
        if status != STATUS_EXECUTED:
            return {
                "ok": False,
                "error": (
                    f"Proposal {proposal_id} has no booked meeting to cancel "
                    f"(status={status})."
                ),
            }
        event_id = str(proposal.get("invite_event_id") or "").strip()
        if not event_id:
            return {
                "ok": False,
                "error": (
                    "No invite event is tracked for this meeting — "
                    "cancel it directly in Outlook."
                ),
            }

        from app.integrations.outlook_calendar import delete_calendar_event

        try:
            delete_error = delete_calendar_event(event_id)
        except Exception as exc:  # noqa: BLE001
            delete_error = f"{type(exc).__name__}: {exc}"
        if delete_error:
            return {"ok": False, "error": f"Could not cancel the meeting: {delete_error}"}

        _insert_approval(
            conn,
            proposal_id=proposal_id,
            decision="cancelled",
            decision_source=decision_source,
            authorized_by=authorized_by,
            modification_notes=(reason or None),
        )
        conn.execute(
            """
            UPDATE proposals
            SET status = 'cancelled', invite_event_id = NULL,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (proposal_id,),
        )
        _insert_audit_log(
            conn,
            step_name="meeting_cancelled",
            reference_id=str(proposal_id),
            log_level="INFO",
            message="Booked meeting cancelled on Kory's command.",
            payload={
                "proposal_id": proposal_id,
                "event_id": event_id,
                "reason": reason,
            },
        )
        conn.commit()
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": "cancelled",
        "subject": proposal.get("subject"),
        "sender": proposal.get("sender"),
    }


def get_lexi_invite_queue() -> list[LexiQueueItem]:
    """Proposals waiting for Kory to approve sending the calendar invite."""
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.thread_id,
                p.intent_classification,
                p.priority_tier,
                p.proposed_slots,
                p.drafted_reply,
                p.confidence_score,
                p.justification,
                p.voice_mode,
                p.recipient_selected_slot,
                e.subject,
                e.sender,
                e.raw_body
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status = ?
            ORDER BY p.id ASC
            """,
            (STATUS_PENDING_INVITE,),
        ).fetchall()

        items: list[LexiQueueItem] = []
        for row in rows:
            proposal_id = int(row["proposal_id"])
            holds = _fetch_holds(conn, proposal_id)
            proposal_record = {
                "id": proposal_id,
                "thread_id": row["thread_id"],
                "intent_classification": row["intent_classification"],
                "priority_tier": row["priority_tier"],
                "drafted_reply": row["drafted_reply"],
                "justification": row["justification"],
                "confidence_score": row["confidence_score"],
                "voice_mode": row["voice_mode"],
                "proposed_slots": _parse_json_list(row["proposed_slots"]),
                "recipient_selected_slot": row["recipient_selected_slot"],
            }
            email_record = {
                "subject": row["subject"],
                "sender": row["sender"],
                "raw_body": row["raw_body"],
            }
            from app.utils.teams_cards import generate_invite_prompt_card

            approval_card = generate_invite_prompt_card(
                proposal_record,
                email_record,
                holds,
            )
            items.append(
                LexiQueueItem(
                    proposal_id=proposal_id,
                    thread_id=str(row["thread_id"]),
                    subject=row["subject"],
                    sender=row["sender"],
                    raw_body=row["raw_body"],
                    intent_classification=row["intent_classification"],
                    priority_tier=row["priority_tier"],
                    proposed_slots=proposal_record["proposed_slots"],
                    drafted_reply=row["drafted_reply"],
                    confidence_score=row["confidence_score"],
                    justification=row["justification"],
                    voice_mode=row["voice_mode"],
                    holds=holds,
                    approval_card=approval_card,
                )
            )
        return items


def _extra_attendees_from_reply(reply_body: str) -> list[str]:
    """Other addresses the recipient asks to include ('add my colleague …').

    Reads only the sender's new text (quoted history repeats every address in
    the thread) and drops Lexi/Kory/known-internal addresses.
    """
    from app.config import resolve_kory_cc_email, resolve_kory_sender_emails, settings
    from app.scheduling.recipient_slot import _reply_text_for_matching

    text = _reply_text_for_matching(reply_body or "").lower()
    if not text:
        return []
    found = set(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text))
    skip = {resolve_kory_cc_email(), *(e.lower() for e in resolve_kory_sender_emails())}
    if settings.lexi_mailbox_email:
        skip.add(settings.lexi_mailbox_email.lower())
    skip.add("lexi@iconicfounders.com")
    return sorted(found - skip)


def mark_recipient_slot_choice(
    proposal_id: int,
    selected_slot: dict[str, str],
    *,
    reply_body: str = "",
) -> dict[str, Any]:
    """Store recipient's chosen slot and move proposal to pending_invite."""
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status, thread_id FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Proposal {proposal_id} not found."}
        # pending_reoffer counts: they rejected the offered times and then
        # proposed a NEW one that validated — that pick is what we wanted.
        if row["status"] not in {STATUS_OFFER_SENT, STATUS_PENDING_REOFFER}:
            return {
                "ok": False,
                "error": f"Proposal {proposal_id} is not awaiting recipient reply (status={row['status']}).",
            }
        sender_row = conn.execute(
            "SELECT sender FROM email_threads WHERE thread_id = ?",
            (row["thread_id"],),
        ).fetchone()
        own = _extract_email(sender_row["sender"] if sender_row else "") or ""
        extras = [
            e for e in _extra_attendees_from_reply(reply_body) if e != own.lower()
        ]
        if extras:
            selected_slot = {**selected_slot, "extra_attendees": extras}
        conn.execute(
            """
            UPDATE proposals
            SET status = ?, recipient_selected_slot = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                STATUS_PENDING_INVITE,
                json.dumps(selected_slot),
                proposal_id,
            ),
        )
        _insert_audit_log(
            conn,
            step_name="recipient_slot_choice",
            reference_id=str(proposal_id),
            log_level="INFO",
            message="Recipient selected a meeting slot.",
            payload={"proposal_id": proposal_id, "selected_slot": selected_slot, "reply_body": reply_body[:500]},
        )
        conn.commit()
    return {"ok": True, "proposal_id": proposal_id, "status": STATUS_PENDING_INVITE}


def mark_recipient_reoffer_request(
    proposal_id: int,
    *,
    reply_body: str = "",
) -> dict[str, Any]:
    """Recipient declined offered times — release holds and ask Kory for a new round."""
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Proposal {proposal_id} not found."}
        if row["status"] != STATUS_OFFER_SENT:
            return {
                "ok": False,
                "error": f"Proposal {proposal_id} is not awaiting recipient reply (status={row['status']}).",
            }
        dummy = ExecutionResult(
            ok=False,
            proposal_id=proposal_id,
            status=STATUS_OFFER_SENT,
            decision="reoffer",
            warnings=[],
            errors=[],
        )
        released = _release_all_holds(conn, proposal_id, dummy)
        conn.execute(
            """
            UPDATE proposals
            SET status = ?, recipient_selected_slot = NULL, updated_at = datetime('now')
            WHERE id = ?
            """,
            (STATUS_PENDING_REOFFER, proposal_id),
        )
        _insert_audit_log(
            conn,
            step_name="recipient_reoffer_request",
            reference_id=str(proposal_id),
            log_level="INFO",
            message="Recipient indicated offered times do not work.",
            payload={
                "proposal_id": proposal_id,
                "holds_released": released,
                "reply_body": reply_body[:500],
            },
        )
        conn.commit()
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": STATUS_PENDING_REOFFER,
        "holds_released": released,
    }


def get_lexi_pending_queue() -> list[LexiQueueItem]:
    """Return pending_approval proposals joined with source email thread metadata."""
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.thread_id,
                p.intent_classification,
                p.priority_tier,
                p.proposed_slots,
                p.drafted_reply,
                p.confidence_score,
                p.justification,
                p.voice_mode,
                p.scheduling_note,
                COALESCE(p.recipient_timezone, e.recipient_timezone) AS recipient_timezone,
                e.subject,
                e.sender,
                e.raw_body
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status = ?
            ORDER BY
                CASE p.priority_tier
                    WHEN 'high' THEN 0
                    WHEN 'medium' THEN 1
                    ELSE 2
                END,
                CASE WHEN p.intent_classification = 'reschedule' THEN 0 ELSE 1 END,
                p.id ASC
            """,
            (PENDING_APPROVAL,),
        ).fetchall()

        items: list[LexiQueueItem] = []
        for row in rows:
            proposal_id = int(row["proposal_id"])
            holds = _fetch_holds(conn, proposal_id)
            proposal_record = {
                "id": proposal_id,
                "thread_id": row["thread_id"],
                "intent_classification": row["intent_classification"],
                "priority_tier": row["priority_tier"],
                "drafted_reply": row["drafted_reply"],
                "justification": row["justification"],
                "confidence_score": row["confidence_score"],
                "voice_mode": row["voice_mode"],
                "proposed_slots": _parse_json_list(row["proposed_slots"]),
                "recipient_timezone": row["recipient_timezone"] if "recipient_timezone" in row.keys() else None,
            }
            email_record = {
                "subject": row["subject"],
                "sender": row["sender"],
                "raw_body": row["raw_body"],
            }
            approval_card = generate_approval_card(proposal_record, email_record, holds)
            items.append(
                LexiQueueItem(
                    proposal_id=proposal_id,
                    thread_id=str(row["thread_id"]),
                    subject=row["subject"],
                    sender=row["sender"],
                    raw_body=row["raw_body"],
                    intent_classification=row["intent_classification"],
                    priority_tier=row["priority_tier"],
                    proposed_slots=proposal_record["proposed_slots"],
                    drafted_reply=row["drafted_reply"],
                    confidence_score=row["confidence_score"],
                    justification=row["justification"],
                    voice_mode=row["voice_mode"],
                    holds=holds,
                    approval_card=approval_card,
                    scheduling_note=row["scheduling_note"],
                )
            )
        return items


def execute_lexi_approval(
    proposal_id: int,
    decision: str,
    selected_slot: str,
    authorized_by: str,
    *,
    decision_source: str = "teams_card",
    modification_notes: str | None = None,
    execution_phase: str = "send_offer",
) -> ExecutionResult:
    """Record approval decision and dispatch calendar/email execution via Composio.

    Phases:
    - send_offer: Kory approves the time-offer email (holds stay on calendar).
    - send_invite: Kory approves sending the Outlook invite after recipient picked a slot.
    """
    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approved", "modified", "rejected"}:
        raise ValueError("decision must be one of: approved, modified, rejected")

    result = ExecutionResult(
        ok=False,
        proposal_id=proposal_id,
        status=PENDING_APPROVAL,
        decision=normalized_decision,
        errors=[],
        warnings=[],
    )

    with get_lexi_connection() as conn:
        conn.execute("SAVEPOINT lexi_execution")
        try:
            proposal = _fetch_proposal_bundle(conn, proposal_id)
            if not proposal:
                raise ValueError(f"Proposal {proposal_id} was not found.")
            phase = (execution_phase or "send_offer").strip().lower()
            if phase not in {"send_offer", "send_invite"}:
                raise ValueError("execution_phase must be send_offer or send_invite")

            status = str(proposal["status"] or "")
            if normalized_decision == "rejected":
                if status not in {
                    PENDING_APPROVAL,
                    STATUS_OFFER_SENT,
                    STATUS_PENDING_INVITE,
                    # Escalated proposals must be rejectable — "just drop it"
                    # after an escalation had no path except DB surgery
                    # (live D5: needs_kory 7999; the 08-06 cleanup hit the
                    # same wall).
                    "needs_kory",
                    "needs_scheduling_guidance",
                }:
                    raise ValueError(
                        f"Proposal {proposal_id} cannot be rejected (status={status})."
                    )
            elif phase == "send_offer":
                if status == STATUS_OFFER_SENT and normalized_decision in {"approved", "modified"}:
                    result.status = STATUS_OFFER_SENT
                    result.ok = True
                    result.warnings = (result.warnings or []) + [
                        "Offer already sent for this proposal; no duplicate email dispatched."
                    ]
                    conn.execute("RELEASE SAVEPOINT lexi_execution")
                    conn.commit()
                    return result
                if status == "needs_kory" and str(proposal.get("drafted_reply") or "").strip():
                    # A failed send escalates to needs_kory with the draft
                    # intact; Kory's "approve #N" retry must work (live H-4:
                    # the escalation itself suggested re-approving, but this
                    # gate refused it).
                    pass
                elif status != PENDING_APPROVAL:
                    raise ValueError(
                        f"Proposal {proposal_id} is not pending draft approval (status={status})."
                    )
            elif phase == "send_invite":
                if status not in {STATUS_PENDING_INVITE, STATUS_OFFER_SENT}:
                    raise ValueError(
                        f"Proposal {proposal_id} is not ready for invite dispatch (status={status})."
                    )

            _insert_approval(
                conn,
                proposal_id=proposal_id,
                decision=normalized_decision,
                decision_source=decision_source,
                authorized_by=authorized_by,
                modification_notes=modification_notes,
            )

            if normalized_decision == "rejected":
                released = _release_all_holds(conn, proposal_id, result)
                _set_proposal_status(conn, proposal_id, STATUS_REJECTED)
                result.holds_released = released
                result.status = STATUS_REJECTED
                result.ok = True
            else:
                if phase == "send_offer":
                    from app.scheduling.hold_reminder import is_hold_reminder_proposal

                    hold_reminder = is_hold_reminder_proposal(proposal)
                    if not hold_reminder:
                        # Final slot gate (live 2026-08-11, proposal 9187): the
                        # draft's offered times must be the staged slots, and
                        # every staged slot must still be free right now. A
                        # hand-edited draft that diverged, or a slot booked
                        # since staging, refuses HERE — before the email exists.
                        gate_error = _pre_send_slot_gate(proposal)
                        if gate_error:
                            raise ValueError(gate_error)
                    email_ok, email_error = _send_drafted_reply(proposal, result)
                    result.email_sent = email_ok
                    if email_error:
                        result.errors.append(email_error)
                    if email_ok:
                        if hold_reminder:
                            _insert_audit_log(
                                conn,
                                step_name="hold_reminder_sent",
                                reference_id=str(proposal_id),
                                log_level="INFO",
                                message="Hold reminder email sent after Kory approval.",
                                payload={"proposal_id": proposal_id},
                            )
                            conn.execute(
                                """
                                UPDATE proposals
                                SET scheduling_note = NULL, updated_at = datetime('now')
                                WHERE id = ?
                                """,
                                (proposal_id,),
                            )
                            _set_proposal_status(conn, proposal_id, STATUS_OFFER_SENT)
                            result.status = STATUS_OFFER_SENT
                            result.warnings = (result.warnings or []) + [
                                "Hold reminder sent — existing calendar holds unchanged."
                            ]
                        else:
                            # ATOMICITY: the offer email is already dispatched (an
                            # external, non-transactional side effect). Commit
                            # OFFER_SENT NOW — before the calendar hold work — so a
                            # failure during hold placement (e.g. a transient DB
                            # lock) can never roll the proposal back to
                            # pending_approval and let it be re-sent (duplicate email).
                            _set_proposal_status(conn, proposal_id, STATUS_OFFER_SENT)
                            result.status = STATUS_OFFER_SENT
                            _insert_audit_log(
                                conn,
                                step_name="offer_email_sent",
                                reference_id=str(proposal_id),
                                log_level="INFO",
                                message="Offer email sent; status committed before hold placement.",
                                payload={"proposal_id": proposal_id},
                            )
                            conn.execute("RELEASE SAVEPOINT lexi_execution")
                            conn.commit()
                            conn.execute("SAVEPOINT lexi_execution")
                            # Holds go on their OWN connection: the approval
                            # connection races the orchestrator/webhook writers,
                            # and both live runs of this path died on a lock
                            # mid-loop (orphaning calendar events). Per-slot
                            # resumability makes the retries incremental.
                            hold_count, hold_error = _place_holds_isolated(
                                proposal_id=proposal_id, proposal=proposal, result=result
                            )
                            if hold_error:
                                result.errors.append(hold_error)
                                result.warnings = (result.warnings or []) + [
                                    f"Email sent — calendar holds need attention: {hold_error}"
                                ]
                            result.holds_confirmed = hold_count
                            result.holds_placed_times = _hold_times_on_calendar(
                                proposal_id
                            )
                            try:
                                _dispatch_asana_reservation_reminder_if_needed(
                                    conn,
                                    proposal_id=proposal_id,
                                    proposal=proposal,
                                    time_slot="",
                                    result=result,
                                )
                            except Exception:  # noqa: BLE001 — asana is best-effort
                                conn.execute("ROLLBACK TO SAVEPOINT lexi_execution")
                                conn.execute("SAVEPOINT lexi_execution")
                    else:
                        result.status = PENDING_APPROVAL
                    result.ok = email_ok
                else:
                    slots = proposal.get("proposed_slots") or []
                    holds = proposal.get("holds") or []
                    confirmed_id: str | None = None
                    selected: dict[str, str] = {}

                    stored_slot = _parse_recipient_selected_slot(proposal)
                    if stored_slot:
                        selected = stored_slot
                    elif slots or holds:
                        selected = _resolve_selected_slot(proposal, selected_slot)

                    if selected:
                        # The calendar is authoritative at the moment of booking,
                        # not at the moment of offering. Something can land on the
                        # slot between the offer going out and this confirmation —
                        # so re-check here, before anything is deleted or released.
                        clash = _confirm_time_conflict(proposal, selected)
                        if clash:
                            # Deliberately no confirm and NO hold release: the holds
                            # are the only thing still protecting this slot, and
                            # releasing them would hand it away while Kory decides.
                            result.status = PENDING_APPROVAL
                            result.ok = False
                            result.holds_confirmed = 0
                            result.errors.append(clash["error"])
                            result.warnings = (result.warnings or []) + [clash["kory_message"]]
                            _insert_audit_log(
                                conn,
                                step_name="confirm_blocked_by_conflict",
                                reference_id=str(proposal_id),
                                log_level="ERROR",
                                message=clash["error"],
                                payload={
                                    "proposal_id": proposal_id,
                                    "slot": selected,
                                    "conflicting_events": clash["conflicting_events"],
                                },
                            )
                            conn.execute("RELEASE SAVEPOINT lexi_execution")
                            conn.commit()
                            return result

                        confirmed_id, hold_errors = _confirm_selected_hold(
                            conn,
                            proposal_id=proposal_id,
                            proposal=proposal,
                            selected_slot=selected,
                            result=result,
                        )
                        result.errors.extend(hold_errors)
                        result.calendar_event_id = confirmed_id
                        result.holds_confirmed = 1 if confirmed_id else 0
                        released = _release_unused_holds(
                            conn,
                            proposal_id,
                            keep_event_id=confirmed_id,
                            result=result,
                        )
                        result.holds_released = released
                        if confirmed_id:
                            prior_invite = str(
                                proposal.get("invite_event_id") or ""
                            ).strip()
                            if prior_invite and prior_invite != confirmed_id:
                                # Reschedule: the new invite replaces the original
                                # meeting — remove the old event only now that the
                                # replacement exists.
                                try:
                                    from app.integrations.outlook_calendar import (
                                        delete_calendar_event,
                                    )

                                    delete_error = delete_calendar_event(prior_invite)
                                except Exception as exc:  # noqa: BLE001
                                    delete_error = f"{type(exc).__name__}: {exc}"
                                _insert_audit_log(
                                    conn,
                                    step_name="reschedule_released_previous_invite",
                                    reference_id=str(proposal_id),
                                    log_level="INFO" if not delete_error else "ERROR",
                                    message=(
                                        "Previous invite removed after reschedule."
                                        if not delete_error
                                        else f"Could not remove previous invite: {delete_error}"
                                    ),
                                    payload={
                                        "proposal_id": proposal_id,
                                        "previous_event_id": prior_invite,
                                        "new_event_id": confirmed_id,
                                    },
                                )
                                if delete_error:
                                    result.warnings = (result.warnings or []) + [
                                        "New invite sent, but the ORIGINAL meeting "
                                        f"could not be removed: {delete_error}"
                                    ]
                            conn.execute(
                                "UPDATE proposals SET invite_event_id = ? WHERE id = ?",
                                (confirmed_id, proposal_id),
                            )

                    _set_proposal_status(conn, proposal_id, STATUS_EXECUTED)
                    result.status = STATUS_EXECUTED
                    result.ok = bool(confirmed_id)
                    if result.errors and not result.ok:
                        result.warnings = list(result.errors)

            _insert_audit_log(
                conn,
                step_name="execution_dispatch",
                reference_id=str(proposal_id),
                log_level="INFO" if result.ok else "ERROR",
                message=(
                    f"Execution dispatch completed for proposal {proposal_id} "
                    f"with decision={normalized_decision}."
                ),
                payload={
                    "proposal_id": proposal_id,
                    "decision": normalized_decision,
                    "decision_source": decision_source,
                    "authorized_by": authorized_by,
                    "selected_slot": selected_slot,
                    "execution": result.to_dict(),
                },
            )
            conn.execute("RELEASE SAVEPOINT lexi_execution")
            conn.commit()
            if (
                phase == "send_offer"
                and normalized_decision in {"approved", "modified"}
                and not result.email_sent
                and result.errors
            ):
                from app.scheduling.kory_escalation import escalate_to_kory

                esc = escalate_to_kory(
                    proposal_id,
                    failure_error="; ".join(result.errors),
                    reason="Offer email send failed after Kory approval.",
                )
                result.warnings = (result.warnings or []) + [
                    esc.get("kory_message")
                    or esc.get("summary")
                    or "The send didn't go through — flagged here for you. Reply to retry.",
                ]
            if result.ok or normalized_decision == "rejected":
                from app.storage.learning_log import record_approval_outcome

                record_approval_outcome(
                    proposal_id=proposal_id,
                    decision=normalized_decision,
                    intent=proposal.get("intent_classification"),
                    voice_mode=proposal.get("voice_mode"),
                    send_channel=proposal.get("send_channel"),
                    drafted_reply=str(proposal.get("drafted_reply") or ""),
                    selected_slot=selected_slot,
                    modification_notes=modification_notes,
                )
            return result
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT lexi_execution")
            conn.execute("RELEASE SAVEPOINT lexi_execution")
            tb = traceback.format_exc()
            _insert_audit_log(
                conn,
                step_name="execution_dispatch",
                reference_id=str(proposal_id),
                log_level="ERROR",
                message=f"Execution dispatch failed for proposal {proposal_id}.",
                payload={
                    "proposal_id": proposal_id,
                    "decision": normalized_decision,
                    "authorized_by": authorized_by,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": tb,
                },
            )
            conn.commit()
            result.errors = (result.errors or []) + [f"{type(exc).__name__}: {exc}"]
            result.ok = False
            return result


def _hold_times_on_calendar(proposal_id: int) -> list[str]:
    """Human-readable times of this proposal's live holds, straight from the
    holds table — the only thing the confirmation reply may claim."""
    from app.scheduling.busy_intervals import local_dt, parse_iso_datetime

    times: list[str] = []
    try:
        with get_lexi_connection() as conn:
            rows = conn.execute(
                "SELECT slot_start FROM holds WHERE proposal_id = ? "
                "AND COALESCE(expires_at, '') != 'released' ORDER BY slot_start",
                (proposal_id,),
            ).fetchall()
        for row in rows:
            start = parse_iso_datetime(str(row["slot_start"] or ""))
            if start:
                times.append(local_dt(start).strftime("%A, %B %-d at %-I:%M %p MT"))
    except Exception:  # noqa: BLE001 — reporting detail must never break the send
        logger.exception("Could not read hold times for proposal %s", proposal_id)
    return times


def _pre_send_slot_gate(proposal: dict[str, Any]) -> str | None:
    """Refuse an offer send whose times are unproven. Two checks:

    1. The draft's offered times must all be staged slots (holds are placed
       for the slots, so a divergent draft means holds that don't match the
       email — the 2026-08-11 defect).
    2. Every staged slot must still be free on the live calendar at this
       moment — the calendar is authoritative at send time, not at staging.
    Returns an error string to refuse with, or None to proceed."""
    slots = proposal.get("proposed_slots") or []
    if not slots:
        return None

    from app.scheduling.draft_slot_sync import (
        conflicting_event_subject,
        draft_matches_slots,
    )

    ok, mismatch = draft_matches_slots(
        draft_body=str(proposal.get("drafted_reply") or ""),
        proposed_slots=slots,
    )
    if not ok:
        return mismatch

    from app.scheduling.busy_intervals import local_dt, parse_iso_datetime, slot_conflicts_busy
    from app.scheduling.calendar_context import load_scheduling_calendar_context

    context = load_scheduling_calendar_context(
        subject=str(proposal.get("subject") or ""),
        body=str(proposal.get("raw_body") or ""),
    )
    if (context or {}).get("status") != "available":
        return (
            "Could not read Kory's calendar to re-verify the offered times — "
            "refusing to send unverified times. Nothing was sent; try again "
            "in a minute."
        )
    busy = list(context.get("busy_events") or [])
    clashes: list[str] = []
    for slot in slots:
        if slot_conflicts_busy(slot, busy):
            start = parse_iso_datetime(str(slot.get("start") or ""))
            label = (
                local_dt(start).strftime("%A, %B %-d at %-I:%M %p MT")
                if start
                else str(slot.get("start"))
            )
            clash = conflicting_event_subject(slot, busy)
            clashes.append(f"{label}" + (f' (overlaps "{clash}")' if clash else ""))
    if clashes:
        return (
            "NOT SENT — these offered times are no longer free on Kory's "
            f"calendar: {'; '.join(clashes)}. Run lexi_retry_scheduling to get "
            "fresh times, then approve again."
        )
    return None


def _place_holds_isolated(
    *,
    proposal_id: int,
    proposal: dict[str, Any],
    result: ExecutionResult,
) -> tuple[int, str | None]:
    """Post-send hold placement on a fresh connection, retried on lock races.

    The offer email is already out; this must never raise. Each attempt gets
    its own connection + transaction so a 'database is locked' loser rolls back
    cleanly, and the per-slot resume in place_offered_holds (plus own-orphan
    adoption) lets the next attempt finish what a crashed one started. The
    outcome — success or final failure — is always audited on a connection that
    cannot be rolled back by the caller.
    """
    import time as _time

    last_error: str | None = None
    for attempt in range(3):
        if attempt:
            _time.sleep(0.5 * attempt)
        try:
            with get_lexi_connection() as hconn:
                count, err = _place_holds_after_offer(
                    hconn, proposal_id=proposal_id, proposal=proposal, result=result
                )
                hconn.commit()
            if not err:
                return count, None
            last_error = err
        except Exception as exc:  # noqa: BLE001 — sent offer must never revert
            last_error = f"{type(exc).__name__}: {exc}"
    try:
        with get_lexi_connection() as aconn:
            _insert_audit_log(
                aconn,
                step_name="hold_placement_failed",
                reference_id=str(proposal_id),
                log_level="ERROR",
                message=f"Offer email sent but hold placement failed: {last_error}",
                payload={"proposal_id": proposal_id, "hold_error": last_error},
            )
            aconn.commit()
    except Exception:  # noqa: BLE001 — auditing is best-effort here
        logger.exception("Could not audit hold placement failure for %s", proposal_id)
    return 0, last_error


def _place_holds_after_offer(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    proposal: dict[str, Any],
    result: ExecutionResult,
) -> tuple[int, str | None]:
    """Place calendar holds after the offer email is sent (per scheduling rules)."""
    from app.integrations.hold_placement import HoldPlacementError, place_offered_holds
    from app.scheduling.calendar_intelligence import resolve_write_calendar_name

    slots = proposal.get("proposed_slots") or []
    if not slots:
        return 0, None
    existing = _fetch_holds(conn, proposal_id)
    if len(existing) >= len(slots):
        return len(existing), None
    # Fewer holds than slots: a partial earlier run — place_offered_holds
    # skips the slots already held and completes the rest.
    try:
        count = place_offered_holds(
            conn,
            proposal_id=proposal_id,
            slots=slots,
            intent_classification=proposal.get("intent_classification"),
            meeting_subject=proposal.get("subject"),
            calendar_name=resolve_write_calendar_name(
                intent=proposal.get("intent_classification")
            ),
            sender=proposal.get("sender"),
            body=str(proposal.get("raw_body") or ""),
        )
        _insert_audit_log(
            conn,
            step_name="hold_placement",
            reference_id=str(proposal_id),
            log_level="INFO",
            message=f"Placed {count} hold(s) after offer email sent.",
            payload={"proposal_id": proposal_id, "hold_count": count},
        )
        return count, None
    except HoldPlacementError as exc:
        return 0, str(exc)


def _parse_recipient_selected_slot(proposal: dict[str, Any]) -> dict[str, Any] | None:
    raw = proposal.get("recipient_selected_slot")
    if not raw:
        return None
    if not isinstance(raw, dict):
        try:
            raw = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError):
            return None
    if isinstance(raw, dict) and raw.get("start"):
        slot: dict[str, Any] = {"start": str(raw["start"]), "end": str(raw.get("end") or "")}
        extras = raw.get("extra_attendees")
        if isinstance(extras, list) and extras:
            slot["extra_attendees"] = [str(e) for e in extras]
        return slot
    return None


def _fetch_holds(conn: sqlite3.Connection, proposal_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, event_id, slot_start, slot_end, created_at
        FROM holds
        WHERE proposal_id = ?
        ORDER BY id ASC
        """,
        (proposal_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_proposal_bundle(conn: sqlite3.Connection, proposal_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            p.id,
            p.thread_id,
            p.status,
            p.intent_classification,
            p.priority_tier,
            p.proposed_slots,
            p.drafted_reply,
            p.confidence_score,
            p.justification,
                p.voice_mode,
                p.send_channel,
                p.is_delegation,
                p.recipient_selected_slot,
                p.reply_message_id,
                p.scheduling_note,
                p.invite_event_id,
                e.subject,
            e.sender,
            e.raw_body,
            e.conversation_id
        FROM proposals AS p
        INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
        WHERE p.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if not row:
        return None
    bundle = dict(row)
    bundle["proposed_slots"] = _parse_json_list(bundle.get("proposed_slots"))
    bundle["holds"] = _fetch_holds(conn, proposal_id)
    return bundle


def _insert_approval(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    decision: str,
    decision_source: str,
    authorized_by: str,
    modification_notes: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO approvals (
            proposal_id, decision, decision_source, authorized_by, modification_notes
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            decision,
            decision_source,
            authorized_by,
            modification_notes,
        ),
    )


def _set_proposal_status(conn: sqlite3.Connection, proposal_id: int, status: str) -> None:
    conn.execute(
        """
        UPDATE proposals
        SET status = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (status, proposal_id),
    )


def _resolve_selected_slot(proposal: dict[str, Any], selected_slot: str) -> dict[str, str]:
    selected_slot = (selected_slot or "").strip()
    if not selected_slot:
        raise ValueError("selected_slot is required for approved/modified decisions.")

    try:
        parsed = json.loads(selected_slot)
        if isinstance(parsed, dict) and parsed.get("start"):
            start_norm = _normalize_slot_token(str(parsed["start"]))
            for slot in proposal.get("proposed_slots") or []:
                if _normalize_slot_token(str(slot.get("start", ""))) == start_norm:
                    return {"start": str(slot["start"]), "end": str(slot["end"])}
            for hold in proposal.get("holds") or []:
                if _normalize_slot_token(str(hold.get("slot_start", ""))) == start_norm:
                    return {
                        "start": str(hold["slot_start"]),
                        "end": str(hold["slot_end"]),
                    }
            return _slot_with_derived_end(
                proposal, str(parsed["start"]), str(parsed.get("end") or "")
            )
    except json.JSONDecodeError:
        pass

    normalized = _normalize_slot_token(selected_slot)
    for slot in proposal.get("proposed_slots") or []:
        if _normalize_slot_token(slot.get("start", "")) == normalized:
            return {"start": str(slot["start"]), "end": str(slot["end"])}

    for hold in proposal.get("holds") or []:
        if _normalize_slot_token(str(hold.get("slot_start", ""))) == normalized:
            return {
                "start": str(hold["slot_start"]),
                "end": str(hold["slot_end"]),
            }

    if "start" in selected_slot and "end" in selected_slot:
        match = re.search(
            r'"start"\s*:\s*"([^"]+)".*?"end"\s*:\s*"([^"]+)"',
            selected_slot,
            flags=re.DOTALL,
        )
        if match:
            return _slot_with_derived_end(proposal, match.group(1), match.group(2))

    raise ValueError(f"Could not resolve selected_slot against proposal {proposal['id']}.")


def _slot_with_derived_end(
    proposal: dict[str, Any], start_str: str, end_str: str = ""
) -> dict[str, str]:
    """A novel time can arrive without a usable end — modify_and_approve sent
    end == start, which booked a zero-minute meeting. Inherit the offered
    slots' duration (every offered slot in a proposal shares one); 30 minutes
    only when there is nothing to inherit."""
    from datetime import datetime, timedelta

    try:
        start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
    except ValueError:
        # Unparseable start: hand back what we got — downstream validation owns it.
        return {"start": str(start_str), "end": str(end_str or start_str)}
    if end_str:
        try:
            end_dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
            if end_dt > start_dt:
                return {"start": str(start_str), "end": str(end_str)}
        except ValueError:
            pass
    minutes = 30
    for slot in proposal.get("proposed_slots") or []:
        try:
            s = datetime.fromisoformat(str(slot["start"]).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(slot["end"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if e > s:
            minutes = int((e - s).total_seconds() // 60)
            break
    return {
        "start": str(start_str),
        "end": (start_dt + timedelta(minutes=minutes)).isoformat(),
    }


def _normalize_slot_token(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _confirm_time_conflict(
    proposal: dict[str, Any], selected_slot: dict[str, str]
) -> dict[str, Any] | None:
    """Re-check the chosen slot against the calendar at the moment of booking.

    The calendar is authoritative when the invite is created, not when the offer
    was composed — anything can land on the slot in between. Returns None when the
    slot is still free, or a description of the clash when it is not.

    Lexi's own holds for this proposal are excluded: they sit on the calendar as
    busy blocks precisely to protect this slot, so counting them would make every
    confirmation look like a conflict with itself.

    Fails CLOSED. If the calendar cannot be read we refuse to confirm, because the
    whole point of this check is to not double-book Kory, and "I could not look"
    is not evidence that the slot is free.
    """
    from app.integrations.outlook_calendar import has_conflict

    start = str(selected_slot.get("start") or "").strip()
    end = str(selected_slot.get("end") or "").strip()
    if not start or not end:
        return None  # nothing to check against; the confirm path handles bad slots

    own_hold_ids = [
        str(hold.get("event_id") or "").strip()
        for hold in (proposal.get("holds") or [])
        if str(hold.get("event_id") or "").strip()
    ]

    try:
        conflict, events, _ = has_conflict(
            {"start": start, "end": end}, ignore_event_ids=own_hold_ids
        )
    except Exception as exc:  # noqa: BLE001 — see fail-closed note above
        return {
            "error": (
                f"Could not re-check the calendar before confirming: "
                f"{type(exc).__name__}: {exc}"
            ),
            "kory_message": (
                "I couldn't check your calendar just now, so I haven't sent the invite — "
                "I won't book a slot I can't verify is still free. The holds are still in "
                "place. Worth trying again in a moment."
            ),
            "conflicting_events": [],
        }

    if not conflict:
        return None

    labels = []
    for event in events[:3]:
        subject = str(event.get("subject") or "Untitled").strip()
        when = str((event.get("start") or {}).get("dateTime") or "").strip()
        labels.append(f"**{subject}**" + (f" ({when})" if when else ""))
    joined = ", ".join(labels) if labels else "something new"

    return {
        "error": (
            f"Slot {start} is no longer free — {len(events)} conflicting event(s) "
            "appeared after the offer was sent. Invite not created."
        ),
        # Only offer what actually works. There is deliberately no override flag —
        # that would be another model-supplied boolean Hermes could set by itself,
        # on the one guard standing between a stale offer and a double-booking.
        # Clearing the clash and re-approving resolves it and leaves the calendar
        # honest, so that is what gets offered.
        "kory_message": (
            f"Heads up — {joined} landed on your calendar after I offered that time, "
            "so I have **not** sent the invite and nothing is double-booked. Your holds "
            "are still in place.\n\n"
            "Want that time anyway? Clear the conflicting event and tell me to send it "
            "again. Otherwise say the word and I'll offer new times."
        ),
        "conflicting_events": events[:5],
    }


def _confirm_selected_hold(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    proposal: dict[str, Any],
    selected_slot: dict[str, str],
    result: ExecutionResult,
) -> tuple[str | None, list[str]]:
    from app.scheduling.invite_builder import build_invite_action
    from app.scheduling.timezone_intel import lookup_recipient_timezone
    from app.config import settings
    from zoneinfo import ZoneInfo

    errors: list[str] = []
    matched_hold = _match_hold_for_slot(proposal.get("holds") or [], selected_slot)
    attendee = _extract_email(proposal.get("sender"))
    tz_result = lookup_recipient_timezone(
        sender_email=attendee,
        body=str(proposal.get("raw_body") or ""),
        stored_timezone=str(proposal.get("recipient_timezone") or "") or None,
        for_scheduling=True,
    )
    format_tz = (
        tz_result.timezone
        if tz_result.timezone and tz_result.confidence != "unknown"
        else ZoneInfo(settings.scheduling_timezone)
    )
    extras = selected_slot.get("extra_attendees") if isinstance(selected_slot, dict) else None
    invite_action = build_invite_action(
        slot=selected_slot,
        meeting_subject=proposal.get("subject"),
        intent=proposal.get("intent_classification"),
        attendee_email=attendee,
        sender_display=proposal.get("sender"),
        body=str(proposal.get("raw_body") or ""),
        extra_attendees=list(extras) if extras else None,
        recipient_timezone=format_tz,
    )

    if matched_hold:
        event_id = _confirm_hold_event(
            hold=matched_hold,
            invite_action=invite_action,
            result=result,
        )
        if event_id and _is_mock_event_id(str(matched_hold.get("event_id", ""))):
            conn.execute(
                "UPDATE holds SET event_id = ? WHERE id = ?",
                (event_id, matched_hold["id"]),
            )
        return event_id, errors

    try:
        event_id, _log_id = create_calendar_event(invite_action)
        if not event_id:
            errors.append("Composio did not return a confirmed calendar event id.")
            result.warnings = (result.warnings or []) + [
                "Calendar confirmation returned no event id; marked locally only."
            ]
        return event_id, errors
    except Exception as exc:
        errors.append(f"Calendar confirmation failed: {type(exc).__name__}: {exc}")
        _insert_audit_log(
            conn,
            step_name="execution_dispatch",
            reference_id=str(proposal_id),
            log_level="WARNING",
            message="Calendar invite failed.",
            payload={"proposal_id": proposal_id, "error": str(exc)},
        )
        return None, errors


def _confirm_hold_event(
    *,
    hold: dict[str, Any],
    invite_action: dict[str, Any],
    result: ExecutionResult,
) -> str | None:
    event_id = str(hold.get("event_id") or "")

    if _is_mock_event_id(event_id):
        try:
            confirmed_id, _ = create_calendar_event(invite_action)
            return confirmed_id
        except Exception as exc:
            result.warnings = (result.warnings or []) + [
                f"Mock hold could not be promoted to Outlook event: {exc}"
            ]
            return None

    try:
        delete_calendar_event(event_id)
    except Exception as exc:
        result.warnings = (result.warnings or []) + [
            f"Could not delete tentative hold {event_id}: {exc}"
        ]

    try:
        confirmed_id, _ = create_calendar_event(invite_action)
        return confirmed_id or event_id
    except Exception as exc:
        result.warnings = (result.warnings or []) + [
            f"Hold conversion failed; retaining reference {event_id}: {exc}"
        ]
        return event_id


def _release_all_holds(
    conn: sqlite3.Connection,
    proposal_id: int,
    result: ExecutionResult,
) -> int:
    holds = _fetch_holds(conn, proposal_id)
    released = 0
    for hold in holds:
        if _release_hold(conn, hold, result):
            released += 1
    return released


def _release_unused_holds(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    keep_event_id: str | None,
    result: ExecutionResult,
) -> int:
    holds = _fetch_holds(conn, proposal_id)
    released = 0
    for hold in holds:
        event_id = str(hold.get("event_id") or "")
        if keep_event_id and event_id == keep_event_id:
            continue
        if _release_hold(conn, hold, result):
            released += 1
    return released


def _release_hold(
    conn: sqlite3.Connection,
    hold: dict[str, Any],
    result: ExecutionResult,
) -> bool:
    event_id = str(hold.get("event_id") or "")
    if event_id and not _is_mock_event_id(event_id):
        try:
            delete_calendar_event(event_id)
        except Exception as exc:
            result.warnings = (result.warnings or []) + [
                f"Failed to delete Outlook hold {event_id}: {exc}"
            ]
    conn.execute("DELETE FROM holds WHERE id = ?", (hold["id"],))
    return True


def _asana_booking_reminder_exists(conn: sqlite3.Connection, thread_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM audit_log
        WHERE step_name = 'asana_booking_reminder' AND reference_id = ?
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    return row is not None


def _dispatch_asana_reservation_reminder_if_needed(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    proposal: dict[str, Any],
    time_slot: str,
    result: ExecutionResult,
) -> None:
    """After a sent email: task on Kory NON-IFG → Reservation Reminders when booking needed."""
    from app.config import settings

    if not settings.asana_enabled:
        return

    intent = str(proposal.get("intent_classification") or "").lower()
    thread_id = str(proposal.get("thread_id") or "")
    if thread_id and _asana_booking_reminder_exists(conn, thread_id):
        return

    meeting_subject = str(proposal.get("subject") or "Meeting")
    participants = str(proposal.get("sender") or "unknown")
    drafted_reply = str(proposal.get("drafted_reply") or "")
    raw_body = str(proposal.get("raw_body") or "")

    try:
        from app.integrations.asana_manager import dispatch_reservation_reminder_for_proposal

        asana_result = dispatch_reservation_reminder_for_proposal(
            intent=intent,
            meeting_subject=meeting_subject,
            thread_id=thread_id or str(proposal_id),
            sender=participants,
            drafted_reply=drafted_reply,
            raw_body=raw_body,
            time_slot=time_slot,
            approved=True,
        )
        if not asana_result:
            return

        log_level = "INFO" if asana_result.get("ok") else "ERROR"
        _insert_audit_log(
            conn,
            step_name="asana_task_dispatch",
            reference_id=str(proposal_id),
            log_level=log_level,
            message=(
                f"Asana reservation reminder {'created' if asana_result.get('ok') else 'failed'} "
                f"after send (intent={intent})."
            ),
            payload={
                "proposal_id": proposal_id,
                "intent_classification": intent,
                "asana_result": asana_result,
                "calendar_event_id": result.calendar_event_id,
            },
        )
        if thread_id:
            _insert_audit_log(
                conn,
                step_name="asana_booking_reminder",
                reference_id=thread_id,
                log_level=log_level,
                message="Asana reservation reminder linked to thread.",
                payload={"proposal_id": proposal_id, "asana_result": asana_result},
            )
        if not asana_result.get("ok"):
            result.warnings = (result.warnings or []) + [
                f"Asana task not created: {asana_result.get('error') or 'unknown error'}"
            ]
    except Exception as exc:
        _insert_audit_log(
            conn,
            step_name="asana_task_dispatch",
            reference_id=str(proposal_id),
            log_level="ERROR",
            message="Asana reservation dispatch raised an exception.",
            payload={
                "proposal_id": proposal_id,
                "intent_classification": intent,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "calendar_event_id": result.calendar_event_id,
            },
        )
        result.warnings = (result.warnings or []) + [
            f"Asana dispatch failed (email send preserved): {type(exc).__name__}: {exc}"
        ]


def _send_drafted_reply(
    proposal: dict[str, Any],
    result: ExecutionResult,
) -> tuple[bool, str | None]:
    body = (proposal.get("drafted_reply") or "").strip()
    if not body:
        return False, "No drafted_reply available on proposal."

    thread_id = str(proposal.get("thread_id") or "").strip()
    if thread_id.startswith("demo-"):
        result.warnings = (result.warnings or []) + [
            "Email dispatch skipped: demo-only thread."
        ]
        return False, None
    if not thread_id:
        # A missing thread_id in production is a broken proposal, not a demo —
        # return an error so the needs_kory escalation fires instead of the
        # approval quietly "succeeding" with nothing sent.
        return False, "Email dispatch failed: proposal has no thread_id to reply to."

    try:
        intended = _extract_email(proposal.get("sender"))
        send_channel = str(proposal.get("send_channel") or "kory").strip().lower()
        if send_channel not in {"kory", "lexi"}:
            send_channel = "kory"
        reply_target = str(proposal.get("reply_message_id") or "").strip() or thread_id

        # Chat-initiated outbound proposals carry a synthetic thread id — there
        # is no Outlook message to reply to, and CREATE_DRAFT_REPLY rejects the
        # placeholder as a malformed id (live: proposal 7997, "set up a call
        # with X" could never send its offer). Compose a fresh email instead.
        if thread_id.startswith("lexi-outbound-"):
            if not intended:
                return False, "Email dispatch failed: outbound proposal has no recipient."
            message_id, _log = send_outbound_email(
                to_email=intended,
                subject=str(proposal.get("subject") or "Scheduling"),
                body=body,
                approved_send=True,
                send_channel=send_channel,  # type: ignore[arg-type]
            )
            if not message_id:
                return False, "Email dispatch failed: outbound send returned no message id."
            return True, None

        if settings.sandbox_email_loopback and settings.lexi_write_mode == "sandbox":
            message_id, _log = send_pilot_reply_for_proposal(
                original_subject=proposal.get("subject"),
                body=body,
                intended_recipient=intended,
                send_channel=send_channel,  # type: ignore[arg-type]
            )
            return bool(message_id), None

        if send_channel == "lexi":
            message_id, _log = send_reply_in_thread(
                reply_target,
                body,
                send_channel="lexi",
                approved_send=True,
                conversation_id=str(proposal.get("conversation_id") or "").strip() or None,
                intended_recipient=intended,
            )
            return bool(message_id), None

        draft_id, _draft_log = create_draft_reply(thread_id, body, send_channel="kory")
        if not draft_id:
            return False, "Composio created no draft message id for reply."
        send_draft(draft_id, send_channel="kory")
        return True, None
    except Exception as exc:
        return False, f"Email dispatch failed: {type(exc).__name__}: {exc}"


def _match_hold_for_slot(
    holds: list[dict[str, Any]],
    selected_slot: dict[str, str],
) -> dict[str, Any] | None:
    target_start = _normalize_slot_token(selected_slot["start"])
    target_end = _normalize_slot_token(selected_slot["end"])
    for hold in holds:
        if (
            _normalize_slot_token(str(hold.get("slot_start", ""))) == target_start
            and _normalize_slot_token(str(hold.get("slot_end", ""))) == target_end
        ):
            return hold
    return None


def _is_mock_event_id(event_id: str) -> bool:
    return event_id.startswith(MOCK_HOLD_PREFIX)


def _confirmed_event_title(subject: str | None, sender: str | None) -> str:
    if subject:
        return f"Confirmed: {subject}"
    if sender:
        return f"Meeting with {_sender_display(sender)}"
    return "Confirmed meeting"


def _sender_display(sender: str | None) -> str:
    if not sender:
        return "Guest"
    if "@" in sender:
        local = sender.split("@", 1)[0]
        return local.replace(".", " ").replace("_", " ").title()
    return sender


def _extract_email(sender: str | None) -> str | None:
    if not sender:
        return None
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", sender)
    return match.group(0).lower() if match else (sender if "@" in sender else None)


def _parse_json_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _insert_audit_log(
    conn: sqlite3.Connection,
    *,
    step_name: str,
    reference_id: str,
    log_level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            step_name,
            reference_id,
            log_level,
            message,
            json.dumps(payload, default=str),
        ),
    )


# ---------------------------------------------------------------------------
# Hermes MCP server bridge (apply these tools in hermes_mcp_server.py)
# ---------------------------------------------------------------------------
#
# from app.agents.comms_agent import execute_lexi_approval, get_lexi_pending_queue
#
# @mcp.tool()
# def get_lexi_pending_queue_tool() -> str:
#     items = get_lexi_pending_queue()
#     payload = [item.to_dict() for item in items]
#     return json.dumps({
#         "ok": True,
#         "count": len(payload),
#         "queue": payload,
#         "formatted_list": [item.teams_summary_line() for item in items],
#     })
#
# @mcp.tool()
# def execute_lexi_approval_tool(
#     proposal_id: str,
#     decision: str,
#     selected_slot: str,
#     authorized_by: str,
#     modification_notes: str = "",
# ) -> str:
#     result = execute_lexi_approval(
#         proposal_id=int(proposal_id),
#         decision=decision,
#         selected_slot=selected_slot,
#         authorized_by=authorized_by,
#         modification_notes=modification_notes or None,
#     )
#     return json.dumps({"ok": result.ok, **result.to_dict()}, default=str)
