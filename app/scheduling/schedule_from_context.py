"""Unified scheduling — one engine path for email, Hermes chat, and delegation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.scheduling.pre_approval_gate import PreApprovalReport, verify_before_kory_approval
from app.scheduling.scheduling_plan import SchedulingPlan, build_scheduling_plan
from app.scheduling.slot_engine import MAX_SLOT_OPTIONS, MIN_SLOT_OPTIONS, propose_meeting_slots
from app.scheduling.travel_window import maybe_shift_plan_window
from app.scheduling.window_fallback import build_failure_kory_message

MIN_SLOTS = MIN_SLOT_OPTIONS


def _window_from_diagnostics(diagnostics: dict[str, Any]) -> Any:
    """Rebuild the window the slot engine used, as recorded in its diagnostics."""
    from datetime import date

    from app.scheduling.scheduling_window import SchedulingWindow

    raw = diagnostics.get("scheduling_window")
    if not isinstance(raw, dict):
        return None
    try:
        return SchedulingWindow(
            start=date.fromisoformat(str(raw["start"])),
            end=date.fromisoformat(str(raw["end"])),
            source=str(raw.get("source") or "engine"),
            label=str(raw.get("label") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class ScheduleFromContextResult:
    ok: bool
    slots: list[dict[str, str]] = field(default_factory=list)
    path: str = "unknown"
    status: str = "unknown"
    diagnostics: dict[str, Any] = field(default_factory=dict)
    plan: SchedulingPlan | None = None
    calendar_context: dict[str, Any] = field(default_factory=dict)
    meeting_format: str = ""
    gate: PreApprovalReport | None = None
    failure_message: str = ""
    recipient_timezone: str | None = None
    recipient_timezone_source: str = ""
    recipient_timezone_confidence: str = ""
    timezone_uncertain: bool = False
    formatted_slots: list[str] = field(default_factory=list)
    inbound_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "path": self.path,
            "slots": self.slots,
            "formatted_slots": self.formatted_slots,
            "diagnostics": self.diagnostics,
            "failure_message": self.failure_message,
            "meeting_format": self.meeting_format,
            "recipient_timezone": self.recipient_timezone,
            "recipient_timezone_source": self.recipient_timezone_source,
            "recipient_timezone_confidence": self.recipient_timezone_confidence,
            "timezone_uncertain": self.timezone_uncertain,
            "gate": self.gate.summary() if self.gate else None,
            "inbound_notes": self.inbound_notes,
            "horizon_days": self.calendar_context.get("horizon_days"),
            "busy_event_count": len(self.calendar_context.get("busy_events") or []),
        }

    def scheduling_note(self) -> str:
        """The caveat Kory must see before approving (e.g. window expansion).

        Single source of truth — every path that persists a proposal draft must
        store this, or the gate's warning is computed and then lost (the C-1/C-2
        live defect: offers landed outside the requested window with no note).
        """
        if self.gate is not None and self.gate.warnings:
            return "; ".join(self.gate.warnings)
        if bool(self.diagnostics.get("window_expanded")):
            requested = str(
                self.diagnostics.get("original_window")
                or (self.plan.window.label if self.plan and self.plan.window else "")
            ).strip()
            offering = str(self.diagnostics.get("expanded_window") or "").strip()
            if requested:
                return f"no availability for {requested}" + (
                    f" — offering {offering} instead" if offering else " — offering the next open times"
                )
            return "Requested window had no availability — offering the next open times."
        return ""


def merge_scheduling_body(body: str, kory_scheduling_guidance: str = "") -> str:
    base = (body or "").strip()
    guidance = (kory_scheduling_guidance or "").strip()
    if guidance:
        return f"{base}\n\nKory (scheduling guidance): {guidance}".strip()
    return base


def schedule_from_context(
    *,
    subject: str = "",
    body: str = "",
    intent: str | None = None,
    sender_email: str | None = None,
    meeting_format: str | None = None,
    kory_scheduling_guidance: str = "",
    stored_recipient_timezone: str | None = None,
    internet_headers: list[dict[str, Any]] | None = None,
    received_at: str | None = None,
    use_llm_plan: bool | None = None,
    try_inbound_availability: bool = True,
    format_slots: bool = True,
    calendar_context: dict[str, Any] | None = None,
) -> ScheduleFromContextResult:
    """Mandatory engine path — calendar load, plan, travel shift, slot_engine, validators."""
    from app.scheduling.calendar_context import load_scheduling_calendar_context
    from app.scheduling.email_format import format_slot_for_email
    from app.scheduling.inbound_availability import (
        body_looks_like_inbound_availability,
        extract_inbound_time_candidates,
        validate_inbound_candidates,
    )
    from app.scheduling.timezone_intel import (
        detect_recipient_timezone,
        is_timezone_uncertain,
    )

    scheduling_body = merge_scheduling_body(body, kory_scheduling_guidance)
    subj = (subject or "").strip()

    tz_result = detect_recipient_timezone(
        sender_email=sender_email,
        body=scheduling_body,
        internet_headers=internet_headers,
        stored_timezone=stored_recipient_timezone,
        allow_prior_threads=True,
    )
    uncertain_tz = is_timezone_uncertain(tz_result)

    calendar_context = calendar_context or load_scheduling_calendar_context(
        subject=subj, body=scheduling_body
    )
    if calendar_context.get("status") != "available":
        detail = calendar_context.get("error") or calendar_context.get("source") or "unavailable"
        return ScheduleFromContextResult(
            ok=False,
            path="calendar_unavailable",
            status="calendar_unavailable",
            calendar_context=calendar_context,
            failure_message=f"Calendar unavailable: {detail}",
            recipient_timezone=tz_result.tz_name(),
            recipient_timezone_source=tz_result.source,
            recipient_timezone_confidence=tz_result.confidence,
            timezone_uncertain=uncertain_tz,
        )

    llm_plan = use_llm_plan if use_llm_plan is not None else bool(settings.llm_api_key)
    plan = build_scheduling_plan(
        subject=subj,
        body=scheduling_body,
        intent=intent,
        use_llm=llm_plan,
    )
    plan = maybe_shift_plan_window(plan, calendar_context.get("busy_events"))

    if plan.task_type != "offer_times":
        return ScheduleFromContextResult(
            ok=False,
            path="plan_non_scheduling",
            status="non_scheduling",
            plan=plan,
            calendar_context=calendar_context,
            diagnostics={"task_type": plan.task_type, "draft_context": plan.draft_context},
            recipient_timezone=tz_result.tz_name(),
            recipient_timezone_source=tz_result.source,
            recipient_timezone_confidence=tz_result.confidence,
            timezone_uncertain=uncertain_tz,
        )

    inbound_notes: list[str] = []
    if try_inbound_availability and body_looks_like_inbound_availability(scheduling_body):
        inbound = _try_inbound_slots(
            scheduling_body,
            calendar_context=calendar_context,
            intent=intent,
            subject=subj,
        )
        if inbound and inbound.ok:
            inbound.recipient_timezone = tz_result.tz_name()
            inbound.recipient_timezone_source = tz_result.source
            inbound.recipient_timezone_confidence = tz_result.confidence
            inbound.timezone_uncertain = uncertain_tz
            inbound.plan = plan
            inbound.calendar_context = calendar_context
            if format_slots and inbound.slots:
                inbound.formatted_slots = _format_slots(
                    inbound.slots,
                    recipient_tz=tz_result.timezone,
                    uncertain=uncertain_tz,
                    sender_email=sender_email,
                    tz_confidence=tz_result.confidence,
                    tz_source=tz_result.source,
                    intent=intent or "",
                    meeting_format=meeting_format or "",
                )
            return inbound
        if inbound and inbound.inbound_notes:
            inbound_notes = list(inbound.inbound_notes)

    engine = propose_meeting_slots(
        calendar_context,
        intent=intent,
        subject=subj,
        body=scheduling_body,
        meeting_format=meeting_format,
        plan=plan,
    )
    meeting_fmt = engine.meeting_format or meeting_format or ""

    if len(engine.slots) < MIN_SLOTS:
        label = plan.window.label if plan and plan.window else None
        failure = build_failure_kory_message(
            intent=str(intent or ""),
            requested_label=label,
        )
        detail = f"{failure} Engine diagnostics: {engine.diagnostics}"
        if inbound_notes:
            detail = (
                "Prospect times did not work; no alternatives found. "
                + "; ".join(inbound_notes[:3])
                + " "
                + detail
            )
        return ScheduleFromContextResult(
            ok=False,
            path="slot_engine",
            status=str(engine.diagnostics.get("status") or "insufficient_slots"),
            slots=engine.slots,
            plan=plan,
            calendar_context=calendar_context,
            meeting_format=meeting_fmt,
            diagnostics=dict(engine.diagnostics),
            failure_message=detail,
            inbound_notes=inbound_notes,
            recipient_timezone=tz_result.tz_name(),
            recipient_timezone_source=tz_result.source,
            recipient_timezone_confidence=tz_result.confidence,
            timezone_uncertain=uncertain_tz,
        )

    slots = engine.slots[:MAX_SLOT_OPTIONS]
    # window_expanded means the engine *deliberately* searched wider. It must not
    # be set just because slots landed outside the requested window: doing that
    # converted a violation into an accepted expansion, so a sender who asked for
    # one week silently got offers three weeks out and the gate still reported ok.
    window_expanded = bool(engine.diagnostics.get("window_expanded")) or bool(
        engine.diagnostics.get("morning_preference_relaxed")
    )
    # The engine infers its own window when the plan carries none, so read back
    # whichever one actually applied rather than trusting plan.window.
    effective_window = (plan.window if (plan and plan.window) else None) or _window_from_diagnostics(
        engine.diagnostics
    )

    gate = verify_before_kory_approval(
        slots=slots,
        calendar_context=calendar_context,
        plan=plan,
        window=effective_window,
        original_window_label=str(engine.diagnostics.get("original_window") or ""),
        expanded_window_label=str(engine.diagnostics.get("expanded_window") or ""),
        intent=intent,
        subject=subj,
        body=scheduling_body,
        meeting_format=meeting_fmt,
        window_expanded=window_expanded,
    )
    if not gate.ok:
        return ScheduleFromContextResult(
            ok=False,
            path="slot_engine",
            status="gate_blocked",
            slots=slots,
            plan=plan,
            calendar_context=calendar_context,
            meeting_format=meeting_fmt,
            gate=gate,
            diagnostics=dict(engine.diagnostics),
            failure_message=gate.summary(),
            recipient_timezone=tz_result.tz_name(),
            recipient_timezone_source=tz_result.source,
            recipient_timezone_confidence=tz_result.confidence,
            timezone_uncertain=uncertain_tz,
        )

    formatted: list[str] = []
    if format_slots:
        formatted = _format_slots(
            slots,
            recipient_tz=tz_result.timezone,
            uncertain=uncertain_tz,
            sender_email=sender_email,
            tz_confidence=tz_result.confidence,
            tz_source=tz_result.source,
            intent=intent or "",
            meeting_format=meeting_fmt,
        )

    path_label = "slot_engine_after_inbound" if inbound_notes else "slot_engine"

    return ScheduleFromContextResult(
        ok=True,
        slots=slots,
        path=path_label,
        status="ok",
        plan=plan,
        calendar_context=calendar_context,
        meeting_format=meeting_fmt,
        gate=gate,
        diagnostics=dict(engine.diagnostics),
        formatted_slots=formatted,
        recipient_timezone=tz_result.tz_name(),
        recipient_timezone_source=tz_result.source,
        recipient_timezone_confidence=tz_result.confidence,
        timezone_uncertain=uncertain_tz,
        inbound_notes=inbound_notes,
    )


def _try_inbound_slots(
    body: str,
    *,
    calendar_context: dict[str, Any],
    intent: str | None,
    subject: str,
) -> ScheduleFromContextResult | None:
    from app.scheduling.inbound_availability import (
        extract_inbound_time_candidates,
        validate_inbound_candidates,
    )

    candidates = extract_inbound_time_candidates(body)
    if not candidates:
        return None

    valid, invalid, notes = validate_inbound_candidates(
        candidates,
        calendar_context=calendar_context,
        intent=intent,
        subject=subject,
        body=body,
    )
    if valid:
        slots = valid[:MAX_SLOT_OPTIONS]
        gate = verify_before_kory_approval(
            slots=slots,
            calendar_context=calendar_context,
            intent=intent,
            subject=subject,
            body=body,
        )
        if not gate.ok:
            return ScheduleFromContextResult(
                ok=False,
                path="inbound_availability",
                status="gate_blocked",
                slots=slots,
                gate=gate,
                inbound_notes=notes,
                failure_message=gate.summary(),
            )
        return ScheduleFromContextResult(
            ok=True,
            slots=slots,
            path="inbound_availability",
            status="ok",
            gate=gate,
            inbound_notes=notes,
        )

    # Proposed times were busy/non-compliant — honor the proposed DATES by finding
    # a compliant open slot on each (like Heidi offering a specific time on the day
    # the prospect asked for, rather than a random earlier date).
    from datetime import datetime as _dt

    from app.scheduling.inbound_availability import find_compliant_slots_on_date

    on_date: list[dict[str, str]] = []
    seen_starts: set[str] = set()
    seen_dates: set[str] = set()
    for cand in candidates:
        try:
            d = _dt.fromisoformat(str(cand["start"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        key = d.date().isoformat()
        if key in seen_dates:
            continue
        seen_dates.add(key)
        # Pull a few options on this proposed date so the offer has >= MIN options
        # even when only one of the proposed dates is open (like Heidi offering a
        # couple of times on the day the prospect asked for).
        for slot in find_compliant_slots_on_date(
            d, calendar_context=calendar_context, intent=intent,
            subject=subject, body=body, near_hour=d.hour, limit=MAX_SLOT_OPTIONS,
        ):
            if slot["start"] not in seen_starts:
                seen_starts.add(slot["start"])
                on_date.append(slot)
        if len(on_date) >= MAX_SLOT_OPTIONS:
            break
    on_date = on_date[:MAX_SLOT_OPTIONS]
    if on_date:
        gate = verify_before_kory_approval(
            slots=on_date, calendar_context=calendar_context,
            intent=intent, subject=subject, body=body,
        )
        if gate.ok:
            return ScheduleFromContextResult(
                ok=True, slots=on_date, path="inbound_availability_on_date",
                status="ok", gate=gate, inbound_notes=notes,
            )

    if invalid:
        return ScheduleFromContextResult(
            ok=False,
            path="inbound_availability",
            status="inbound_times_invalid",
            slots=invalid[:3],
            inbound_notes=notes,
            failure_message=(
                "Prospect proposed times but none meet calendar/rules: "
                + "; ".join(notes[:4])
            ),
        )
    return None


def _format_slots(
    slots: list[dict[str, str]],
    *,
    recipient_tz: Any,
    uncertain: bool,
    sender_email: str | None = None,
    tz_confidence: str = "",
    tz_source: str = "",
    intent: str = "",
    meeting_format: str = "",
) -> list[str]:
    from app.config import settings
    from app.scheduling.email_format import format_slot_for_email, should_note_mt_only_timezone
    from zoneinfo import ZoneInfo

    mt = ZoneInfo(settings.scheduling_timezone)
    mt_only = should_note_mt_only_timezone(
        sender_email=sender_email,
        uncertain=uncertain,
        tz_confidence=tz_confidence,
        tz_source=tz_source,
    )
    tz = mt if mt_only else (recipient_tz or mt)
    return [format_slot_for_email(slot, recipient_tz=tz) for slot in slots[:3]]
