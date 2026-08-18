"""Follow-up emails on threads where Lexi is already involved — context-aware Teams pings."""

from __future__ import annotations

import json

import logging
import re
from typing import Any

from app.config import settings
from app.scheduling.proposal_state import LEXI_INVOLVED, ProposalStatus, transition
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

# Sorted so the SQL placeholder order is stable; membership is declared once
# in the state machine.
LEXI_INVOLVED_STATUSES = tuple(sorted(LEXI_INVOLVED))



def _proposal_slots(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """The slots this proposal actually offered."""
    raw = proposal.get("proposed_slots")
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [s for s in parsed if isinstance(s, dict)] if isinstance(parsed, list) else []


def _instant(value: Any) -> datetime | None:
    """Compare times as INSTANTS, never as strings: the same moment is written
    -06:00 by us and +00:00 by their mail client."""
    from app.scheduling.busy_intervals import parse_iso_datetime

    return parse_iso_datetime(str(value or "")) if value else None


def _accepted_offered_slot(
    proposal: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, str] | None:
    """The offered slot they just accepted, if their reply names one.

    Returns the slot AS WE OFFERED IT, not as they wrote it, so the booking uses
    the times we validated and hold.
    """
    for offered in _proposal_slots(proposal):
        offered_at = _instant(offered.get("start"))
        if not offered_at:
            continue
        for cand in candidates:
            cand_at = _instant(cand.get("start"))
            if cand_at and cand_at == offered_at:
                return {
                    "start": str(offered.get("start")),
                    "end": str(offered.get("end") or ""),
                }
    return None


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

    if str(proposal.get("status") or "") == "executed" and (
        _CANCEL_RE.search(body) or _RESCHEDULE_RE.search(body)
    ):
        # A booked meeting being cancelled/moved MUST win over the
        # inbound-time path: "cancel our Wednesday call" mentions a weekday,
        # and the time-suggestion detector would hijack it (live E-8).
        handled = _handle_generic_lexi_followup(raw_email, proposal, body=body)
        if handled is not None:
            return handled

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

    candidates = extract_inbound_time_candidates(
        body,
        default_tz=str(proposal.get("recipient_timezone") or "") or None,
    )
    if not candidates:
        return None

    subject = str(proposal.get("subject") or raw_email.get("subject") or "")
    try:
        calendar_context = load_scheduling_calendar_context(subject=subject, body=body)
    except Exception:
        return None

    if calendar_context.get("status") != "available":
        return None

    # A time WE offered cannot be a conflict when they accept it. We validated
    # it before offering, and we are holding it for them — so our own HOLD event
    # sits on the calendar at exactly that time. Re-validating against a calendar
    # containing our own hold made Lexi refuse the acceptance as "already
    # booked", leaving the meeting unbooked and the hold in place. Live E2E
    # 2026-08-18; it was masked earlier by the calendar cache still holding a
    # pre-hold read, which is why it looked intermittent.
    slot = _accepted_offered_slot(proposal, candidates)
    if slot:
        from app.agents.comms_agent import mark_recipient_slot_choice

        pick = mark_recipient_slot_choice(
            int(proposal["proposal_id"]), slot, reply_body=body
        )
        if pick.get("ok") and settings.lexi_teams_enabled:
            from app.bot.teams_publisher import schedule_teams_invite_prompt_push

            schedule_teams_invite_prompt_push(int(proposal["proposal_id"]))
        return {
            "ok": pick.get("ok", False),
            "action": "recipient_slot_choice",
            "proposal_id": proposal.get("proposal_id"),
            "message": (
                f"**{subject}** — they accepted a time you offered. "
                "Invite card is ready."
            ),
            "selected_slot": slot,
        }

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
        proposal_id = int(proposal["proposal_id"])
        # The recipient effectively declined the offered times by proposing
        # their own: release the stale holds and park the proposal in
        # pending_reoffer so Kory's "retry scheduling for #N — …" can act on it
        # (offer_sent blocks the retry tool by design).
        if str(proposal.get("status") or "") == "offer_sent":
            from app.agents.comms_agent import mark_recipient_reoffer_request

            try:
                mark_recipient_reoffer_request(proposal_id, reply_body=body)
            except Exception:  # noqa: BLE001 — the Kory ping must still go out
                logger.exception(
                    "Could not mark reoffer for proposal %s", proposal_id
                )
        retry_hint = (
            f"Say **retry scheduling for #{proposal_id} — <your times>** to offer "
            "alternatives."
        )
        if alt:
            alt_text = ", ".join(format_slot_for_email(s) for s in alt[:3])
            summary = (
                f"**{subject}** — they asked for a time that's booked ({reason}), but you're "
                f"open {alt_text} on that day. Want me to offer those? {retry_hint}"
            )
        else:
            summary = (
                f"**{subject}** — they suggested a time but it doesn't fit: {reason} "
                f"That day is full — should I offer other days? {retry_hint}"
            )
        _notify_kory_followup(proposal_id, summary=summary, kind="inbound_time_blocked")
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


def _truncate_at_word(text: str, limit: int) -> str:
    """Cut on a word boundary with an ellipsis — a mid-word chop ("I a") reads
    as a glitch in the Teams card (live 2026-08-13)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—-")
    return f"{cut}…"


def _body_preview(body: str, limit: int = 120) -> str:
    """First substantive sentence-ish chunk — skip greeting-only lines
    ('Hi Lexi,'), and join hard-wrapped lines so the preview isn't cut at a
    wrap point."""
    lines = (body or "").strip().splitlines()
    started = False
    collected: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            if started:
                break
            continue
        if not started and re.match(
            r"^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[^a-z]*[a-z]*\s*[,!—-]*$",
            line,
            re.I,
        ):
            continue
        started = True
        collected.append(line)
        if sum(len(c) for c in collected) >= limit:
            break
    if collected:
        return _truncate_at_word(" ".join(collected), limit)
    return _truncate_at_word((body or "").strip(), limit)


# "How about you shoot me some days/times that work for you" (real Curtis
# thread) — the counterpart is asking KORY's side to propose. This must not
# read as an unparseable mystery: the right next step is a fresh offer.
_ASK_US_TO_PROPOSE_RE = re.compile(
    r"(?i)\b(?:shoot|send|give|throw)\s+(?:me|us|over)\s+(?:some\s+)?"
    r"(?:days?|times?|dates?|options|availability|windows)\b"
    r"|\bdays?/times?\s+that\s+work\s+for\s+you\b"
    r"|\bwhat\s+(?:days?|times?)\s+works?\s+(?:best\s+)?for\s+you\b"
    r"|\byour\s+availability\b|\bwhat(?:'s| is)\s+your\s+(?:calendar|schedule)\s+look"
)


def _handle_unparsed_followup(
    raw_email: dict[str, Any],
    proposal: dict[str, Any],
    *,
    body: str,
    prior: dict[str, Any],
) -> dict[str, Any]:
    sender = str(proposal.get("sender") or "them")
    subject = str(proposal.get("subject") or "(no subject)")
    preview = _body_preview(body)
    proposal_id = int(proposal["proposal_id"])
    if _ASK_US_TO_PROPOSE_RE.search(_body_preview(body, limit=400)):
        summary = (
            f"**{subject}** — {sender} asked US to propose times:\n"
            f"\"{preview}\"\n\n"
            f"Want me to send fresh options? Say **retry scheduling for "
            f"#{proposal_id}** (add any days/times to steer it)."
        )
    else:
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
            preview = _body_preview(body)
            proposal_id = int(proposal["proposal_id"])
            summary = (
                f"**{subject}** — {sender} wants to CANCEL the booked meeting:\n"
                f"\"{preview}\"\n\n"
                f"I have NOT touched the calendar — say **cancel meeting "
                f"#{proposal_id}** to cancel it."
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
    preview = _body_preview(body)
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
        moved = transition(
            conn,
            proposal_id,
            to=ProposalStatus.PENDING_TRIAGE,
            reason="Counterpart changed the ask before the offer went out; redrafting.",
            actor="recipient",
            fields={
                "drafted_reply": None,
                "proposed_slots": None,
                "teams_approval_notified_at": None,
            },
        )
        conn.commit()
        if not moved.claimed:
            return None

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
        moved = transition(
            conn,
            proposal_id,
            to=ProposalStatus.PENDING_TRIAGE,
            reason="Counterpart asked to move the booked meeting; drafting new times.",
            actor="recipient",
            fields={
                "drafted_reply": None,
                "proposed_slots": None,
                "recipient_selected_slot": None,
                "intent_classification": "reschedule",
                "teams_approval_notified_at": None,
            },
        )
        conn.commit()
        if not moved.claimed:
            return None

    from app.agents.scheduler_agent import process_proposal_schedule

    # Deliberate new round on a thread that already has a booked meeting. The
    # original invite is NOT touched here — invite dispatch removes it only once
    # a replacement is confirmed, so a dead-end reschedule never costs Kory the
    # meeting he already has.
    if process_proposal_schedule(proposal_id, reoffer=True):
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


# FYI-grade pings ("new reply on a thread") collapse per proposal: several new
# messages on one thread in one poll cycle used to fire one card each (live
# 2026-08-13: one reply split across messages -> three cards in six seconds).
# Decision-grade kinds (cancel_request, inbound_time_blocked, unparsed_reply)
# are never suppressed.
_FYI_PING_KINDS = frozenset({"thread_update"})
_FYI_PING_COOLDOWN_SECONDS = 900
_last_fyi_ping: dict[int, float] = {}


def _notify_kory_followup(proposal_id: int, *, summary: str, kind: str) -> None:
    if not settings.lexi_teams_enabled:
        return
    if kind in _FYI_PING_KINDS:
        import time as _time

        last = _last_fyi_ping.get(proposal_id, 0.0)
        if _time.time() - last < _FYI_PING_COOLDOWN_SECONDS:
            logger.info(
                "Lexi thread follow-up ping (%s) for proposal %s suppressed — "
                "within the per-thread cooldown window.",
                kind,
                proposal_id,
            )
            return
        _last_fyi_ping[proposal_id] = _time.time()
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
                    p.recipient_timezone,
                    p.intent_classification,
                    p.is_delegation,
                    e.sender,
                    e.subject
                FROM proposals AS p
                INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE p.status IN ({placeholders})
                  AND e.conversation_id = ?
                -- Newest-first is wrong when two proposals share a
                -- conversation: a reply answers the offer that was actually
                -- SENT, not a newer draft still waiting on Kory. Live E2E
                -- 2026-08-18 wrote the counterpart's acceptance onto a
                -- pending_approval row and left the real offer untouched, so
                -- the meeting was never booked. Outstanding offers first.
                ORDER BY CASE p.status
                             WHEN 'offer_sent' THEN 0
                             WHEN 'pending_invite' THEN 1
                             WHEN 'pending_reoffer' THEN 2
                             ELSE 3
                         END,
                         p.id DESC
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
                    p.recipient_timezone,
                    p.intent_classification,
                    p.is_delegation,
                    e.sender,
                    e.subject
                FROM proposals AS p
                INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE p.status IN ({placeholders})
                  AND lower(replace(replace(e.subject, 'Re: ', ''), 'RE: ', '')) LIKE ?
                -- Newest-first is wrong when two proposals share a
                -- conversation: a reply answers the offer that was actually
                -- SENT, not a newer draft still waiting on Kory. Live E2E
                -- 2026-08-18 wrote the counterpart's acceptance onto a
                -- pending_approval row and left the real offer untouched, so
                -- the meeting was never booked. Outstanding offers first.
                ORDER BY CASE p.status
                             WHEN 'offer_sent' THEN 0
                             WHEN 'pending_invite' THEN 1
                             WHEN 'pending_reoffer' THEN 2
                             ELSE 3
                         END,
                         p.id DESC
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
