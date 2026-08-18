"""Hermes orchestration — context + mandatory engine + draft staging (no send)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.scheduling.reply_composer import compose_scheduling_reply
from app.scheduling.schedule_from_context import ScheduleFromContextResult, schedule_from_context
from app.scheduling.session_sync import sync_scheduling_session_for_proposal
from app.scheduling.proposal_state import ProposalStatus, transition
from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)

PENDING_APPROVAL = ProposalStatus.PENDING_APPROVAL


def orchestrate_scheduling_from_email(
    *,
    subject: str = "",
    body: str = "",
    intent: str | None = None,
    sender_email: str | None = None,
    thread_id: str = "",
    voice_mode: str = "lexi",
    stored_recipient_timezone: str | None = None,
    kory_scheduling_guidance: str = "",
    compose_draft: bool = True,
    use_template_only: bool = False,
    calendar_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hermes scheduling task for any inbound/chat context — engine always runs first."""
    result = schedule_from_context(
        subject=subject,
        body=body,
        intent=intent,
        sender_email=sender_email,
        kory_scheduling_guidance=kory_scheduling_guidance,
        stored_recipient_timezone=stored_recipient_timezone,
        # None = settings-driven: the LLM plan interpreter runs when an API key
        # exists. This was pinned False, which left every live proposal parsing
        # requests with regex alone — the direct cause of the C-1/C-2 window
        # misses. The regex parser still runs first and remains the fallback
        # whenever the LLM call fails or returns nothing usable.
        use_llm_plan=None,
        calendar_context=calendar_context,
    )

    payload: dict[str, Any] = {
        "ok": result.ok,
        "scheduling": result.to_dict(),
        "path": result.path,
        "status": result.status,
    }

    if not result.ok:
        payload["error"] = result.failure_message or result.status
        payload["kory_message"] = _kory_failure_message(result)
        return payload

    draft = ""
    draft_source = ""
    if compose_draft and result.slots:
        if use_template_only:
            from app.scheduling.email_format import build_scheduling_reply, sender_first_name

            draft = build_scheduling_reply(
                recipient_first_name=sender_first_name(sender_email),
                slots=result.slots,
                sender_email=sender_email,
                subject=subject,
                recipient_body=body,
                voice_mode=voice_mode,
                stored_recipient_timezone=stored_recipient_timezone or result.recipient_timezone,
                intent=intent,
            )
            draft_source = "template"
        else:
            draft, draft_source = compose_scheduling_reply(
                proposal_sender=sender_email,
                proposal_subject=subject,
                proposal_body=body,
                thread_id=thread_id,
                slots=result.slots,
                voice_mode=voice_mode,
                stored_recipient_timezone=stored_recipient_timezone or result.recipient_timezone,
                plan=result.plan,
                intent=intent,
            )

    payload.update(
        {
            "slots": result.slots,
            "formatted_slots": result.formatted_slots,
            "drafted_reply": draft,
            "draft_source": draft_source,
            "recipient_timezone": result.recipient_timezone,
            "timezone_uncertain": result.timezone_uncertain,
            "scheduling_note": result.scheduling_note(),
            "kory_message": "Draft is ready — review the times on the card.",
        }
    )
    return payload


def orchestrate_proposal_scheduling(
    proposal_id: int,
    bundle: dict[str, Any],
    *,
    compose_draft: bool = True,
) -> dict[str, Any]:
    """Run unified scheduling for a triaged proposal; persist draft when successful."""
    sync_scheduling_session_for_proposal(proposal_id)

    voice_mode = str(bundle.get("voice_mode") or "lexi").lower()
    subject = str(bundle.get("subject") or "")
    body = str(bundle.get("raw_body") or "")
    intent = str(bundle.get("intent_classification") or "") or None
    thread_id = str(bundle.get("thread_id") or "")
    sender = bundle.get("sender")
    stored_tz = str(bundle.get("recipient_timezone") or "") or None
    guidance = str(bundle.get("kory_scheduling_guidance") or "")

    outcome = orchestrate_scheduling_from_email(
        subject=subject,
        body=body,
        intent=intent,
        sender_email=str(sender or "") or None,
        thread_id=thread_id,
        voice_mode=voice_mode,
        stored_recipient_timezone=stored_tz,
        kory_scheduling_guidance=guidance,
        compose_draft=compose_draft,
    )

    outcome["proposal_id"] = proposal_id
    outcome["voice_mode"] = voice_mode

    if outcome.get("ok") and compose_draft and outcome.get("drafted_reply"):
        _persist_proposal_draft(
            proposal_id,
            draft=str(outcome["drafted_reply"]),
            slots=list(outcome.get("slots") or []),
            voice_mode=voice_mode,
            recipient_timezone=str(outcome.get("recipient_timezone") or "") or None,
            scheduling_note=str(outcome.get("scheduling_note") or ""),
        )
        outcome["status"] = PENDING_APPROVAL
        return outcome

    if not outcome.get("ok"):
        from app.scheduling.kory_escalation import escalate_to_kory

        return escalate_to_kory(
            proposal_id,
            failure_error=str(outcome.get("error") or outcome.get("kory_message") or "scheduling_failed"),
        )

    return outcome


def preview_scheduling_draft(
    *,
    subject: str = "",
    body: str = "",
    intent: str | None = None,
    sender_email: str | None = None,
    calendar_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dry-run scheduling + template draft (no DB writes, no sends, no holds)."""
    return orchestrate_scheduling_from_email(
        subject=subject,
        body=body,
        intent=intent,
        sender_email=sender_email,
        compose_draft=True,
        use_template_only=True,
        calendar_context=calendar_context,
    )


def _persist_proposal_draft(
    proposal_id: int,
    *,
    draft: str,
    slots: list[dict[str, str]],
    voice_mode: str,
    recipient_timezone: str | None,
    scheduling_note: str = "",
) -> None:
    with get_lexi_connection() as conn:
        transition(
            conn,
            proposal_id,
            to=PENDING_APPROVAL,
            reason=f"Hermes staged {len(slots)} slot(s); awaiting Kory's approval.",
            actor="lexi",
            fields={
                "drafted_reply": draft,
                "proposed_slots": json.dumps(slots, default=str),
                "voice_mode": voice_mode,
                # A fresh draft carries its fresh caveat — or clears a stale one.
                "scheduling_note": scheduling_note.strip() or None,
            },
            coalesce_fields={"recipient_timezone": recipient_timezone},
        )
        conn.commit()


def _kory_failure_message(result: ScheduleFromContextResult) -> str:
    if result.path == "inbound_availability" and result.status == "inbound_times_invalid":
        return result.failure_message or "Their proposed times don't work — reply here with how you'd like to handle it."
    if result.status == "calendar_unavailable":
        return "I couldn't read the calendar right now — try again shortly."
    if result.status == "insufficient_slots":
        return "No valid slots in that window — try a different week?"
    return result.failure_message or "Scheduling couldn't complete — review the diagnostics."
