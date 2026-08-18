"""Handle recipient replies after Kory sent a time offer."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.comms_agent import (
    STATUS_OFFER_SENT,
    mark_recipient_reoffer_request,
    mark_recipient_slot_choice,
)
from app.config import settings
from app.scheduling.recipient_slot import match_recipient_slot_choice, recipient_times_rejected
from app.scheduling.thread_matching import (
    find_proposal_for_inbound,
    is_internal_sender,
    same_person as _same_person,
)
from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)


def try_handle_recipient_slot_reply(raw_email: dict[str, Any]) -> dict[str, Any] | None:
    """Route offer_sent thread replies: slot pick, re-offer request, or unparsed."""
    conversation_id = str(raw_email.get("conversation_id") or "").strip()
    message_id = str(raw_email.get("message_id") or raw_email.get("thread_id") or "").strip()
    sender = str(raw_email.get("sender") or "").strip().lower()
    subject = str(raw_email.get("subject") or "").strip()
    body = str(raw_email.get("raw_body") or raw_email.get("body") or "")

    if not body.strip():
        return None

    proposal = _find_offer_sent_proposal(conversation_id, subject=subject)
    if not proposal:
        return None

    original_sender = str(proposal.get("sender") or "").strip().lower()
    if original_sender and sender and not _same_person(sender, original_sender):
        return None

    if _is_kory_sender(sender):
        return None

    slots = _parse_slots(proposal.get("proposed_slots"))
    if not slots:
        return None

    if recipient_times_rejected(body):
        from app.scheduling.inbound_availability import (
            body_looks_like_inbound_availability,
            extract_inbound_time_candidates,
        )

        if body_looks_like_inbound_availability(body) and extract_inbound_time_candidates(
            body,
            default_tz=str(proposal.get("recipient_timezone") or "") or None,
        ):
            # "None of those work — could we do Monday at 1 instead?" is a
            # COUNTER-PROPOSAL, not a plain rejection: hand it to the
            # inbound-time path so the proposed time is calendar-validated
            # (and a busy time escalates to Kory instead of a blind re-offer).
            return {
                "skipped": False,
                "action": "offer_reply_unparsed",
                "proposal_id": proposal.get("proposal_id"),
                "thread_id": message_id,
                "reason": "Rejected offered times and proposed a new one.",
            }
        result = mark_recipient_reoffer_request(
            int(proposal["proposal_id"]),
            reply_body=body,
        )
        if result.get("ok") and settings.lexi_teams_enabled:
            from app.bot.teams_publisher import schedule_teams_reoffer_prompt_push

            schedule_teams_reoffer_prompt_push(int(proposal["proposal_id"]), reply_body=body)
        return {
            "ok": result.get("ok", False),
            "action": "recipient_reoffer_request",
            "proposal_id": proposal.get("proposal_id"),
            "status": result.get("status"),
            "message": "Recipient said offered times do not work — notified Kory.",
        }

    chosen = match_recipient_slot_choice(
        body,
        slots,
        sender_email=sender,
        recipient_tz=str(proposal.get("recipient_timezone") or "") or None,
    )
    if not chosen:
        logger.info(
            "Reply on offer_sent proposal %s but no slot match yet.",
            proposal.get("proposal_id"),
        )
        return {
            "skipped": False,
            "action": "offer_reply_unparsed",
            "proposal_id": proposal.get("proposal_id"),
            "thread_id": message_id,
            "reason": "Could not parse recipient reply — left for Kory review.",
        }

    result = mark_recipient_slot_choice(
        int(proposal["proposal_id"]),
        chosen,
        reply_body=body,
    )
    if not result.get("ok"):
        return result

    if settings.lexi_teams_enabled:
        from app.bot.teams_publisher import schedule_teams_invite_prompt_push

        schedule_teams_invite_prompt_push(int(proposal["proposal_id"]))

    return {
        "ok": True,
        "action": "recipient_slot_selected",
        "proposal_id": proposal["proposal_id"],
        "selected_slot": chosen,
        "status": result.get("status"),
    }


def _find_offer_sent_proposal(
    conversation_id: str,
    *,
    subject: str = "",
) -> dict[str, Any] | None:
    """The sent offer this reply is answering.

    Uses the shared resolver so the ordering and the subject normalisation match
    every other lookup. The old local copy ordered purely by id, and normalised
    subjects with a SQL replace that stripped "Re: " from anywhere in the string.
    """
    with get_lexi_connection() as conn:
        return find_proposal_for_inbound(
            conn,
            conversation_id=conversation_id,
            subject=subject,
            statuses={STATUS_OFFER_SENT},
        )


def _parse_slots(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict) and s.get("start")]
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [s for s in parsed if isinstance(s, dict) and s.get("start")]
    return []


def _is_kory_sender(sender: str) -> bool:
    return is_internal_sender(sender)
