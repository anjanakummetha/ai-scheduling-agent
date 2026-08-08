"""Lexi Phase 9: proactive outbound scheduling delegation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import traceback
import uuid
from typing import Any

from app.agents.scheduler_agent import _load_calendar_context
from app.config import settings
from app.integrations.outlook_calendar import create_calendar_event
from app.integrations.outlook_email import send_outbound_email
from app.llm.hermes_client import get_hermes_client
from app.rules.validators import filter_slots_by_rules
from app.scheduling.calendar_intelligence import resolve_write_calendar_name
from app.scheduling.email_format import build_scheduling_reply, sender_first_name
from app.storage.lexi_db import get_lexi_connection

PENDING_APPROVAL = "pending_approval"
STATUS_EXECUTED = "executed"
MAX_SLOT_OPTIONS = 3
MIN_SLOT_OPTIONS = 2

OUTBOUND_SYSTEM_PROMPT = """You are Lexi, Kory's executive scheduling assistant initiating proactive outreach.
Draft an outbound invitation to schedule a meeting.

Return ONLY a valid JSON object with exactly these keys:
- slots: array of 2-3 objects, each with "start" and "end" in ISO-8601 format (include timezone offset).
  Slots must avoid provided busy calendar blocks and respect the requested meeting duration.
- drafted_reply: string, a proactive invitation email in Kory's voice. Sign off with "Let's Win,\\nKory".
- confidence_score: float between 0.0 and 1.0

Do not include markdown fences or any text outside the JSON object."""


@dataclass
class OutboundScheduleResult:
    slots: list[dict[str, str]]
    drafted_reply: str
    confidence_score: float
    source: str = "llm"

    def as_dict(self) -> dict[str, Any]:
        return {
            "slots": self.slots,
            "drafted_reply": self.drafted_reply,
            "confidence_score": self.confidence_score,
            "source": self.source,
        }


def initiate_outbound_scheduling(
    recipient_email: str,
    subject: str,
    meeting_intent: str,
    duration_minutes: int,
    authorized_by: str,
    *,
    require_ceo_signoff: bool = True,
    voice_mode: str = "kory",
    constraints: str = "",
) -> dict[str, Any]:
    """Start an outbound Lexi scheduling thread with slots, holds, and optional auto-send."""
    from app.scheduling.lexi_voice import normalize_voice_mode

    recipient = recipient_email.strip().lower()
    if not recipient or "@" not in recipient:
        raise ValueError("recipient_email must be a valid email address.")
    if duration_minutes < 15:
        raise ValueError("duration_minutes must be at least 15.")
    # Voice decides the mailbox too: a Kory-voice email from the Lexi address
    # (or vice versa) is an identity mismatch. Was hard-coded kory — "as
    # Lexi" outbound requests staged Kory-voice drafts (live O-2b, #6813).
    voice = normalize_voice_mode(voice_mode)

    thread_id = f"lexi-outbound-{uuid.uuid4().hex}"
    intent = _normalize_intent(meeting_intent)
    received_at = datetime.now(timezone.utc).isoformat()
    outbound_context = (
        f"Outbound delegation initiated by {authorized_by} for a {duration_minutes}-minute "
        f"{intent.replace('_', ' ')} with {recipient}."
    )
    # Kory's own words ("mornings her time", "next week", "she's in Boston")
    # must reach the slot engine — the canned context alone gave a Boston
    # morning request noon-ET slots because the window/timezone cues that the
    # inbound-email path parses were simply never present here.
    if constraints.strip():
        outbound_context += f" Kory's scheduling instructions: {constraints.strip()}"

    result: dict[str, Any] = {
        "ok": False,
        "thread_id": thread_id,
        "proposal_id": None,
        "status": None,
        "require_ceo_signoff": require_ceo_signoff,
        "email_sent": False,
        "slots": [],
        "errors": [],
    }

    # Slot search runs BEFORE the DB transaction opens: the calendar fetch can
    # take 40-80s+ on a cold cache, and SQLite has a single writer — holding
    # the write lock through a network fetch starves the orchestrator and
    # webhook ingress for its whole duration (live DB-lock incidents).
    try:
        calendar_context = _load_calendar_context(
            subject=subject.strip(),
            body=outbound_context,
        )
        schedule = _build_outbound_schedule(
            recipient_email=recipient,
            subject=subject.strip(),
            meeting_intent=intent,
            duration_minutes=duration_minutes,
            authorized_by=authorized_by,
            calendar_context=calendar_context,
            voice_mode=voice,
            constraints=constraints.strip(),
        )
        schedule.slots = _filter_non_conflicting_slots(
            schedule.slots,
            calendar_context,
        )
        if len(schedule.slots) < MIN_SLOT_OPTIONS:
            raise ValueError(
                f"Outbound scheduling produced insufficient slots ({len(schedule.slots)})."
            )
    except Exception as exc:
        tb = traceback.format_exc()
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        with get_lexi_connection() as conn:
            _insert_audit_log(
                conn,
                step_name="outbound_delegation_init",
                reference_id=thread_id,
                log_level="ERROR",
                message="Outbound delegation initialization failed.",
                payload={
                    "thread_id": thread_id,
                    "recipient_email": recipient,
                    "error": str(exc),
                    "traceback": tb,
                    "partial_result": result,
                },
            )
            conn.commit()
        return result

    with get_lexi_connection() as conn:
        conn.execute("SAVEPOINT outbound_init")
        try:
            conn.execute(
                """
                INSERT INTO email_threads (thread_id, subject, sender, received_at, raw_body)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, subject.strip(), recipient, received_at, outbound_context),
            )

            proposal_id = _insert_outbound_proposal(
                conn,
                thread_id=thread_id,
                intent=intent,
                schedule=schedule,
                authorized_by=authorized_by,
                duration_minutes=duration_minutes,
                require_ceo_signoff=require_ceo_signoff,
                voice_mode=voice,
            )
            # Holds are placed after Kory approves send (comms_agent send_offer).

            result["proposal_id"] = proposal_id
            result["slots"] = schedule.slots
            result["drafted_reply"] = schedule.drafted_reply

            from app.safety.approval_gate import immediate_send_allowed

            if require_ceo_signoff or not immediate_send_allowed():
                require_ceo_signoff = True
                _set_proposal_status(conn, proposal_id, PENDING_APPROVAL)
                result["status"] = PENDING_APPROVAL
                result["ok"] = True
            else:
                dispatch = _dispatch_outbound_execution(
                    conn,
                    proposal_id=proposal_id,
                    thread_id=thread_id,
                    recipient_email=recipient,
                    subject=subject.strip(),
                    schedule=schedule,
                    authorized_by=authorized_by,
                )
                result.update(dispatch)
                result["status"] = dispatch.get("status", STATUS_EXECUTED)
                result["ok"] = bool(dispatch.get("ok"))

            _insert_audit_log(
                conn,
                step_name="outbound_delegation_init",
                reference_id=thread_id,
                log_level="INFO" if result["ok"] else "ERROR",
                message="Outbound delegation initialized.",
                payload={
                    "thread_id": thread_id,
                    "proposal_id": proposal_id,
                    "recipient_email": recipient,
                    "meeting_intent": intent,
                    "duration_minutes": duration_minutes,
                    "authorized_by": authorized_by,
                    "require_ceo_signoff": require_ceo_signoff,
                    "result": result,
                    "calendar_status": calendar_context.get("status"),
                },
            )
            conn.execute("RELEASE SAVEPOINT outbound_init")
            conn.commit()
            return result
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT outbound_init")
            conn.execute("RELEASE SAVEPOINT outbound_init")
            tb = traceback.format_exc()
            result["errors"].append(f"{type(exc).__name__}: {exc}")
            _insert_audit_log(
                conn,
                step_name="outbound_delegation_init",
                reference_id=thread_id,
                log_level="ERROR",
                message="Outbound delegation initialization failed.",
                payload={
                    "thread_id": thread_id,
                    "recipient_email": recipient,
                    "error": str(exc),
                    "traceback": tb,
                    "partial_result": result,
                },
            )
            conn.commit()
            return result


def _build_outbound_schedule(
    *,
    recipient_email: str,
    subject: str,
    meeting_intent: str,
    duration_minutes: int,
    authorized_by: str,
    calendar_context: dict[str, Any],
    voice_mode: str = "kory",
    constraints: str = "",
) -> OutboundScheduleResult:
    from app.scheduling.slot_engine import propose_meeting_slots

    body = f"Outbound delegation by {authorized_by} for {duration_minutes} minutes."
    if constraints:
        # The engine parses window/timezone/morning cues from body text —
        # Kory's instructions belong there, same as an inbound sender's words.
        body += f" {constraints}"
    engine = propose_meeting_slots(
        calendar_context,
        intent=meeting_intent,
        subject=subject,
        body=body,
    )
    if len(engine.slots) >= MIN_SLOT_OPTIONS:
        first_name = sender_first_name(recipient_email)
        return OutboundScheduleResult(
            slots=engine.slots[:MAX_SLOT_OPTIONS],
            drafted_reply=build_scheduling_reply(
                recipient_first_name=first_name,
                slots=engine.slots[:MAX_SLOT_OPTIONS],
                sender_email=recipient_email,
                voice_mode=voice_mode,
                outbound=True,
            ),
            confidence_score=0.92,
            source="slot_engine",
        )

    try:
        llm = _call_outbound_llm(
            recipient_email=recipient_email,
            subject=subject,
            meeting_intent=meeting_intent,
            duration_minutes=duration_minutes,
            authorized_by=authorized_by,
            calendar_context=calendar_context,
        )
        llm.slots = _filter_non_conflicting_slots(llm.slots, calendar_context)
        safe, _ = filter_slots_by_rules(
            llm.slots,
            intent=meeting_intent,
            busy_events=calendar_context.get("busy_events"),
        )
        if len(safe) >= MIN_SLOT_OPTIONS:
            llm.slots = safe[:MAX_SLOT_OPTIONS]
            llm.source = "llm_validated"
            return llm
    except Exception as exc:
        if calendar_context.get("status") != "available":
            raise RuntimeError(
                "Outbound scheduling failed: no valid slots and calendar unavailable."
            ) from exc

    raise ValueError(
        "No valid outbound slots found for Kory's calendar and rules. "
        f"Diagnostics: {engine.diagnostics}"
    )


def _call_outbound_llm(
    *,
    recipient_email: str,
    subject: str,
    meeting_intent: str,
    duration_minutes: int,
    authorized_by: str,
    calendar_context: dict[str, Any],
) -> OutboundScheduleResult:
    client = get_hermes_client()
    user_payload = {
        "mode": "outbound_delegation",
        "recipient_email": recipient_email,
        "subject": subject,
        "meeting_intent": meeting_intent,
        "duration_minutes": duration_minutes,
        "authorized_by": authorized_by,
        "instruction": (
            f"Draft an outward invite to {recipient_email} for a {duration_minutes}-minute "
            f"{meeting_intent.replace('_', ' ')}."
        ),
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
            {"role": "system", "content": OUTBOUND_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content or ""
    payload = _parse_json_object(content)
    return _coerce_outbound_schedule(payload)


def _fallback_outbound_schedule(
    *,
    duration_minutes: int,
    meeting_intent: str,
    recipient_email: str,
    error: Exception,
) -> OutboundScheduleResult:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.scheduling_timezone)
    anchor = datetime.now(tz=tz).replace(hour=10, minute=0, second=0, microsecond=0)
    slots: list[dict[str, str]] = []
    for day_offset in (1, 2, 3):
        start = anchor + timedelta(days=day_offset)
        end = start + timedelta(minutes=duration_minutes)
        slots.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
    first_name = recipient_email.split("@", 1)[0].replace(".", " ").title()
    return OutboundScheduleResult(
        slots=slots,
        drafted_reply=(
            f"Hi {first_name},\n\n"
            f"I'd like to find {duration_minutes} minutes for a {meeting_intent.replace('_', ' ')}. "
            "Sharing a few options that work on my end — let me know what is best.\n\n"
            "Let's Win,\n"
            "Kory"
        ),
        confidence_score=0.4,
        source=f"fallback ({type(error).__name__})",
    )


def _coerce_outbound_schedule(payload: dict[str, Any]) -> OutboundScheduleResult:
    raw_slots = payload.get("slots") or []
    slots: list[dict[str, str]] = []
    if isinstance(raw_slots, list):
        for item in raw_slots[:MAX_SLOT_OPTIONS]:
            if isinstance(item, dict) and item.get("start") and item.get("end"):
                slots.append({"start": str(item["start"]), "end": str(item["end"])})

    drafted_reply = str(payload.get("drafted_reply", "")).strip()
    if not drafted_reply:
        raise ValueError("Outbound LLM returned an empty drafted_reply.")

    try:
        confidence = float(payload.get("confidence_score", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    return OutboundScheduleResult(
        slots=slots,
        drafted_reply=drafted_reply,
        confidence_score=max(0.0, min(1.0, confidence)),
        source="llm",
    )


def _dispatch_outbound_execution(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    thread_id: str,
    recipient_email: str,
    subject: str,
    schedule: OutboundScheduleResult,
    authorized_by: str,
) -> dict[str, Any]:
    """Send outbound email and finalize proposal without CEO sign-off."""
    dispatch: dict[str, Any] = {
        "ok": False,
        "email_sent": False,
        "calendar_event_id": None,
        "status": PENDING_APPROVAL,
    }

    try:
        message_id, _log_id = send_outbound_email(
            to_email=recipient_email,
            subject=subject,
            body=schedule.drafted_reply,
            approved_send=True,
        )
        dispatch["email_sent"] = True
        dispatch["outlook_message_id"] = message_id
    except Exception as exc:
        dispatch["error"] = f"outbound_email_failed: {exc}"
        _insert_audit_log(
            conn,
            step_name="outbound_delegation_init",
            reference_id=thread_id,
            log_level="ERROR",
            message="Outbound email dispatch failed.",
            payload={"proposal_id": proposal_id, "error": str(exc), "traceback": traceback.format_exc()},
        )
        return dispatch

    first_slot = schedule.slots[0]
    try:
        event_id, _ = create_calendar_event(
            {
                "start": first_slot["start"],
                "end": first_slot["end"],
                "title": subject,
                "location": "Teams",
                "attendees": [recipient_email],
            }
        )
        dispatch["calendar_event_id"] = event_id
    except Exception as exc:
        dispatch["calendar_warning"] = f"{type(exc).__name__}: {exc}"

    conn.execute(
        """
        INSERT INTO approvals (
            proposal_id, decision, decision_source, authorized_by, modification_notes
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            "approved",
            "outbound_auto_execute",
            authorized_by,
            "Outbound delegation sent without CEO sign-off.",
        ),
    )
    _set_proposal_status(conn, proposal_id, STATUS_EXECUTED)
    _finalize_outbound_holds(
        conn,
        proposal_id=proposal_id,
        selected_slot=first_slot,
        calendar_event_id=dispatch.get("calendar_event_id"),
    )
    dispatch["status"] = STATUS_EXECUTED
    dispatch["ok"] = bool(dispatch["email_sent"])
    return dispatch


def _finalize_outbound_holds(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    selected_slot: dict[str, str],
    calendar_event_id: str | None,
) -> None:
    """Keep the selected hold row; remove unselected holds from the database."""
    rows = conn.execute(
        "SELECT id, slot_start, slot_end FROM holds WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchall()
    selected_start = selected_slot.get("start", "")
    for row in rows:
        if str(row["slot_start"]) == selected_start:
            if calendar_event_id:
                conn.execute(
                    "UPDATE holds SET event_id = ? WHERE id = ?",
                    (calendar_event_id, row["id"]),
                )
            continue
        conn.execute("DELETE FROM holds WHERE id = ?", (row["id"],))


def _insert_outbound_proposal(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    intent: str,
    schedule: OutboundScheduleResult,
    authorized_by: str,
    duration_minutes: int,
    require_ceo_signoff: bool,
    voice_mode: str = "kory",
) -> int:
    rule_reasoning = {
        "source": "outbound_delegation",
        "authorized_by": authorized_by,
        "duration_minutes": duration_minutes,
        "require_ceo_signoff": require_ceo_signoff,
        "schedule": schedule.as_dict(),
    }
    cursor = conn.execute(
        """
        INSERT INTO proposals (
            thread_id,
            status,
            intent_classification,
            priority_tier,
            rule_reasoning,
            proposed_slots,
            drafted_reply,
            confidence_score,
            justification,
            voice_mode,
            send_channel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            PENDING_APPROVAL,
            intent,
            "medium",
            json.dumps(rule_reasoning, default=str),
            json.dumps(schedule.slots, default=str),
            schedule.drafted_reply,
            schedule.confidence_score,
            f"Outbound delegation to schedule a {duration_minutes}-minute {intent.replace('_', ' ')}.",
            voice_mode,
            voice_mode,  # voice decides the mailbox: kory-voice → kory channel
        ),
    )
    return int(cursor.lastrowid)


def _set_proposal_status(conn: sqlite3.Connection, proposal_id: int, status: str) -> None:
    conn.execute(
        """
        UPDATE proposals
        SET status = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (status, proposal_id),
    )


def _filter_non_conflicting_slots(
    slots: list[dict[str, str]],
    calendar_context: dict[str, Any],
) -> list[dict[str, str]]:
    # Uses the shared busy_intervals check, NOT a private copy: the local
    # duplicate compared naive event times against aware slot times and blew
    # up the whole outbound flow ("can't compare offset-naive and
    # offset-aware datetimes", live O-2b).
    from app.scheduling.busy_intervals import slot_conflicts_busy

    busy_events = calendar_context.get("busy_events") or []
    if calendar_context.get("status") != "available" or not busy_events:
        return slots[:MAX_SLOT_OPTIONS]

    return [slot for slot in slots if not slot_conflicts_busy(slot, busy_events)]


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


def _normalize_intent(meeting_intent: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", meeting_intent.strip().lower()).strip("_")
    return normalized or "unknown"


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
