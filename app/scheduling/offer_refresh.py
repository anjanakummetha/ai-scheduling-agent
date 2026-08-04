"""Refresh stored proposal slots + draft from live calendar and email context."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.inbound_reply import is_scheduling_intent
from app.config import settings
from app.scheduling.busy_intervals import slot_conflicts_busy
from app.scheduling.calendar_context import load_scheduling_calendar_context
from app.scheduling.calendar_intelligence import infer_duration_from_email, slot_duration_minutes
from app.scheduling.reply_composer import compose_scheduling_reply
from app.scheduling.scheduling_plan import build_scheduling_plan
from app.scheduling.slot_engine import propose_meeting_slots

logger = logging.getLogger(__name__)

MIN_SLOT_OPTIONS = 2


def prepare_scheduling_offer_for_approval(
    proposal_record: dict[str, Any],
    email_record: dict[str, Any],
    holds_list: list[dict[str, Any]] | None = None,
    *,
    persist: bool = True,
) -> tuple[dict[str, Any] | None, bool]:
    """Rebuild slots + draft from email context before Kory sees the card.

    Returns (proposal_record, ready). When ready is False, do not notify Teams.
    """
    holds = holds_list or []
    intent = str(proposal_record.get("intent_classification") or "")

    # Only refresh when intent is explicitly a scheduling intent. Avoid surprising
    # live calendar reads during card rendering for generic drafts/tests.
    if holds or not intent.strip() or not is_scheduling_intent(intent):
        return proposal_record, True

    # Keep Hermes-composed drafts from delegation — do not regenerate on card open.
    existing_draft = str(proposal_record.get("drafted_reply") or "").strip()
    existing_slots = proposal_record.get("proposed_slots")
    if existing_draft and existing_slots:
        try:
            parsed = json.loads(existing_slots) if isinstance(existing_slots, str) else existing_slots
            required = 1 if _proposal_guidance(proposal_record) else MIN_SLOT_OPTIONS
            if isinstance(parsed, list) and len(parsed) >= required:
                return proposal_record, True
        except (TypeError, json.JSONDecodeError):
            pass

    updated, ok = refresh_proposal_scheduling_offer(
        proposal_record,
        email_record,
        persist=persist,
    )
    if not ok:
        return None, False
    return updated, True


def _proposal_guidance(proposal_record: dict[str, Any]) -> str:
    """Kory's per-proposal guidance — from the record, or the DB when the
    caller's record shape omits the column (several call sites do)."""
    guidance = str(proposal_record.get("kory_scheduling_guidance") or "").strip()
    if guidance or not proposal_record.get("id"):
        return guidance
    try:
        from app.storage.lexi_db import get_lexi_connection

        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT kory_scheduling_guidance FROM proposals WHERE id = ?",
                (int(proposal_record["id"]),),
            ).fetchone()
        return str(row["kory_scheduling_guidance"] or "").strip() if row else ""
    except Exception:  # noqa: BLE001 — guidance lookup must never break rendering
        return ""


def refresh_proposal_scheduling_offer(
    proposal_record: dict[str, Any],
    email_record: dict[str, Any],
    *,
    persist: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Recompute slots + draft from email context and Master calendar truth."""
    subject = str(email_record.get("subject") or "")
    body = str(email_record.get("raw_body") or "")
    intent = str(proposal_record.get("intent_classification") or "")
    voice_mode = str(proposal_record.get("voice_mode") or "kory")
    thread_id = str(proposal_record.get("thread_id") or "")
    guidance = _proposal_guidance(proposal_record)

    if guidance:
        # Kory's directive must shape the re-run, or this card-time refresh
        # re-schedules from the original email alone and undoes the exception
        # the whole retry chain just honored (live I-2, seventh copy).
        from app.scheduling.schedule_from_context import merge_scheduling_body

        body = merge_scheduling_body(body, guidance)

    plan = build_scheduling_plan(
        subject=subject,
        body=body,
        intent=intent,
        use_llm=bool(settings.llm_api_key),
    )
    plan.kory_guidance = guidance
    if plan.task_type != "offer_times":
        return proposal_record, True

    try:
        calendar_context = load_scheduling_calendar_context(subject=subject, body=body)
    except Exception as exc:
        logger.warning("offer refresh calendar load failed: %s", exc)
        return proposal_record, False

    if calendar_context.get("status") != "available":
        logger.warning(
            "offer refresh blocked — calendar unavailable for proposal %s",
            proposal_record.get("id"),
        )
        return proposal_record, False

    engine = propose_meeting_slots(
        calendar_context,
        intent=intent,
        subject=subject,
        body=body,
        plan=plan,
    )
    if len(engine.slots) < (1 if guidance else MIN_SLOT_OPTIONS):
        logger.warning(
            "offer refresh found insufficient slots for proposal %s: %s",
            proposal_record.get("id"),
            engine.diagnostics,
        )
        return proposal_record, False

    slots = engine.slots[:3]
    busy = list(calendar_context.get("busy_events") or [])
    if busy and any(slot_conflicts_busy(slot, busy) for slot in slots):
        logger.warning(
            "offer refresh produced conflicting slots for proposal %s — blocking card",
            proposal_record.get("id"),
        )
        return proposal_record, False

    draft, _ = compose_scheduling_reply(
        proposal_sender=str(email_record.get("sender") or "") or None,
        proposal_subject=subject,
        proposal_body=body,
        thread_id=thread_id,
        slots=slots,
        voice_mode=voice_mode,
        stored_recipient_timezone=str(proposal_record.get("recipient_timezone") or "") or None,
        plan=plan,
        intent=intent,
    )

    from app.scheduling.pre_approval_gate import verify_before_kory_approval
    from app.scheduling.slot_engine import infer_meeting_format

    type_key = engine.intent
    meeting_format = engine.meeting_format or infer_meeting_format(
        type_key, subject=subject, body=body
    )
    gate = verify_before_kory_approval(
        slots=slots,
        calendar_context=calendar_context,
        plan=plan,
        intent=intent,
        subject=subject,
        body=body,
        meeting_format=meeting_format,
    )
    if not gate.ok:
        logger.warning(
            "offer refresh blocked by pre-approval gate for proposal %s: %s",
            proposal_record.get("id"),
            gate.summary(),
        )
        return proposal_record, False

    updated = {
        **proposal_record,
        "proposed_slots": slots,
        "drafted_reply": draft,
        "meeting_type_key": gate.meeting_type_key,
        "meeting_type_label": gate.meeting_type_label,
        "rules_status": gate.rules_status_line(),
    }
    if persist and proposal_record.get("id"):
        _persist_offer(int(proposal_record["id"]), slots, draft)
    return updated, True


def _stored_offer_is_valid(
    proposal_record: dict[str, Any],
    email_record: dict[str, Any],
) -> bool:
    """Fallback when live calendar is unavailable — only show stored offer if it looks sane."""
    slots = _normalize_slots(proposal_record.get("proposed_slots"))
    if len(slots) < MIN_SLOT_OPTIONS:
        return False
    draft = str(proposal_record.get("drafted_reply") or "")
    voice_mode = str(proposal_record.get("voice_mode") or "kory")
    if _draft_needs_repair(draft, slots, voice_mode):
        return False
    subject = str(email_record.get("subject") or "")
    body = str(email_record.get("raw_body") or "")
    intent = str(proposal_record.get("intent_classification") or "")
    plan = build_scheduling_plan(
        subject=subject,
        body=body,
        intent=intent,
        use_llm=False,
    )
    expected = infer_duration_from_email(
        subject=subject,
        body=body,
        intent=intent,
        plan_duration_minutes=plan.duration_minutes,
    )
    actual = slot_duration_minutes(slots[0])
    return not actual or actual == expected


def _draft_needs_repair(draft: str, slots: list[dict[str, str]], voice_mode: str) -> bool:
    text = (draft or "").strip()
    if not text:
        return True
    if not slots:
        return False
    if "•" not in text and "- " not in text:
        return True
    if text.lower().startswith("subject:"):
        return True
    lower = text.lower()
    if "hi — i'm lexi" in lower or "hi - i'm lexi" in lower:
        return True
    if "a few times that work for scheduling" in lower:
        return True
    if voice_mode.lower() == "lexi" and "on my end" in lower and "kory's end" not in lower:
        return True
    if slots and "i have a few times for" not in lower and "a few options that work on kory's end" in lower:
        return True
    if not _draft_reflects_slots(text, slots):
        return True
    return False


def _draft_reflects_slots(draft: str, slots: list[dict[str, str]]) -> bool:
    from datetime import datetime

    for slot in slots[:3]:
        start = str(slot.get("start") or "")
        if not start:
            continue
        try:
            day = datetime.fromisoformat(start.replace("Z", "+00:00")).strftime("%A")
        except ValueError:
            continue
        if day not in draft:
            return False
    return True


def _normalize_slots(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [slot for slot in raw if isinstance(slot, dict) and slot.get("start")]


def _persist_offer(proposal_id: int, slots: list[dict[str, str]], draft: str) -> None:
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET proposed_slots = ?, drafted_reply = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (json.dumps(slots), draft, proposal_id),
        )
        conn.commit()
