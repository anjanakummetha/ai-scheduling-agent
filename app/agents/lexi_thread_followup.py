"""Follow-up emails on threads where Lexi is already involved — context-aware Teams pings."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings
from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)

# Recipient asking to MOVE a booked meeting (checked before cancel — "can't make
# Monday, can we find another time" is a reschedule, not a cancellation).
_RESCHEDULE_RE = re.compile(
    r"(?i)\b(re-?schedul\w+|move (?:our|the|this|it)|different (?:time|day|slot)"
    r"|another (?:time|day|slot)|push (?:it|this|back|to)|can'?t make|cannot make"
    r"|something came up|need to (?:change|move)|no longer works?|won'?t work)\b"
)
_CANCEL_RE = re.compile(
    r"(?i)\b(cancel\w*|call (?:it|this) off|scrap (?:it|the meeting)"
    r"|don'?t need (?:the|this) (?:meeting|call) (?:anymore|any more))\b"
)

LEXI_INVOLVED_STATUSES = (
    "offer_sent",
    "pending_invite",
    "pending_reoffer",
    "pending_approval",
    "executed",
    "awaiting_reply_prompt",
)


def try_handle_lexi_thread_followup(raw_email: dict[str, Any]) -> dict[str, Any] | None:
    """Route recipient follow-ups on Lexi-involved threads before cold inbound triage."""
    conversation_id = str(raw_email.get("conversation_id") or "").strip()
    sender = str(raw_email.get("sender") or raw_email.get("sender_email") or "").strip().lower()
    subject = str(raw_email.get("subject") or "").strip()
    body = str(raw_email.get("raw_body") or raw_email.get("body") or "")

    if not body.strip():
        return None

    if _is_kory_sender(sender):
        return None

    proposal = _find_lexi_involved_proposal(conversation_id, subject=subject)
    if not proposal:
        return None

    from app.agents.offer_reply import try_handle_recipient_slot_reply

    offer_result = try_handle_recipient_slot_reply(raw_email)
    if offer_result and offer_result.get("action") in {
        "recipient_slot_selected",
        "recipient_reoffer_request",
    }:
        return offer_result

    if offer_result and offer_result.get("action") == "offer_reply_unparsed":
        # They didn't pick an offered slot — they may be proposing a NEW time
        # ("can he do 9am instead?"). Try the inbound-time path before giving up.
        inbound_result = _try_inbound_time_suggestion(raw_email, proposal, body=body)
        if inbound_result:
            return inbound_result
        return _handle_unparsed_followup(raw_email, proposal, body=body, prior=offer_result)

    inbound_result = _try_inbound_time_suggestion(raw_email, proposal, body=body)
    if inbound_result:
        return inbound_result

    return _handle_generic_lexi_followup(raw_email, proposal, body=body)


def _try_inbound_time_suggestion(
    raw_email: dict[str, Any],
    proposal: dict[str, Any],
    *,
    body: str,
) -> dict[str, Any] | None:
    """Prospect proposes a specific time — validate calendar and notify Kory."""
    from app.scheduling.inbound_availability import (
        body_looks_like_inbound_availability,
        extract_inbound_time_candidates,
        validate_inbound_candidates,
    )
    from app.scheduling.calendar_context import load_scheduling_calendar_context

    if not body_looks_like_inbound_availability(body):
        return None

    candidates = extract_inbound_time_candidates(body)
    if not candidates:
        return None

    subject = str(proposal.get("subject") or raw_email.get("subject") or "")
    try:
        calendar_context = load_scheduling_calendar_context(subject=subject, body=body)
    except Exception:
        return None

    if calendar_context.get("status") != "available":
        return None

    intent = str(proposal.get("intent_classification") or "")
    validated, invalid, notes = validate_inbound_candidates(
        candidates,
        calendar_context=calendar_context,
        intent=intent,
        subject=subject,
        body=body,
    )
    if not validated:
        reason = "; ".join(notes[:2]) if notes else "Calendar conflict or rules violation."
        # The proposed time is busy — look for open times ON the same day so Kory
        # can offer a near alternative instead of restarting (mirrors Heidi picking
        # another time on the day the prospect asked for).
        from app.scheduling.inbound_availability import find_compliant_slots_on_date
        from app.scheduling.email_format import format_slot_for_email

        alt: list[dict[str, str]] = []
        for cand in candidates[:2]:
            for s in find_compliant_slots_on_date(
                cand["start"], calendar_context=calendar_context, intent=intent,
                subject=subject, body=body, near_hour=int(cand["start"][11:13] or 12), limit=2,
            ):
                if s not in alt:
                    alt.append(s)
            if alt:
                break
        if alt:
            alt_text = ", ".join(format_slot_for_email(s) for s in alt[:3])
            summary = (
                f"**{subject}** — they asked for a time that's booked ({reason}), but you're "
                f"open {alt_text} on that day. Want me to offer those?"
            )
        else:
            summary = (
                f"**{subject}** — they suggested a time but it doesn't fit: {reason} "
                f"That day is full — should I offer other days?"
            )
        _notify_kory_followup(int(proposal["proposal_id"]), summary=summary, kind="inbound_time_blocked")
        return {
            "ok": True,
            "action": "inbound_time_blocked",
            "proposal_id": proposal.get("proposal_id"),
            "message": summary,
            "same_day_alternatives": alt,
        }

    slot = validated[0]
    from app.agents.comms_agent import mark_recipient_slot_choice

    pick = mark_recipient_slot_choice(
        int(proposal["proposal_id"]),
        slot,
        reply_body=body,
    )
    if pick.get("ok") and settings.lexi_teams_enabled:
        from app.bot.teams_publisher import schedule_teams_invite_prompt_push

        schedule_teams_invite_prompt_push(int(proposal["proposal_id"]))

    summary = (
        f"**{subject}** — they suggested a time and your calendar looks free. "
        f"Invite card is ready."
    )
    return {
        "ok": pick.get("ok", False),
        "action": "inbound_time_suggested",
        "proposal_id": proposal.get("proposal_id"),
        "selected_slot": slot,
        "status": pick.get("status"),
        "message": summary,
    }


def _handle_unparsed_followup(
    raw_email: dict[str, Any],
    proposal: dict[str, Any],
    *,
    body: str,
    prior: dict[str, Any],
) -> dict[str, Any]:
    sender = str(proposal.get("sender") or "them")
    subject = str(proposal.get("subject") or "(no subject)")
    preview = body.strip().split("\n")[0][:120]
    summary = (
        f"**{subject}** — {sender} replied and I couldn't auto-parse it:\n"
        f"\"{preview}\"\n\n"
        f"Should I draft a follow-up or confirm a time?"
    )
    _notify_kory_followup(int(proposal["proposal_id"]), summary=summary, kind="unparsed_reply")
    return {
        **prior,
        "kory_notified": True,
        "message": summary,
    }


def _handle_generic_lexi_followup(
    raw_email: dict[str, Any],
    proposal: dict[str, Any],
    *,
    body: str,
) -> dict[str, Any] | None:
    status = str(proposal.get("status") or "")
    is_delegation = bool(proposal.get("is_delegation"))
    notify_statuses = {
        "pending_approval",
        "offer_sent",
        "pending_invite",
        "pending_reoffer",
        "executed",
    }
    if status not in notify_statuses:
        return None
    if not is_delegation and status not in {
        "offer_sent",
        "pending_invite",
        "pending_reoffer",
        "executed",
    }:
        return None

    if status == "executed":
        # The meeting is BOOKED — a reply here is the highest-stakes follow-up
        # (live E-7: it used to be dropped in total silence).
        if _CANCEL_RE.search(body) and not _RESCHEDULE_RE.search(body):
            sender = str(proposal.get("sender") or "them")
            subject = str(proposal.get("subject") or "(no subject)")
            preview = body.strip().split("\n")[0][:120]
            summary = (
                f"**{subject}** — {sender} wants to CANCEL the booked meeting:\n"
                f"\"{preview}\"\n\n"
                "I have NOT touched the calendar — tell me if you want the "
                "meeting cancelled."
            )
            _notify_kory_followup(
                int(proposal["proposal_id"]), summary=summary, kind="cancel_request"
            )
            return {
                "ok": True,
                "action": "lexi_thread_followup",
                "proposal_id": proposal.get("proposal_id"),
                "status": status,
                "message": summary,
            }
        if _RESCHEDULE_RE.search(body):
            rescheduled = _reschedule_booked_meeting(proposal, followup_body=body)
            if rescheduled is not None:
                return rescheduled
        # Anything else on a booked thread: fall through to the generic ping.

    if status == "pending_approval":
        # The offer has NOT gone out yet and the sender just changed the ask
        # ("how about Thursday instead?"). A ping alone leaves the stale draft
        # one approve-tap from sending an answer that ignores their reply
        # (live C-5) — regenerate the offer with the follow-up folded in.
        refreshed = _reschedule_unsent_offer(proposal, followup_body=body)
        if refreshed is not None:
            return refreshed

    sender = str(proposal.get("sender") or "them")
    subject = str(proposal.get("subject") or "(no subject)")
    preview = body.strip().split("\n")[0][:120]
    summary = (
        f"**{subject}** — new reply from {sender} on a thread Lexi is handling:\n"
        f"\"{preview}\"\n\n"
        f"Status: {status.replace('_', ' ')}."
    )
    _notify_kory_followup(int(proposal["proposal_id"]), summary=summary, kind="thread_update")
    return {
        "ok": True,
        "action": "lexi_thread_followup",
        "proposal_id": proposal.get("proposal_id"),
        "status": status,
        "message": summary,
    }


def _reschedule_unsent_offer(
    proposal: dict[str, Any],
    *,
    followup_body: str,
) -> dict[str, Any] | None:
    """Fold the sender's follow-up into the thread and regenerate the draft.

    Sender text is appended to the scheduling context — it is deliberately NOT
    routed through kory_scheduling_guidance, which is trusted input that can
    unlock policy exceptions (a sender saying "lunch works!" must never flip
    the lunch rule).
    """
    proposal_id = int(proposal["proposal_id"])
    addition = (followup_body or "").strip()
    if not addition:
        return None

    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT raw_body FROM email_threads WHERE thread_id = "
            "(SELECT thread_id FROM proposals WHERE id = ?)",
            (proposal_id,),
        ).fetchone()
        base = str(row["raw_body"] or "") if row else ""
        if addition not in base:
            merged = f"{base}\n\n[Sender follow-up]: {addition}".strip()
            conn.execute(
                "UPDATE email_threads SET raw_body = ? WHERE thread_id = "
                "(SELECT thread_id FROM proposals WHERE id = ?)",
                (merged, proposal_id),
            )
        conn.execute(
            """
            UPDATE proposals
            SET status = 'pending_triage', drafted_reply = NULL,
                proposed_slots = NULL, teams_approval_notified_at = NULL,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (proposal_id,),
        )
        conn.commit()

    from app.agents.scheduler_agent import process_proposal_schedule

    if process_proposal_schedule(proposal_id):
        from app.bot.teams_publisher import schedule_teams_approval_push

        schedule_teams_approval_push(proposal_id, force=True)
        logger.info(
            "Regenerated unsent offer for proposal %s after sender follow-up.",
            proposal_id,
        )
        return {
            "ok": True,
            "action": "lexi_thread_followup",
            "proposal_id": proposal_id,
            "status": "pending_approval",
            "rescheduled": True,
            "message": "Sender updated the ask before the offer went out — draft regenerated.",
        }
    # Rescheduling failed (e.g. nothing fits the new ask) — fall through to the
    # notify branch so Kory still hears about the reply.
    return None


def _reschedule_booked_meeting(
    proposal: dict[str, Any],
    *,
    followup_body: str,
) -> dict[str, Any] | None:
    """Recipient asked to move a BOOKED meeting — regenerate a reschedule offer.

    The existing invite stays on the calendar untouched; invite dispatch removes
    it (via proposals.invite_event_id) only once the NEW time is confirmed, so a
    dead-end reschedule never costs Kory the original meeting.
    """
    proposal_id = int(proposal["proposal_id"])
    addition = (followup_body or "").strip()
    if not addition:
        return None

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT raw_body FROM email_threads WHERE thread_id = "
            "(SELECT thread_id FROM proposals WHERE id = ?)",
            (proposal_id,),
        ).fetchone()
        base = str(row["raw_body"] or "") if row else ""
        if addition not in base:
            merged = f"{base}\n\n[Sender follow-up]: {addition}".strip()
            conn.execute(
                "UPDATE email_threads SET raw_body = ? WHERE thread_id = "
                "(SELECT thread_id FROM proposals WHERE id = ?)",
                (merged, proposal_id),
            )
        conn.execute(
            """
            UPDATE proposals
            SET status = 'pending_triage', drafted_reply = NULL,
                proposed_slots = NULL, recipient_selected_slot = NULL,
                teams_approval_notified_at = NULL, updated_at = datetime('now')
            WHERE id = ?
            """,
            (proposal_id,),
        )
        conn.commit()

    from app.agents.scheduler_agent import process_proposal_schedule

    if process_proposal_schedule(proposal_id):
        from app.bot.teams_publisher import schedule_teams_approval_push

        schedule_teams_approval_push(proposal_id, force=True)
        logger.info(
            "Reschedule offer regenerated for booked proposal %s.", proposal_id
        )
        return {
            "ok": True,
            "action": "lexi_thread_followup",
            "proposal_id": proposal_id,
            "status": "pending_approval",
            "rescheduled": True,
            "message": (
                "Recipient asked to move the booked meeting — new offer drafted. "
                "The current invite stays until a new time is confirmed."
            ),
        }
    # Regeneration failed (e.g. nothing fits) — fall back to a Kory ping so the
    # ask is never silent.
    sender = str(proposal.get("sender") or "them")
    subject = str(proposal.get("subject") or "(no subject)")
    summary = (
        f"**{subject}** — {sender} asked to move the booked meeting, but I "
        "couldn't draft new times automatically. The original invite is "
        "unchanged — tell me how you'd like to handle it."
    )
    _notify_kory_followup(proposal_id, summary=summary, kind="reschedule_failed")
    return {
        "ok": False,
        "action": "lexi_thread_followup",
        "proposal_id": proposal_id,
        "status": str(proposal.get("status") or ""),
        "message": summary,
    }


def _notify_kory_followup(proposal_id: int, *, summary: str, kind: str) -> None:
    if not settings.lexi_teams_enabled:
        return
    from app.bot.teams_publisher import schedule_teams_scheduling_guidance_push

    schedule_teams_scheduling_guidance_push(proposal_id, summary=summary, force=True)
    logger.info("Lexi thread follow-up Teams ping (%s) for proposal %s", kind, proposal_id)


def _find_lexi_involved_proposal(
    conversation_id: str,
    *,
    subject: str = "",
) -> dict[str, Any] | None:
    placeholders = ",".join("?" * len(LEXI_INVOLVED_STATUSES))
    with get_lexi_connection() as conn:
        if conversation_id:
            row = conn.execute(
                f"""
                SELECT
                    p.id AS proposal_id,
                    p.status,
                    p.proposed_slots,
                    p.intent_classification,
                    p.is_delegation,
                    e.sender,
                    e.subject
                FROM proposals AS p
                INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE p.status IN ({placeholders})
                  AND e.conversation_id = ?
                ORDER BY p.id DESC
                LIMIT 1
                """,
                (*LEXI_INVOLVED_STATUSES, conversation_id),
            ).fetchone()
            if row:
                return dict(row)

        norm = _normalize_subject(subject)
        if norm:
            row = conn.execute(
                f"""
                SELECT
                    p.id AS proposal_id,
                    p.status,
                    p.proposed_slots,
                    p.intent_classification,
                    p.is_delegation,
                    e.sender,
                    e.subject
                FROM proposals AS p
                INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE p.status IN ({placeholders})
                  AND lower(replace(replace(e.subject, 'Re: ', ''), 'RE: ', '')) LIKE ?
                ORDER BY p.id DESC
                LIMIT 1
                """,
                (*LEXI_INVOLVED_STATUSES, f"%{norm[:60]}%"),
            ).fetchone()
            if row:
                return dict(row)
    return None


def _normalize_subject(subject: str) -> str:
    s = (subject or "").strip().lower()
    while s.startswith("re:") or s.startswith("fwd:"):
        s = s.split(":", 1)[1].strip()
    return s


def _is_kory_sender(sender: str) -> bool:
    from app.config import settings

    addr = (sender or "").strip().lower()
    if not addr:
        return False
    if addr in {e.lower() for e in settings.kory_sender_emails}:
        return True
    return any(domain in addr for domain in ("@iconicfounders.com", "@ifg.vc"))
