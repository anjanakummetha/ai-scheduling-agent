"""Deterministic scheduling actions for Hermes (Option A conversational Lexi)."""

from __future__ import annotations

import json
import traceback
from typing import Any

from app.config import resolve_lexi_env, safety_posture_summary, settings
from app.integrations.calendar_holds import place_tentative_hold
from app.integrations.named_calendars import list_all_calendars, resolve_calendar_name
from app.integrations.outlook_calendar import has_conflict
from app.scheduling.email_format import build_scheduling_reply, example_draft_preview, sender_first_name
from app.integrations.outlook_email import send_outbound_email
from app.storage.lexi_db import get_lexi_connection


def _ingress_status() -> dict[str, Any]:
    import os

    from app.orchestrator import describe_ingress_mode
    from app.worker.runner import is_worker_running

    interval = int(os.getenv("LEXI_ORCHESTRATOR_INTERVAL", "30"))
    status = describe_ingress_mode(interval_seconds=interval)
    status["worker_running"] = is_worker_running()
    return status


_KORY_TZ_LABELS = {
    "America/Denver": "Mountain Time (MT)",
    "America/Los_Angeles": "Pacific Time (PT)",
    "America/Chicago": "Central Time (CT)",
    "America/New_York": "Eastern Time (ET)",
}


def _kory_home_timezone_label() -> str:
    return _KORY_TZ_LABELS.get(settings.scheduling_timezone, settings.scheduling_timezone)


def format_kory_status_brief(status: dict[str, Any]) -> str:
    """2–4 lines for Teams chat — no connection IDs or API internals."""
    dry = bool(status.get("lexi_dry_run"))
    mode = "test mode (no live sends)" if dry else "live"
    ingress = status.get("ingress") or {}
    worker = bool(status.get("worker_running") or ingress.get("worker_running"))
    pending = int(status.get("pending_approval_count") or 0)

    lines = [f"Lexi is running ({mode}). Inbox worker: {'on' if worker else 'off'}."]
    if pending > 0:
        lines.append(f"{pending} draft{'s' if pending != 1 else ''} waiting for your Send.")
    else:
        lines.append("No drafts waiting.")
    lines.append(
        f"Your home timezone is {_kory_home_timezone_label()} — you travel; "
        "outbound emails show the other person's time first."
    )
    if not status.get("teams_cards_ready", True):
        lines.append("Teams cards aren't configured yet.")
    return "\n".join(lines)


def _safe_cost_rollup() -> dict[str, Any] | None:
    try:
        from app.storage.llm_cost_log import cost_rollup

        return cost_rollup(days=30)
    except Exception:
        return None


def _safe_composio_budget() -> dict[str, Any] | None:
    try:
        from app.storage.composio_call_log import budget_status

        return budget_status()
    except Exception:
        return None


def get_lexi_system_status() -> dict[str, Any]:
    """Runtime flags for Hermes to explain dry-run vs live Outlook."""
    pending_count = 0
    try:
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM proposals WHERE status = 'pending_approval'",
            ).fetchone()
            pending_count = int(row["c"]) if row else 0
    except Exception:
        pending_count = -1

    from app.bot.teams_conversation_store import load_conversation_reference, teams_delivery_ready
    from app.safety.kory_read_only import read_only_safety_snapshot
    from app.storage.learning_log import recent_feedback_summary

    safety = read_only_safety_snapshot()
    teams_ref = load_conversation_reference()
    ingress = _ingress_status()
    payload = {
        "lexi_dry_run": settings.lexi_dry_run,
        "lexi_suppress_teams_push": settings.lexi_suppress_teams_push,
        "staging_mode": (
            "dry_run"
            if settings.lexi_dry_run
            else ("teams_suppressed" if settings.lexi_suppress_teams_push else "live")
        ),
        "lexi_write_mode": settings.lexi_write_mode,
        "demo_mode": settings.demo_mode,
        "read_connection": settings.kory_composio_connection_id,
        "lexi_connection": settings.lexi_composio_connection_id,
        "lexi_mailbox_email": settings.lexi_mailbox_email,
        "write_connection": settings.sandbox_composio_connection_id
        if settings.lexi_write_mode == "sandbox"
        else settings.kory_composio_connection_id,
        "sandbox_mailbox_email": settings.sandbox_mailbox_email,
        "sandbox_email_loopback": settings.sandbox_email_loopback,
        "asana_enabled": settings.asana_enabled,
        "asana_connection": settings.asana_composio_connection_id,
        "asana_project_gid_set": bool(settings.asana_project_gid),
        "hubspot_connection": settings.hubspot_composio_connection_id,
        "hubspot_configured": bool(settings.hubspot_composio_connection_id),
        "lexi_teams_enabled": settings.lexi_teams_enabled,
        "lexi_teams_text_only": settings.lexi_teams_text_only,
        "teams_cards_ready": teams_delivery_ready(),
        "teams_conversation_registered": bool(teams_ref),
        "teams_conversation_id_prefix": (teams_ref or {}).get("conversation_id", "")[:24] or None,
        "lexi_teams_inbound_notify_mode": settings.lexi_teams_inbound_notify_mode,
        "lexi_delegation_auto_draft": settings.lexi_delegation_auto_draft,
        "lexi_composio_search_enabled": settings.lexi_composio_search_enabled,
        "composio_configured": bool(settings.composio_api_key and settings.kory_composio_connection_id),
        "pending_approval_count": pending_count,
        "kory_home_timezone": settings.scheduling_timezone,
        "kory_home_timezone_label": _kory_home_timezone_label(),
        "scheduling_timezone": settings.scheduling_timezone,
        "outlook_timezone": settings.outlook_timezone,
        "llm_model": settings.llm_model,
        "learning_summary": recent_feedback_summary(limit=5) or None,
        "safety": safety,
        "lexi_env": resolve_lexi_env(),
        "safety_posture": safety_posture_summary(),
        "llm_cost_30d": _safe_cost_rollup(),
        "composio_budget": _safe_composio_budget(),
        "ingress": ingress,
        "note": (
            "Internal: outlook_timezone is API parsing only — Kory's home TZ is scheduling_timezone. "
            "Quote kory_brief to Kory; do not dump this object."
        ),
    }
    payload["kory_brief"] = format_kory_status_brief({**payload, "worker_running": ingress.get("worker_running")})
    payload["kory_chat"] = "Reply to Kory using kory_brief only — 2–4 short lines, no tables or connection IDs."
    return payload


def get_calendar_availability(*, days: int = 0) -> dict[str, Any]:
    """Return Kory-blocking events across Outlook calendars (intelligence-filtered)."""
    from app.integrations.family_google_calendar import family_calendar_status
    from app.scheduling.calendar_context import load_scheduling_calendar_context

    window_days = days if days > 0 else settings.lexi_calendar_search_days
    window_days = max(7, min(window_days, settings.lexi_calendar_search_days_max))
    context = load_scheduling_calendar_context(horizon_days=window_days)
    busy = context.get("busy_events") or []
    consulted = context.get("calendars_consulted") or []
    resolved_names = [c["name"] for c in consulted if c.get("resolved")]
    family = family_calendar_status()
    family_count = sum(1 for e in busy if e.get("source") == "family_calendar")
    if family["configured"] and family_count == 0:
        family_note = "Family calendar connected; no Do Not Move blocks in this window."
    elif not family["configured"]:
        family_note = family["hint"]
    else:
        family_note = f"Family Do Not Move blocks in window: {family_count}"
    return {
        "window_days": context.get("horizon_days", window_days),
        "calendar_status": context.get("status"),
        "calendars_consulted": resolved_names,
        "busy_summary": context.get("busy_summary"),
        "family_calendar": family,
        "family_blocks_in_window": family_count,
        "busy_event_count": len(busy),
        "busy_events": busy[:80],
        "range_start": context.get("range_start"),
        "range_end": context.get("range_end"),
        "scheduling_timezone": settings.scheduling_timezone,
        "kory_chat": (
            "Summarize for Kory in 1–2 plain sentences (e.g. 'You're full next week' or "
            "'Tuesday afternoon is open'). Do not mention calendars, APIs, or tool output. "
            "For a day-by-day week summary, call lexi_summarize_calendar_window instead."
        ),
    }


def summarize_calendar_window(*, query: str) -> dict[str, Any]:
    """Read-only day-by-day calendar summary for chat (Master + work Calendar merge)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.scheduling.calendar_context import load_scheduling_calendar_context
    from app.scheduling.calendar_summary import build_calendar_window_summary, infer_summary_window

    mt = ZoneInfo(settings.scheduling_timezone)
    text = query.strip()
    if not text:
        return {"ok": False, "error": "query is required (e.g. 'summarize my calendar for next week')."}

    window = infer_summary_window(query=text)
    if not window:
        return {
            "ok": False,
            "error": (
                "Could not infer a date window. Include e.g. 'next week', 'this week', "
                "or 'July 6 through July 12'."
            ),
        }

    today = datetime.now(tz=mt).date()
    horizon_days = max(7, (window.end - today).days + 2)
    context = load_scheduling_calendar_context(subject="", body=text, horizon_days=horizon_days)
    if context.get("status") != "available":
        detail = context.get("error") or context.get("source") or "unavailable"
        return {"ok": False, "error": f"Calendar unavailable: {detail}"}

    busy = context.get("busy_events") or []
    summary = build_calendar_window_summary(busy_events=busy, window=window)
    return {
        "ok": True,
        **summary,
        "busy_event_count_in_window": summary["total_events"],
        "kory_chat": (
            "Reply using formatted_summary exactly for dates and events. "
            "You may tighten wording slightly but do NOT invent, omit, or shift events. "
            "Do NOT mention Composio, group calendars, Master rollup, or API visibility."
        ),
    }


def check_time_slot(*, start_iso: str, end_iso: str) -> dict[str, Any]:
    """Check whether a proposed interval conflicts with Kory's calendar."""
    action = {"start": start_iso.strip(), "end": end_iso.strip()}
    conflict, events, log_id = has_conflict(action)
    return {
        "start": action["start"],
        "end": action["end"],
        "has_conflict": conflict,
        "conflicting_events": events[:10],
        "composio_log_id": log_id,
    }


def list_calendars(*, role: str = "read") -> dict[str, Any]:
    """List named Outlook calendars (read=Kory, write=sandbox/kory)."""
    conn_role = "write" if role.strip().lower() == "write" else "read"
    calendars = list_all_calendars(role=conn_role)  # type: ignore[arg-type]
    return {"ok": True, "role": conn_role, "count": len(calendars), "calendars": calendars}


def add_conflict_calendar(calendar_name: str) -> dict[str, Any]:
    """Add an Outlook calendar to Lexi's conflict-read list (persists config/calendars.yaml)."""
    from app.integrations.named_calendars import add_conflict_calendar as _add

    return _add(calendar_name)


def preview_scheduling_email_example() -> dict[str, Any]:
    """Return a formatted example scheduling reply for Kory's rules."""
    return {"ok": True, **example_draft_preview()}


def place_calendar_hold(
    *,
    title: str,
    start_iso: str,
    end_iso: str,
    attendee_email: str = "",
    location: str = "TBD",
    notes: str = "",
    calendar_name: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Place a tentative hold on a named Outlook calendar (default: Kory Master)."""
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=confirm, action="Calendar hold")
    # The canonical "HOLD: " prefix is applied downstream by calendar_holds.py
    # (and is what hold-detection matches). Don't prefix here or holds end up
    # titled "HOLD: Hold - ...".
    subject = (title or "Meeting").strip()

    attendees = []
    email = attendee_email.strip().lower()
    if email and "@" in email:
        attendees.append(email)

    body_note = (notes or "").strip() or "Tentative hold created by Lexi via Hermes."
    action = {
        "title": subject,
        "start": start_iso.strip(),
        "end": end_iso.strip(),
        "attendees": attendees,
        "location": location.strip() or "TBD",
        "body": body_note,
    }

    conflict, conflicts, _ = has_conflict(action)
    if conflict:
        return {
            "ok": False,
            "error": "Requested slot conflicts with existing calendar blocks.",
            "conflicting_events": conflicts[:5],
            "action": action,
        }

    cal = (calendar_name or "").strip()
    if cal:
        resolved = resolve_calendar_name(cal, role="write")
        if not resolved:
            return {
                "ok": False,
                "error": f"Calendar not found: {cal}. Call lexi_list_calendars first.",
            }

    try:
        hold = place_tentative_hold(action=action, calendar_name=cal or None)
        if not hold.get("ok"):
            return hold
        event_id = hold.get("event_id")
        log_id = hold.get("composio_log_id")
        _audit(
            "hermes_place_hold",
            reference_id=event_id or "unknown",
            message=f"Calendar hold placed: {subject}",
            payload={
                "action": action,
                "event_id": event_id,
                "log_id": log_id,
                "calendar_name": cal or "default",
            },
        )
        return {
            "ok": bool(event_id),
            "event_id": event_id,
            "composio_log_id": log_id,
            "dry_run": settings.lexi_dry_run,
            "calendar_name": cal or "default",
            "action": action,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "action": action,
        }


def infer_outbound_send_channel(body: str, *, explicit: str = "") -> str:
    """Pick kory vs lexi mailbox for chat-initiated outbound mail."""
    from app.integrations.outlook_email import infer_outbound_send_channel as _infer

    return _infer(body, explicit=explicit)


def draft_outbound_email_preview(
    *,
    to_email: str,
    subject: str,
    body: str,
    send_channel: str = "",
) -> dict[str, Any]:
    """Return an outbound email preview without sending (Hermes drafts the body in chat)."""
    recipient = to_email.strip().lower()
    if not recipient or "@" not in recipient:
        return {"ok": False, "error": "to_email must be a valid email address."}

    channel = infer_outbound_send_channel(body, explicit=send_channel)
    from_addr = (
        (settings.kory_sender_emails[0] if settings.kory_sender_emails else "kory@ifg.vc")
        if channel == "kory"
        else (settings.lexi_mailbox_email or "lexi@iconicfounders.com")
    )

    return {
        "ok": True,
        "preview_only": True,
        "to": recipient,
        "subject": subject.strip() or "(no subject)",
        "body": body.strip(),
        "send_channel": channel,
        "from_mailbox": from_addr,
        "dry_run": settings.lexi_dry_run,
        "next_step": (
            "Show this preview to Kory. After explicit approval, call "
            f"lexi_send_outbound_email with confirm_send=true and send_channel={channel}."
        ),
    }


def send_outbound_email_confirmed(
    *,
    to_email: str,
    subject: str,
    body: str,
    confirm_send: bool,
    authorized_by: str = "hermes_user",
    send_channel: str | None = None,
) -> dict[str, Any]:
    """Send outbound email only when confirm_send is true (lexi@ by default)."""
    if not confirm_send:
        return {
            "ok": False,
            "error": (
                "confirm_send must be true. Get Kory's explicit approval first "
                "(e.g. 'yes send it')."
            ),
        }

    recipient = to_email.strip().lower()
    if not recipient or "@" not in recipient:
        return {"ok": False, "error": "to_email must be a valid email address."}

    try:
        from app.integrations.outlook_email import infer_outbound_send_channel

        channel = infer_outbound_send_channel(body, explicit=send_channel or "")
        message_id, log_id = send_outbound_email(
            to_email=recipient,
            subject=subject.strip(),
            body=body.strip(),
            approved_send=True,
            send_channel=channel,  # type: ignore[arg-type]
        )
        _audit(
            "hermes_send_email",
            reference_id=message_id or recipient,
            message=f"Outbound email dispatched to {recipient}",
            payload={
                "to": recipient,
                "subject": subject,
                "message_id": message_id,
                "authorized_by": authorized_by,
                "dry_run": settings.lexi_dry_run,
            },
        )
        return {
            "ok": bool(message_id),
            "message_id": message_id,
            "composio_log_id": log_id,
            "dry_run": settings.lexi_dry_run,
            "to": recipient,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def search_inbox(query: str = "", top: int = 10) -> dict[str, Any]:
    """Search Kory's Outlook inbox (read-only)."""
    from app.integrations.outlook_inbox import search_inbox as _search

    try:
        messages, log_id = _search(query=query, top=top)
        return {
            "ok": True,
            "count": len(messages),
            "messages": messages,
            "composio_log_id": log_id,
            "query": query,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def get_email_thread(message_id: str) -> dict[str, Any]:
    """Fetch one inbox message from Kory's mailbox by message id."""
    from app.integrations.outlook_inbox import get_thread_message

    try:
        payload = get_thread_message(message_id.strip())
        return {"ok": True, **payload}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def run_propose_schedule(
    *,
    subject: str = "",
    body: str = "",
    sender: str = "unknown@example.com",
    thread_id: str = "",
) -> dict[str, Any]:
    """Unified propose_schedule via inbound email dict (Hermes / console)."""
    from app.scheduling.propose import propose_schedule
    import uuid

    tid = thread_id.strip() or f"hermes-{uuid.uuid4().hex[:10]}"
    raw_email = {
        "thread_id": tid,
        "subject": subject.strip(),
        "sender": sender.strip(),
        "received_at": "",
        "raw_body": body.strip(),
    }
    try:
        result = propose_schedule(raw_email=raw_email)
        return {"ok": True, **result}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def get_scheduling_session(session_id: str) -> dict[str, Any]:
    from app.storage.scheduling_sessions import get_session

    session = get_session(session_id.strip())
    if not session:
        return {"ok": False, "error": "session_not_found"}
    return {"ok": True, "session": session}


def upsert_scheduling_session(
    *,
    session_id: str = "",
    channel: str = "hermes",
    context: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    from app.storage.scheduling_sessions import create_session, get_session, update_session

    if session_id.strip():
        updated = update_session(session_id.strip(), context=context, status=status)
        if not updated:
            return {"ok": False, "error": "session_not_found"}
        session = get_session(session_id.strip())
        return {"ok": True, "session": session}

    new_id = create_session(channel=channel, context=context or {})
    session = get_session(new_id)
    return {"ok": True, "session": session}


def find_slots_for_request(
    *,
    subject: str = "",
    body: str = "",
    intent: str = "",
    meeting_format: str = "",
    sender_email: str = "",
) -> dict[str, Any]:
    """Chat-safe slot search — unified schedule_from_context (same path as inbound email)."""
    from app.scheduling.schedule_from_context import schedule_from_context

    subj = subject.strip()
    text = body.strip()
    if not subj and not text:
        return {"ok": False, "error": "subject or body is required (include window e.g. 'next week')."}

    result = schedule_from_context(
        subject=subj,
        body=text,
        intent=intent.strip() or None,
        sender_email=sender_email.strip() or None,
        meeting_format=meeting_format.strip() or None,
    )
    payload = result.to_dict()
    payload["kory_chat"] = (
        "Reply with formatted_slots only — these are calendar-verified. "
        "Never invent or adjust times in chat. If insufficient_slots, say so in one sentence."
    )
    return payload


def validate_scheduling_cases_action(
    *,
    cases: list[dict[str, Any]] | None = None,
    preset: str = "",
) -> dict[str, Any]:
    """Batch-validate slots against live calendar + rules; returns formatted_summary."""
    from app.scheduling.slot_validation import validate_scheduling_cases

    return validate_scheduling_cases(cases=cases, preset=preset)


def validate_slots_preview(
    slots: list[dict[str, str]],
    intent: str = "",
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.rules.validators import validate_proposal_slots
    from app.scheduling.calendar_context import load_scheduling_calendar_context

    horizon_days = 14
    latest_end: datetime | None = None
    for slot in slots:
        raw_end = str(slot.get("end") or "")
        if not raw_end:
            continue
        try:
            parsed = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        if latest_end is None or parsed > latest_end:
            latest_end = parsed
    if latest_end is not None:
        now_utc = datetime.now(timezone.utc)
        horizon_days = max(7, min(settings.lexi_calendar_search_days_max, (latest_end - now_utc).days + 3))

    calendar_context = load_scheduling_calendar_context(horizon_days=horizon_days)
    busy_events = calendar_context.get("busy_events") or []
    result = validate_proposal_slots(slots, intent=intent or None, busy_events=busy_events)
    return {
        "ok": result.valid,
        **result.to_dict(),
        "calendar_status": calendar_context.get("status"),
        "busy_event_count": len(busy_events),
    }


def get_inbound_reply_queue_action() -> dict[str, Any]:
    from app.agents.inbound_reply import get_inbound_reply_queue

    items = get_inbound_reply_queue()
    return {"ok": True, "count": len(items), "queue": items}


def begin_reoffer_action(*, proposal_id: int) -> dict[str, Any]:
    from app.agents.inbound_reply import begin_reoffer_schedule

    return begin_reoffer_schedule(proposal_id)


def recipient_timezone_action(
    *,
    sender_email: str = "",
    body: str = "",
) -> dict[str, Any]:
    from app.scheduling.timezone_intel import detect_recipient_timezone

    result = detect_recipient_timezone(sender_email=sender_email or None, body=body)
    return {
        "ok": True,
        "timezone": result.tz_name(),
        "confidence": result.confidence,
        "source": result.source,
        "label": result.label(),
        "detail": result.detail,
    }


def get_scheduling_context_action(*, proposal_id: int) -> dict[str, Any]:
    from app.scheduling.hermes_compose import get_scheduling_context_for_proposal

    return get_scheduling_context_for_proposal(proposal_id)


def inbox_review_action(*, hours: int = 48) -> dict[str, Any]:
    from app.assistant.inbox_review import build_inbox_review

    return build_inbox_review(hours=hours)


def escalate_to_heidi_action(*, proposal_id: int, reason: str = "") -> dict[str, Any]:
    from app.scheduling.heidi_escalation import escalate_to_heidi

    return escalate_to_heidi(proposal_id, reason=reason)


def begin_draft_reply_action(*, proposal_id: int, voice_mode: str = "") -> dict[str, Any]:
    from app.agents.inbound_reply import begin_draft_reply

    return begin_draft_reply(proposal_id, voice_mode=voice_mode)


def draft_reply_for_subject_action(
    *,
    subject_contains: str,
    voice_mode: str = "kory",
) -> dict[str, Any]:
    from app.agents.inbound_reply import draft_reply_for_subject

    return draft_reply_for_subject(subject_contains, voice_mode=voice_mode or "kory")


def retry_scheduling_with_guidance_action(
    *,
    proposal_id: int,
    guidance: str,
) -> dict[str, Any]:
    from app.agents.inbound_reply import retry_scheduling_with_guidance

    return retry_scheduling_with_guidance(proposal_id, guidance)


def decline_inbound_reply_action(*, proposal_id: int, reason: str = "") -> dict[str, Any]:
    from app.agents.inbound_reply import decline_reply

    return decline_reply(proposal_id, reason=reason)


def update_proposal_draft_action(*, proposal_id: int, drafted_reply: str) -> dict[str, Any]:
    from app.agents.inbound_reply import update_proposal_draft

    return update_proposal_draft(proposal_id, drafted_reply)


def start_outbound_scheduling(
    *,
    recipient_email: str,
    subject: str,
    meeting_intent: str,
    duration_minutes: int,
    authorized_by: str,
    require_ceo_signoff: bool = True,
) -> dict[str, Any]:
    """Start full outbound proposal flow (slots + draft + optional pending_approval)."""
    from app.agents.outbound_agent import initiate_outbound_scheduling

    try:
        result = initiate_outbound_scheduling(
            recipient_email=recipient_email,
            subject=subject,
            meeting_intent=meeting_intent,
            duration_minutes=duration_minutes,
            authorized_by=authorized_by,
            require_ceo_signoff=require_ceo_signoff,
        )
        result["dry_run"] = settings.lexi_dry_run
        return result
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def _audit(
    step_name: str,
    *,
    reference_id: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    try:
        with get_lexi_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    step_name,
                    reference_id,
                    "INFO",
                    message,
                    json.dumps(payload, default=str),
                ),
            )
            conn.commit()
    except Exception:
        pass


def execute_outlook_action_action(
    *,
    slug: str,
    arguments_json: str = "{}",
    confirm: bool = False,
    send_channel: str = "kory",
    allow_unlisted: bool = False,
) -> dict[str, Any]:
    from app.integrations.outlook_actions import execute_outlook_action

    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"arguments_json invalid: {exc}"}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments_json must be a JSON object."}
    channel = send_channel.strip().lower()
    if channel not in {"kory", "lexi"}:
        channel = "kory"
    try:
        result = execute_outlook_action(
            slug,
            arguments,
            confirm=confirm,
            send_channel=channel,  # type: ignore[arg-type]
            allow_unlisted=allow_unlisted,
        )
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def accept_calendar_invite_action(event_id: str) -> dict[str, Any]:
    from app.integrations.outlook_actions import accept_calendar_invite

    try:
        return {"ok": True, "result": accept_calendar_invite(event_id)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def decline_calendar_invite_action(event_id: str, comment: str = "") -> dict[str, Any]:
    from app.integrations.outlook_actions import decline_calendar_invite

    try:
        return {"ok": True, "result": decline_calendar_invite(event_id, comment=comment)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def find_meeting_times_action(payload_json: str) -> dict[str, Any]:
    from app.integrations.outlook_actions import find_meeting_times

    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"payload_json invalid: {exc}"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload_json must be a JSON object."}
    try:
        return {"ok": True, "result": find_meeting_times(payload)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def get_thread_context_action(conversation_id: str, exclude_message_id: str = "") -> dict[str, Any]:
    from app.integrations.outlook_thread import fetch_conversation_context

    context = fetch_conversation_context(
        conversation_id,
        exclude_message_id=exclude_message_id or None,
    )
    return {"ok": True, "conversation_id": conversation_id, "context": context}


def remember_kory_fact_action(fact_key: str, fact_value: str) -> dict[str, Any]:
    from app.storage.kory_memory import upsert_fact

    return upsert_fact(fact_key=fact_key, fact_value=fact_value, source="hermes")


def list_kory_memory_action() -> dict[str, Any]:
    from app.storage.kory_memory import list_facts

    facts = list_facts(limit=50)
    return {"ok": True, "count": len(facts), "facts": facts}


def unanswered_brief_action(*, hours: int = 72) -> dict[str, Any]:
    from app.assistant.briefings import build_unanswered_brief

    return build_unanswered_brief(hours=hours)


def today_calendar_brief_action() -> dict[str, Any]:
    from app.assistant.briefings import build_today_calendar_brief

    return build_today_calendar_brief()


def prebrief_action(*, include_research: bool = False) -> dict[str, Any]:
    from app.assistant.briefings import build_prebriefs_for_today

    return build_prebriefs_for_today(include_research=include_research)



def list_asana_tasks_action(*, bucket: str = "all", mine_only: bool = True) -> dict[str, Any]:
    from app.integrations.asana_manager import list_asana_tasks

    allowed = {"overdue", "due_today", "upcoming", "all"}
    key = bucket if bucket in allowed else "all"
    return list_asana_tasks(bucket=key, mine_only=mine_only)  # type: ignore[arg-type]


def create_asana_task_action(
    *, title: str, notes: str = "", due_on: str = "", section: str = "", confirm: bool = False
) -> dict[str, Any]:
    from app.integrations.asana_manager import create_asana_task_from_chat

    return create_asana_task_from_chat(
        title=title, notes=notes, due_on=due_on, section=section, approved=confirm
    )


def complete_asana_task_action(*, task_gid: str, confirm: bool = False, owner_ack: bool = False) -> dict[str, Any]:
    from app.integrations.asana_manager import complete_asana_task

    return complete_asana_task(task_gid=task_gid, approved=confirm, owner_ack=owner_ack)


def update_asana_task_action(
    *,
    task_gid: str,
    title: str = "",
    notes: str = "",
    due_on: str = "",
    confirm: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    from app.integrations.asana_manager import update_asana_task

    return update_asana_task(
        task_gid=task_gid,
        title=title,
        notes=notes,
        due_on=due_on,
        approved=confirm,
        owner_ack=owner_ack,
    )


def delete_asana_task_action(*, task_gid: str, confirm: bool = False, owner_ack: bool = False) -> dict[str, Any]:
    from app.integrations.asana_manager import delete_asana_task

    return delete_asana_task(task_gid=task_gid, approved=confirm, owner_ack=owner_ack)


def search_asana_tasks_action(*, query: str) -> dict[str, Any]:
    from app.integrations.asana_manager import search_asana_tasks

    return search_asana_tasks(query=query)


def move_asana_task_action(
    *,
    task_gid: str,
    section_gid: str = "",
    section_name: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    from app.integrations.asana_manager import move_asana_task_to_section

    return move_asana_task_to_section(
        task_gid=task_gid,
        section_gid=section_gid,
        section_name=section_name,
        approved=confirm,
    )


def comment_asana_task_action(*, task_gid: str, comment: str, confirm: bool = False, owner_ack: bool = False) -> dict[str, Any]:
    from app.integrations.asana_manager import comment_on_asana_task

    return comment_on_asana_task(task_gid=task_gid, comment=comment, approved=confirm, owner_ack=owner_ack)


def hubspot_status_action() -> dict[str, Any]:
    from app.integrations.hubspot_manager import hubspot_status_brief

    return hubspot_status_brief()


def hubspot_cleanup_proposals_action(*, inactive_days: int = 180) -> dict[str, Any]:
    from app.integrations.hubspot_manager import propose_inactive_cleanup

    return propose_inactive_cleanup(inactive_days=inactive_days)


def hubspot_outreach_batch_action(*, goal: str = "", limit: int = 10) -> dict[str, Any]:
    from app.integrations.hubspot_manager import propose_outreach_batch

    return propose_outreach_batch(goal=goal, limit=limit)


def hubspot_duplicate_merges_action(*, limit: int = 50) -> dict[str, Any]:
    from app.integrations.hubspot_manager import propose_duplicate_merges

    return propose_duplicate_merges(limit=limit)


def hubspot_lead_source_fills_action(*, limit: int = 25) -> dict[str, Any]:
    """Retained name; lead-source fills were retired for contact enrichment."""
    return hubspot_enrichment_action(limit=limit)


def hubspot_enrichment_action(*, limit: int = 25) -> dict[str, Any]:
    from app.integrations.hubspot_manager import propose_field_enrichment

    return propose_field_enrichment(limit=limit)


def hubspot_health_report_action(*, all_owners: bool = False) -> dict[str, Any]:
    from app.integrations.hubspot_manager import crm_health_report, kory_owner_id

    # "" means every owner; None would fall through to the Kory default.
    return crm_health_report(owner_id="" if all_owners else kory_owner_id())


def hubspot_find_contacts_action(
    *,
    company: str = "",
    quiet_days: int = 0,
    lifecycle: str = "",
    limit: int = 25,
    all_owners: bool = False,
) -> dict[str, Any]:
    from app.integrations.hubspot_manager import find_contacts

    return find_contacts(
        company=company,
        quiet_days=quiet_days,
        lifecycle=lifecycle,
        limit=limit,
        include_all_owners=all_owners,
    )


def hubspot_compare_books_action() -> dict[str, Any]:
    from app.integrations.hubspot_manager import compare_books

    return compare_books()


def hubspot_recent_changes_action(*, days: int = 7) -> dict[str, Any]:
    from app.integrations.hubspot_manager import recent_changes

    return recent_changes(days=days)


def hubspot_prebrief_enrich_action(*, email: str = "", name: str = "") -> dict[str, Any]:
    from app.integrations.hubspot_manager import enrich_prebrief_from_hubspot

    return enrich_prebrief_from_hubspot(email=email, name=name)


def hubspot_meeting_note_action(
    *,
    email: str,
    note: str,
    meeting_subject: str = "",
    confirm: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    from app.integrations.hubspot_manager import stage_meeting_note

    return stage_meeting_note(
        email=email,
        note=note,
        meeting_subject=meeting_subject,
        approved=confirm,
        owner_ack=owner_ack,
    )


def hubspot_outreach_candidates_action(
    *,
    goal: str = "",
    lifecycle: str = "",
    limit: int = 15,
) -> dict[str, Any]:
    from app.integrations.hubspot_manager import find_contacts_for_outreach

    return find_contacts_for_outreach(goal=goal, lifecycle=lifecycle, limit=limit)


def hubspot_deals_snapshot_action(*, limit: int = 8) -> dict[str, Any]:
    from app.integrations.hubspot_manager import deals_snapshot_for_brief

    return deals_snapshot_for_brief(limit=limit)


# ── Outreach campaigns (draft locally; no send / no Teams cards) ─────────────


def create_outreach_campaign_action(
    *,
    name: str,
    goal: str = "",
    template_key: str = "generic",
    pasted_list: str = "",
    hubspot_limit: int = 0,
    hubspot_lifecycle: str = "",
    include_research: bool = False,
    custom_opener: str = "",
    custom_subject: str = "",
) -> dict[str, Any]:
    from app.scheduling.outreach_campaign import create_outreach_campaign

    return create_outreach_campaign(
        name=name,
        goal=goal,
        template_key=template_key,
        pasted_list=pasted_list,
        hubspot_limit=hubspot_limit,
        hubspot_lifecycle=hubspot_lifecycle,
        include_research=include_research,
        custom_opener=custom_opener,
        custom_subject=custom_subject,
    )


def list_outreach_campaigns_action(*, limit: int = 20) -> dict[str, Any]:
    from app.scheduling.outreach_campaign import list_campaigns

    return list_campaigns(limit=limit)


def get_outreach_campaign_action(*, campaign_id: str) -> dict[str, Any]:
    from app.scheduling.outreach_campaign import get_campaign

    detail = get_campaign(campaign_id)
    if not detail:
        return {"ok": False, "error": f"Unknown campaign {campaign_id}"}
    camp = detail["campaign"]
    drafts = detail["drafts"]
    lines = [
        f"**Campaign** `{camp.get('campaign_id')}` — {camp.get('name')}",
        f"Status: {camp.get('status')} · Template: {camp.get('template_key')}",
        f"Drafts: {len(drafts)} · Sends blocked: {detail.get('sends_blocked')}",
        "",
    ]
    for d in drafts[:15]:
        lines.append(
            f"• {d.get('recipient_name')} <{d.get('recipient_email')}> — {d.get('subject')} "
            f"[{d.get('status')}]"
        )
    if len(drafts) > 15:
        lines.append(f"…and {len(drafts) - 15} more")
    return {
        "ok": True,
        **detail,
        "kory_message": "\n".join(lines),
    }


def approve_outreach_campaign_action(*, campaign_id: str, confirm: bool = False) -> dict[str, Any]:
    from app.scheduling.outreach_campaign import approve_outreach_campaign

    if not confirm:
        return {
            "ok": False,
            "error": "Set confirm=true to mark the campaign approved (still will not send).",
        }
    return approve_outreach_campaign(campaign_id=campaign_id, approved_by="kory")


def send_outreach_campaign_action(*, campaign_id: str, confirm: bool = False) -> dict[str, Any]:
    from app.scheduling.outreach_campaign import send_outreach_campaign

    return send_outreach_campaign(campaign_id=campaign_id, approved=confirm)


def remove_outreach_recipient_action(*, campaign_id: str, email: str) -> dict[str, Any]:
    from app.scheduling.outreach_campaign import remove_outreach_recipient

    return remove_outreach_recipient(campaign_id=campaign_id, email=email)


# ── Composio Search (web, travel, maps — read-only) ──────────────────────────


def web_search_action(query: str) -> dict[str, Any]:
    from app.integrations.composio_search import web_search

    try:
        return {"ok": True, "result": web_search(query)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def search_flights_action(query: str = "", payload_json: str = "{}") -> dict[str, Any]:
    from app.integrations.composio_search import parse_arguments_json, search_flights

    try:
        args = parse_arguments_json(payload_json)
        if query.strip():
            args.setdefault("query", query.strip())
        if not args.get("query") and not args.get("departure_id"):
            return {"ok": False, "error": "Provide query or departure_id/arrival_id/outbound_date."}
        return {"ok": True, "result": search_flights(args)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def search_hotels_action(payload_json: str) -> dict[str, Any]:
    from app.integrations.composio_search import parse_arguments_json, search_hotels

    try:
        return {"ok": True, "result": search_hotels(parse_arguments_json(payload_json))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def search_maps_action(query: str) -> dict[str, Any]:
    from app.integrations.composio_search import search_maps

    try:
        return {"ok": True, "result": search_maps(query)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def search_news_action(query: str) -> dict[str, Any]:
    from app.integrations.composio_search import search_news

    try:
        return {"ok": True, "result": search_news(query)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def fetch_url_content_action(url: str, max_characters: int = 8000) -> dict[str, Any]:
    from app.integrations.composio_search import fetch_url_content

    try:
        return {"ok": True, "result": fetch_url_content(url, max_characters=max_characters)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def execute_search_action_action(
    slug: str,
    arguments_json: str = "{}",
    allow_unlisted: bool = False,
) -> dict[str, Any]:
    from app.integrations.composio_search import execute_search_action, parse_arguments_json

    try:
        result = execute_search_action(
            slug,
            parse_arguments_json(arguments_json),
            allow_unlisted=allow_unlisted,
        )
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def get_family_calendar_status_action() -> dict[str, Any]:
    from app.integrations.family_google_calendar import family_calendar_status

    return {"ok": True, **family_calendar_status()}


def research_person_action(
    name: str,
    company: str = "",
    email: str = "",
    include_inbox: bool = True,
) -> dict[str, Any]:
    from app.integrations.person_research import research_person

    try:
        result = research_person(
            name,
            company=company,
            email=email,
            include_inbox=include_inbox,
        )
        return {"ok": True, "research": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}



def list_asana_projects_action() -> dict[str, Any]:
    from app.integrations.asana_manager import list_asana_project_options

    result = list_asana_project_options()
    projects = result.get("projects", [])
    return {
        "ok": True,
        "count": len(projects),
        "projects": [p.get("name") for p in projects],
        "note": "Reads cover all of these; new tasks go to Kory's personal project.",
    }


def list_asana_boards_action() -> dict[str, Any]:
    from app.integrations.asana_manager import list_project_sections

    sections = list_project_sections()
    return {
        "ok": True,
        "count": len(sections),
        "boards": [s.get("name") for s in sections],
    }
