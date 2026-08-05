"""Lexi Phase 3: schedule pending triage proposals and stage holds for approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import time
import traceback
from typing import Any

from app.config import settings
from app.llm.hermes_client import get_hermes_client
from app.storage.lexi_db import get_lexi_connection

PENDING_TRIAGE = "pending_triage"
PENDING_APPROVAL = "pending_approval"
MIN_SLOT_OPTIONS = 2
MAX_SLOT_OPTIONS = 3

INTENT_TO_MEETING_TYPE: dict[str, str] = {
    "dinner_request": "dinner",
    "lunch_request": "lunch",
    "coffee": "coffee",
    "happy_hour": "happy_hour",
    "pitch": "new_client",
    "internal_sync": "unknown",
    "board_meeting": "unknown",
    "reschedule": "reschedule",
    "cancellation": "unknown",
    "delegation": "unknown",
    "non_scheduling": "unknown",
    "unknown": "unknown",
}

SCHEDULER_SYSTEM_PROMPT = """You are Lexi, Kory's executive scheduling assistant.
Given an inbound email, triage metadata, and busy calendar blocks, propose meeting options.

Return ONLY a valid JSON object with exactly these keys:
- slots: array of 2-3 objects, each with "start" and "end" in ISO-8601 format (include timezone offset).
  Slots MUST NOT overlap any busy block provided. Prefer business hours in America/Denver unless the email implies otherwise.
- drafted_reply: string, a concise email reply written in Kory's voice offering those exact times.
- confidence_score: float between 0.0 and 1.0 reflecting scheduling match quality.

CEO drafting rules (mandatory for drafted_reply):
A) Timezones: Quote each offered time in the recipient's local zone first, then MT in parentheses.
   If recipient timezone is not stated in the email and not confirmed by Kory, do NOT invent times —
   set drafted_reply to a short note asking Kory which timezone to use (slots may be empty).
B) Signature: Always sign off external emails with exactly "Let's Win," on its own line followed by "Kory".
   Never use "Best", "Warmly", "Thanks", or other closings.

Do not include markdown fences or any text outside the JSON object."""


@dataclass(frozen=True)
class PendingProposal:
    proposal_id: int
    thread_id: str
    intent_classification: str | None
    priority_tier: str | None
    triage_confidence: float | None
    justification: str | None
    rule_reasoning: str | None
    subject: str | None
    sender: str | None
    received_at: str | None
    raw_body: str | None
    voice_mode: str = "kory"
    send_channel: str = "kory"
    recipient_timezone: str | None = None
    kory_scheduling_guidance: str | None = None

    def scheduling_body(self) -> str:
        base = (self.raw_body or "").strip()
        guidance = (self.kory_scheduling_guidance or "").strip()
        if guidance:
            return f"{base}\n\nKory (scheduling guidance): {guidance}".strip()
        return base


@dataclass
class ScheduleResult:
    slots: list[dict[str, str]]
    drafted_reply: str
    confidence_score: float
    source: str = "llm"
    scheduling_note: str = ""
    kory_message: str = ""
    suggested_guidance: str | None = None
    window_expanded: bool = False
    # The plan the engine actually scheduled against (already travel-shifted). Threaded
    # to the pre-approval gate so it doesn't re-derive a conflicting, un-shifted window.
    plan: Any = None


def process_proposal_schedule(proposal_id: int) -> bool:
    """Advance one proposal to pending_approval (from awaiting_reply_prompt or pending_triage)."""
    with get_lexi_connection() as conn:
        proposal = _fetch_proposal_by_id(conn, proposal_id)
        if not proposal:
            return False
        status_row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if status_row and status_row["status"] in {"awaiting_reply_prompt", "pending_reoffer"}:
            conn.execute(
                "UPDATE proposals SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (PENDING_TRIAGE, proposal_id),
            )
            conn.commit()
        return _advance_proposal(conn, proposal)


def process_pending_schedules() -> list[int]:
    """Advance pending_triage proposals to pending_approval with slots, reply, and holds."""
    processed_ids: list[int] = []

    with get_lexi_connection() as conn:
        pending = _fetch_pending_proposals(conn)
        if not pending:
            return processed_ids

        for proposal in pending:
            if _advance_proposal(conn, proposal):
                processed_ids.append(proposal.proposal_id)

    return processed_ids


def _advance_proposal(conn: sqlite3.Connection, proposal: PendingProposal) -> bool:
    from app.rules.validators import filter_slots_by_rules
    from app.scheduling.meeting_type import normalize_scheduling_intent
    from app.scheduling.pre_approval_gate import verify_before_kory_approval
    from app.scheduling.scheduling_plan import build_scheduling_plan
    from app.scheduling.slot_engine import infer_meeting_format

    started = time.perf_counter()
    savepoint = f"proposal_{proposal.proposal_id}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        calendar_context = _load_calendar_context(
            subject=proposal.subject or "",
            body=proposal.scheduling_body(),
        )
        if calendar_context.get("status") != "available":
            raise RuntimeError(
                "Live calendar unavailable; cannot propose slots without calendar truth. "
                f"Detail: {calendar_context.get('error') or calendar_context.get('source')}"
            )
        schedule = _build_schedule(proposal, calendar_context)
        schedule.slots = _filter_non_conflicting_slots(
            schedule.slots,
            calendar_context,
        )
        type_key = normalize_scheduling_intent(
            proposal.intent_classification,
            subject=proposal.subject or "",
            body=proposal.scheduling_body(),
        )
        meeting_format = infer_meeting_format(
            type_key,
            subject=proposal.subject or "",
            body=proposal.scheduling_body(),
        )
        from app.scheduling.preferences import (
            guidance_relaxes_slot_minimum,
            load_scheduling_preferences,
        )

        guidance = (proposal.kory_scheduling_guidance or "").strip()
        # Guidance-aware preferences, or this re-validation strips the very
        # slots Kory's exception allowed (the engine already honored it and
        # this second pass silently undid it — live I-2, fourth layer).
        schedule.slots, rule_validation = filter_slots_by_rules(
            schedule.slots,
            intent=type_key,
            meeting_format=meeting_format,
            busy_events=calendar_context.get("busy_events"),
            preferences=load_scheduling_preferences(guidance=guidance),
        )
        required_slots = 1 if guidance_relaxes_slot_minimum(guidance) else MIN_SLOT_OPTIONS
        if len(schedule.slots) < required_slots:
            raise ValueError(
                f"Insufficient valid slots ({len(schedule.slots)}); "
                f"rules: {rule_validation.violations}"
            )

        # Reuse the plan the engine actually scheduled against (already travel-shifted).
        # Rebuilding here re-parsed relative windows like "next week" WITHOUT the travel
        # shift, so the gate rejected valid post-travel slots as "outside requested window"
        # and over-deferred to Kory. Fall back to a fresh plan only for non-engine paths.
        plan = schedule.plan or build_scheduling_plan(
            subject=proposal.subject or "",
            body=proposal.scheduling_body(),
            intent=proposal.intent_classification,
            use_llm=bool(settings.llm_api_key),
        )
        gate = verify_before_kory_approval(
            slots=schedule.slots,
            calendar_context=calendar_context,
            plan=plan,
            intent=proposal.intent_classification,
            subject=proposal.subject or "",
            body=proposal.scheduling_body(),
            meeting_format=meeting_format,
            window_expanded=schedule.window_expanded,
        )
        if not gate.ok:
            raise ValueError(f"Pre-approval gate failed: {gate.summary()}")

        resolved_tz = _resolve_recipient_timezone(proposal)
        _update_proposal_for_approval(
            conn,
            proposal.proposal_id,
            schedule,
            voice_mode=proposal.voice_mode,
            recipient_timezone=resolved_tz,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        _insert_audit_log(
            conn,
            step_name="scheduler_engine",
            reference_id=str(proposal.proposal_id),
            log_level="INFO",
            message="Proposed slots and draft; awaiting Kory approval (holds placed after send).",
            payload={
                "proposal_id": proposal.proposal_id,
                "thread_id": proposal.thread_id,
                "slot_count": len(schedule.slots),
                "duration_ms": duration_ms,
                "schedule_source": schedule.source,
                "calendar_status": calendar_context.get("status"),
                "confidence_score": schedule.confidence_score,
                "rule_validation": rule_validation.to_dict(),
                "recipient_timezone": proposal.recipient_timezone,
            },
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        conn.commit()
        return True
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        _insert_audit_log(
            conn,
            step_name="scheduler_engine",
            reference_id=str(proposal.proposal_id),
            log_level="ERROR",
            message="Scheduler failed; proposal left in pending_triage for review.",
            payload={
                "proposal_id": proposal.proposal_id,
                "thread_id": proposal.thread_id,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        conn.commit()
        return False


def _proposal_from_row(row: sqlite3.Row) -> PendingProposal:
    return PendingProposal(
        proposal_id=int(row["proposal_id"]),
        thread_id=str(row["thread_id"]),
        intent_classification=row["intent_classification"],
        priority_tier=row["priority_tier"],
        triage_confidence=row["triage_confidence"],
        justification=row["justification"],
        rule_reasoning=row["rule_reasoning"],
        subject=row["subject"],
        sender=row["sender"],
        received_at=row["received_at"],
        raw_body=row["raw_body"],
        voice_mode=str(row["voice_mode"] or "kory"),
        send_channel=str(row["send_channel"] or "kory"),
        recipient_timezone=row["recipient_timezone"] if "recipient_timezone" in row.keys() else None,
        kory_scheduling_guidance=(
            row["kory_scheduling_guidance"] if "kory_scheduling_guidance" in row.keys() else None
        ),
    )


_PROPOSAL_SELECT = """
    SELECT
        p.id AS proposal_id,
        p.thread_id,
        p.intent_classification,
        p.priority_tier,
        p.confidence_score AS triage_confidence,
        p.justification,
        p.rule_reasoning,
        p.voice_mode,
        p.send_channel,
        p.kory_scheduling_guidance,
        COALESCE(p.recipient_timezone, e.recipient_timezone) AS recipient_timezone,
        e.subject,
        e.sender,
        e.received_at,
        e.raw_body
    FROM proposals AS p
    INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
"""


def _fetch_proposal_by_id(conn: sqlite3.Connection, proposal_id: int) -> PendingProposal | None:
    row = conn.execute(
        f"{_PROPOSAL_SELECT} WHERE p.id = ? LIMIT 1",
        (proposal_id,),
    ).fetchone()
    return _proposal_from_row(row) if row else None


def _fetch_pending_proposals(conn: sqlite3.Connection) -> list[PendingProposal]:
    rows = conn.execute(
        f"{_PROPOSAL_SELECT} WHERE p.status = ? ORDER BY p.id ASC",
        (PENDING_TRIAGE,),
    ).fetchall()
    return [_proposal_from_row(row) for row in rows]


def _load_calendar_context(
    *,
    subject: str = "",
    body: str = "",
) -> dict[str, Any]:
    """Fetch intelligence-filtered Outlook busy blocks (horizon from settings + email cues)."""
    from app.scheduling.calendar_context import load_scheduling_calendar_context

    return load_scheduling_calendar_context(subject=subject, body=body)


def _mock_calendar_context(
    start: datetime,
    end: datetime,
    exc: Exception,
) -> dict[str, Any]:
    """Structural fallback when Composio/Outlook is unavailable."""
    return {
        "status": "unavailable",
        "source": "mock",
        "endpoint": "https://connect.composio.dev/mcp",
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "busy_events": [],
        "error": f"{type(exc).__name__}: {exc}",
    }


def _build_schedule(
    proposal: PendingProposal,
    calendar_context: dict[str, Any],
) -> ScheduleResult:
    from app.scheduling.reply_composer import compose_scheduling_reply
    from app.scheduling.schedule_from_context import schedule_from_context

    result = schedule_from_context(
        subject=proposal.subject or "",
        body=proposal.raw_body or "",
        intent=proposal.intent_classification,
        sender_email=proposal.sender,
        kory_scheduling_guidance=proposal.kory_scheduling_guidance or "",
        stored_recipient_timezone=proposal.recipient_timezone,
        try_inbound_availability=True,
        format_slots=False,
        calendar_context=calendar_context,
    )

    if result.path == "plan_non_scheduling":
        from app.scheduling.scheduling_plan import build_scheduling_plan

        plan = result.plan or build_scheduling_plan(
            subject=proposal.subject or "",
            body=proposal.scheduling_body(),
            intent=proposal.intent_classification,
        )
        return ScheduleResult(
            slots=[],
            drafted_reply=_general_reply_placeholder(proposal, plan),
            confidence_score=0.5,
            source="plan_non_scheduling",
        )

    if not result.ok:
        raise ValueError(result.failure_message or f"Scheduling failed: {result.status}")

    slots = result.slots
    window_expanded = bool(result.diagnostics.get("window_expanded")) or bool(
        result.diagnostics.get("morning_preference_relaxed")
    )
    if result.plan and result.plan.window and slots and not window_expanded:
        from app.scheduling.scheduling_window import slot_date_in_window

        if any(not slot_date_in_window(slot, result.plan.window) for slot in slots):
            window_expanded = True
    draft, draft_source = compose_scheduling_reply(
        proposal_sender=proposal.sender,
        proposal_subject=proposal.subject or "",
        proposal_body=proposal.raw_body or "",
        thread_id=proposal.thread_id,
        slots=slots,
        voice_mode=proposal.voice_mode,
        stored_recipient_timezone=proposal.recipient_timezone or result.recipient_timezone,
        plan=result.plan,
        intent=proposal.intent_classification,
    )
    source_label = result.path
    if draft_source:
        source_label = f"{result.path}+{draft_source}"
    # The gate's warnings (e.g. "no availability for week of August 10 —
    # offering week of August 17 instead") were computed here and then dropped:
    # nothing copied them onto the result, so the persisted proposal carried no
    # scheduling_note and Kory approved window expansions he was never shown.
    scheduling_note = result.scheduling_note()
    return ScheduleResult(
        slots=slots,
        drafted_reply=draft,
        confidence_score=0.92,
        source=source_label,
        window_expanded=window_expanded,
        plan=result.plan,
        scheduling_note=scheduling_note,
    )


def _scheduler_system_prompt(*, recipient_email: str | None = None, voice_mode: str = "kory") -> str:
    from app.llm.kory_voice import voice_prompt_block
    from app.scheduling.lexi_voice import normalize_voice_mode, voice_instruction_for_mode
    from app.storage.kory_memory import facts_prompt_block

    base = SCHEDULER_SYSTEM_PROMPT + "\n\n" + voice_prompt_block(recipient_email=recipient_email)
    memory = facts_prompt_block()
    if memory:
        base += "\n\n" + memory
    mode = normalize_voice_mode(voice_mode)
    if mode == "lexi":
        base += "\n\n" + voice_instruction_for_mode("lexi")
    else:
        base += "\n\n" + voice_instruction_for_mode("kory")
    return base


def _call_llm_scheduler(
    proposal: PendingProposal,
    calendar_context: dict[str, Any],
) -> ScheduleResult:
    client = get_hermes_client()
    user_payload = {
        "email": {
            "subject": proposal.subject,
            "sender": proposal.sender,
            "body": proposal.raw_body,
            "received_at": proposal.received_at,
        },
        "triage": {
            "intent": proposal.intent_classification,
            "priority": proposal.priority_tier,
            "triage_confidence": proposal.triage_confidence,
            "justification": proposal.justification,
        },
        "calendar": {
            "status": calendar_context.get("status"),
            "range_start": calendar_context.get("range_start"),
            "range_end": calendar_context.get("range_end"),
            "busy_events": calendar_context.get("busy_events") or [],
            "timezone": settings.scheduling_timezone,
        },
    }
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {
                "role": "system",
                "content": _scheduler_system_prompt(
                    recipient_email=proposal.sender,
                    voice_mode=proposal.voice_mode,
                ),
            },
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content or ""
    payload = _parse_json_object(content)
    return _coerce_schedule_result(payload, source="llm")


def _fallback_schedule_from_engine(
    proposal: PendingProposal,
    calendar_context: dict[str, Any],
    llm_exc: Exception,
) -> ScheduleResult:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.scheduling_timezone)
    anchor = datetime.now(tz=tz).replace(hour=10, minute=0, second=0, microsecond=0)
    duration = timedelta(minutes=30)
    candidate_hours = (9, 10, 11, 13, 14, 15, 16)
    slots: list[dict[str, str]] = []
    for day_offset in range(1, settings.lexi_calendar_search_days):
        for hour in candidate_hours:
            start = (anchor + timedelta(days=day_offset)).replace(hour=hour)
            end = start + duration
            slots.append({"start": start.isoformat(), "end": end.isoformat()})
            if len(slots) >= MAX_SLOT_OPTIONS * 8:
                break
        if len(slots) >= MAX_SLOT_OPTIONS * 8:
            break

    slots = _filter_non_conflicting_slots(slots, calendar_context)[:MAX_SLOT_OPTIONS]
    from app.scheduling.reply_composer import compose_scheduling_reply

    draft, draft_source = compose_scheduling_reply(
        proposal_sender=proposal.sender,
        proposal_subject=proposal.subject or "",
        proposal_body=proposal.raw_body or "",
        thread_id=proposal.thread_id,
        slots=slots,
        voice_mode=proposal.voice_mode,
        stored_recipient_timezone=proposal.recipient_timezone,
    )
    return ScheduleResult(
        slots=slots,
        drafted_reply=draft,
        confidence_score=0.35,
        source=f"engine_fallback+{draft_source}",
    )


def _coerce_schedule_result(payload: dict[str, Any], *, source: str) -> ScheduleResult:
    raw_slots = payload.get("slots") or []
    if not isinstance(raw_slots, list):
        raise ValueError("LLM slots must be a JSON array")

    slots: list[dict[str, str]] = []
    for item in raw_slots[:MAX_SLOT_OPTIONS]:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start", "")).strip()
        end = str(item.get("end", "")).strip()
        if start and end:
            slots.append({"start": start, "end": end})

    drafted_reply = str(payload.get("drafted_reply", "")).strip()
    if not drafted_reply:
        raise ValueError("LLM drafted_reply is empty")

    try:
        confidence = float(payload.get("confidence_score", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return ScheduleResult(
        slots=slots,
        drafted_reply=drafted_reply,
        confidence_score=confidence,
        source=source,
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain valid JSON") from None
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON root must be an object")
    return parsed


def _filter_non_conflicting_slots(
    slots: list[dict[str, str]],
    calendar_context: dict[str, Any],
) -> list[dict[str, str]]:
    from app.scheduling.busy_intervals import slot_conflicts_busy

    busy_events = calendar_context.get("busy_events") or []
    if calendar_context.get("status") != "available":
        return []

    safe: list[dict[str, str]] = []
    for slot in slots:
        if not slot_conflicts_busy(slot, busy_events):
            safe.append(slot)
    return safe


def _ensure_aware(dt: datetime) -> datetime:
    """Normalize datetimes to UTC-aware form for safe interval comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _slot_conflicts_busy(slot: dict[str, str], busy_events: list[dict[str, Any]]) -> bool:
    slot_start = _parse_iso_datetime(slot["start"])
    slot_end = _parse_iso_datetime(slot["end"])
    if not slot_start or not slot_end:
        return True

    slot_start = _ensure_aware(slot_start)
    slot_end = _ensure_aware(slot_end)

    for event in busy_events:
        event_start = _parse_event_datetime(event.get("start"))
        event_end = _parse_event_datetime(event.get("end"))
        if not event_start or not event_end:
            continue
        event_start = _ensure_aware(event_start)
        event_end = _ensure_aware(event_end)
        if event_start < slot_end and event_end > slot_start:
            return True
    return False


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _ensure_aware(parsed)


def _parse_event_datetime(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("dateTime")
    if not isinstance(value, str):
        return None
    return _parse_iso_datetime(value)


def _update_proposal_for_approval(
    conn: sqlite3.Connection,
    proposal_id: int,
    schedule: ScheduleResult,
    *,
    voice_mode: str = "kory",
    recipient_timezone: str | None = None,
) -> None:
    from app.agents.inbound_reply import _finalize_draft

    drafted = _finalize_draft(schedule.drafted_reply, voice_mode=voice_mode)
    # NO COALESCE on the note: a clean fresh schedule must CLEAR a stale
    # warning, or Kory sees yesterday's "no availability for Thursday" on top
    # of a draft that offers exactly Thursday (live O-4, #6481 — same lesson
    # as hermes_orchestrator._persist_proposal_draft, drifted copy).
    note = (schedule.scheduling_note or "").strip() or None
    conn.execute(
        """
        UPDATE proposals
        SET status = ?,
            proposed_slots = ?,
            drafted_reply = ?,
            confidence_score = ?,
            recipient_timezone = COALESCE(?, recipient_timezone),
            scheduling_note = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            PENDING_APPROVAL,
            json.dumps(schedule.slots, default=str),
            drafted,
            schedule.confidence_score,
            recipient_timezone,
            note,
            proposal_id,
        ),
    )


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


def _resolve_recipient_timezone(proposal: PendingProposal) -> str | None:
    if proposal.recipient_timezone:
        return proposal.recipient_timezone
    from app.scheduling.timezone_intel import lookup_recipient_timezone

    result = lookup_recipient_timezone(
        sender_email=proposal.sender,
        body=proposal.raw_body or "",
        received_at=proposal.received_at,
    )
    if result.confidence != "unknown" and result.tz_name():
        return result.tz_name()
    return None


def _general_reply_placeholder(proposal: PendingProposal, plan) -> str:
    from app.scheduling.email_format import recipient_display_name, sender_first_name
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK, normalize_voice_mode

    name = recipient_display_name(
        proposal.sender,
        proposal.raw_body or "",
        fallback_first_name=sender_first_name(proposal.sender),
    )
    if normalize_voice_mode(proposal.voice_mode) == "lexi":
        return (
            f"Hi {name},\n\n"
            "I'm Lexi, Kory's assistant. Thanks for your note — "
            f"{plan.draft_context or 'I will follow up shortly.'}\n\n"
            f"{LEXI_SIGNOFF_BLOCK}"
        )
    return f"Hi {name},\n\nThanks for your note. Kory will follow up shortly.\n\nLet's Win,\nKory"


def _template_reply(proposal: PendingProposal, slots: list[dict[str, str]]) -> str:
    from app.scheduling.email_format import build_scheduling_reply, recipient_display_name, sender_first_name
    from app.scheduling.lexi_voice import normalize_voice_mode
    from app.integrations.outlook_email import get_message
    from app.scheduling.timezone_intel import extract_internet_headers

    headers: list[dict[str, Any]] | None = None
    try:
        full_message, _ = get_message(proposal.thread_id)
        headers = extract_internet_headers(full_message)
    except Exception:
        headers = None

    first = recipient_display_name(
        proposal.sender,
        proposal.raw_body or "",
        fallback_first_name=sender_first_name(proposal.sender),
    )
    return build_scheduling_reply(
        recipient_first_name=first,
        slots=slots[:MAX_SLOT_OPTIONS],
        sender_email=proposal.sender,
        recipient_body=proposal.raw_body or "",
        internet_headers=headers,
        stored_recipient_timezone=proposal.recipient_timezone,
        voice_mode=normalize_voice_mode(proposal.voice_mode),
    )


def _format_slot_line(slot: dict[str, str]) -> str:
    try:
        start = datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(slot["end"].replace("Z", "+00:00"))
        return (
            f"{start.strftime('%A, %B %-d at %-I:%M %p')} to "
            f"{end.strftime('%-I:%M %p')} MT"
        )
    except ValueError:
        return f"{slot.get('start')} – {slot.get('end')}"
