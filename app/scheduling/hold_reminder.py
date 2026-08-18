"""Hold reminder drafts — notify Kory on Teams before sending to prospect."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.scheduling.email_format import format_slot_for_email, recipient_display_name
from app.scheduling.proposal_state import ProposalStatus, transition
from app.storage.lexi_db import get_lexi_connection

import rules as kory_rules

logger = logging.getLogger(__name__)

HOLD_REMINDER_PREFIX = "HOLD_REMINDER"
OFFER_SENT = ProposalStatus.OFFER_SENT
PENDING_APPROVAL = ProposalStatus.PENDING_APPROVAL
RELEASED_STATUS = "released"


def is_hold_reminder_proposal(proposal: dict[str, Any]) -> bool:
    note = str(proposal.get("scheduling_note") or "")
    return note.startswith(HOLD_REMINDER_PREFIX)


def compose_hold_reminder_draft(
    *,
    sender: str,
    subject: str,
    slots: list[dict[str, str]],
    recipient_timezone: str | None = None,
) -> str:
    """Short follow-up when prospect has not picked a time."""
    first_name = recipient_display_name(sender).split()[0] if sender else "there"
    sign_off = kory_rules.EMAIL_RULES.get("sign_off", "Let's Win")

    if slots:
        # Quote the reminder in the same timezone the offer email used —
        # recipient_timezone was fetched by every caller but never applied,
        # so an Eastern contact's reminder silently switched to MT.
        tz = None
        if recipient_timezone:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(str(recipient_timezone))
            except Exception:  # unknown tz label — MT fallback, as before
                tz = None
        slot_lines = [
            format_slot_for_email(slot, recipient_tz=tz) for slot in slots[:3]
        ]
        options = "\n".join(f"- {line}" for line in slot_lines if line)
        body = (
            f"Hi {first_name},\n\n"
            f"Just circling back on scheduling — wanted to make sure the times below still work "
            f"for you:\n\n{options}\n\n"
            f"Happy to adjust if none of these fit.\n\n"
            f"{sign_off},\nLexi\n(on behalf of Kory Mitchell)"
        )
    else:
        body = (
            f"Hi {first_name},\n\n"
            f"Just circling back on scheduling for {subject or 'our meeting'}. "
            f"Let me know what works on your end and I will get something on the calendar.\n\n"
            f"{sign_off},\nLexi\n(on behalf of Kory Mitchell)"
        )
    return body


def effective_reminder_days() -> int:
    """Days after hold creation when the reminder actually fires.

    Not simply `reminder_after_days`: the reminder has to land BEFORE the release,
    so it is clamped to one day short of it. With both rules at 3 that means the
    reminder fires at 2 days, not 3. Kory's Teams card quotes this number, and
    quoting the unclamped rule told him "no reply after 3 days" for something
    that triggers at 2.
    """
    reminder_days = int(kory_rules.HOLD_RULES.get("reminder_after_days", 3))
    release_days = int(kory_rules.HOLD_RULES.get("release_hold_after_days", 3))
    return min(reminder_days, max(1, release_days - 1))


def _reminder_due_at(created_at: str, expires_at: str | None) -> datetime | None:
    """When to stage a hold reminder (default: 1 day before hold release)."""
    created = _parse_iso(created_at)
    expires = _parse_iso(expires_at)
    if created:
        return created + timedelta(days=effective_reminder_days())
    if expires:
        return expires - timedelta(days=1)
    return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value or value == RELEASED_STATUS:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _hold_reminder_already_staged(conn, proposal_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM audit_log
        WHERE step_name IN ('hold_reminder_staged', 'hold_reminder_sent')
          AND reference_id = ?
        LIMIT 1
        """,
        (str(proposal_id),),
    ).fetchone()
    return row is not None


def process_due_hold_reminders() -> list[dict[str, Any]]:
    """Stage reminder drafts and notify Kory before prospect follow-up."""
    now = datetime.now(timezone.utc)
    staged: list[dict[str, Any]] = []

    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.proposed_slots,
                p.recipient_timezone,
                p.status,
                e.subject,
                e.sender,
                MIN(h.created_at) AS oldest_hold_at,
                MIN(h.expires_at) AS earliest_expires_at
            FROM proposals AS p
            INNER JOIN holds AS h ON h.proposal_id = p.id
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status = ?
              AND h.expires_at IS NOT NULL
              AND h.expires_at != ?
              AND COALESCE(h.event_id, '') != ''
              AND h.event_id NOT LIKE 'hold-pending-%'
            GROUP BY p.id
            """,
            (OFFER_SENT, RELEASED_STATUS),
        ).fetchall()

        for row in rows:
            proposal_id = int(row["proposal_id"])
            if _hold_reminder_already_staged(conn, proposal_id):
                continue

            due_at = _reminder_due_at(
                str(row["oldest_hold_at"] or ""),
                str(row["earliest_expires_at"] or ""),
            )
            if due_at is None or now < due_at:
                continue

            slots = _parse_slots(row["proposed_slots"])
            draft = compose_hold_reminder_draft(
                sender=str(row["sender"] or ""),
                subject=str(row["subject"] or ""),
                slots=slots,
                recipient_timezone=str(row["recipient_timezone"] or "") or None,
            )

            # offer_sent -> pending_approval, a legal move but NOT a fresh
            # offer: the original email is still in their inbox and the holds
            # are still on the calendar. offer_sent_at stays stamped, so the
            # send path treats an approval here as a follow-up and leaves the
            # holds alone.
            reminder = transition(
                conn,
                proposal_id,
                to=PENDING_APPROVAL,
                expect=OFFER_SENT,
                reason="No reply within the hold period; reminder drafted for Kory.",
                actor="lexi",
                fields={
                    "drafted_reply": draft,
                    "scheduling_note": (
                        f"{HOLD_REMINDER_PREFIX}: No reply after hold period — "
                        "approve to send reminder."
                    ),
                    "teams_approval_notified_at": None,
                },
            )
            if not reminder.claimed:
                continue
            conn.execute(
                """
                INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "hold_reminder_staged",
                    str(proposal_id),
                    "INFO",
                    "Hold reminder draft staged for Kory approval.",
                    json.dumps(
                        {
                            "subject": row["subject"],
                            "sender": row["sender"],
                            "due_at": due_at.isoformat(),
                        },
                        default=str,
                    ),
                ),
            )
            staged.append(
                {
                    "proposal_id": proposal_id,
                    "subject": row["subject"],
                    "sender": row["sender"],
                    "draft_preview": draft[:240],
                }
            )

        if staged:
            conn.commit()

    for item in staged:
        _notify_kory_hold_reminder(item["proposal_id"])

    return staged


def stage_release_followups(proposal_ids: list[int]) -> list[int]:
    """Stage the "holds released — still interested?" prospect follow-up.

    HOLD_RULES says release and re-remind happen together; historically the
    release was silent to the prospect. Same approve-to-send contract as the
    day-2 reminder: this only stages a draft for Kory.
    """
    staged: list[int] = []
    with get_lexi_connection() as conn:
        for proposal_id in proposal_ids:
            row = conn.execute(
                """
                SELECT p.id, p.status, p.recipient_timezone, e.subject, e.sender
                FROM proposals AS p
                INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if not row or str(row["status"]) != OFFER_SENT:
                continue
            already = conn.execute(
                "SELECT 1 FROM audit_log WHERE step_name = 'hold_release_followup_staged' "
                "AND reference_id = ? LIMIT 1",
                (str(proposal_id),),
            ).fetchone()
            if already:
                continue

            # No slots on purpose: the held times are gone; the draft asks for
            # their availability instead of re-quoting released options.
            draft = compose_hold_reminder_draft(
                sender=str(row["sender"] or ""),
                subject=str(row["subject"] or ""),
                slots=[],
                recipient_timezone=str(row["recipient_timezone"] or "") or None,
            )
            followup = transition(
                conn,
                proposal_id,
                to=PENDING_APPROVAL,
                reason="Holds expired with no reply; follow-up drafted for Kory.",
                actor="lexi",
                fields={
                    "drafted_reply": draft,
                    # The holds are gone and this draft quotes no times, so the
                    # staged slots must go with them. Leaving them behind is the
                    # record disagreeing with the world again: the send gate
                    # would re-validate released times, and hold placement would
                    # put fresh holds on slots nobody is being offered.
                    "proposed_slots": None,
                    "scheduling_note": (
                        f"{HOLD_REMINDER_PREFIX}: Holds released after no reply — "
                        "approve to send the follow-up."
                    ),
                    "teams_approval_notified_at": None,
                },
            )
            if not followup.claimed:
                continue
            conn.execute(
                "INSERT INTO audit_log (step_name, reference_id, log_level, message, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "hold_release_followup_staged",
                    str(proposal_id),
                    "INFO",
                    "Release follow-up draft staged for Kory approval.",
                    json.dumps({"subject": row["subject"], "sender": row["sender"]}),
                ),
            )
            staged.append(proposal_id)
        if staged:
            conn.commit()

    for proposal_id in staged:
        _notify_kory_hold_reminder(proposal_id)
    return staged


def _parse_slots(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    try:
        parsed = json.loads(raw)
        return [s for s in parsed if isinstance(s, dict)] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _notify_kory_hold_reminder(proposal_id: int) -> None:
    from app.bot.teams_publisher import schedule_teams_hold_reminder_push

    schedule_teams_hold_reminder_push(proposal_id)
