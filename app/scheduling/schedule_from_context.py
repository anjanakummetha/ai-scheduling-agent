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
            # Name the dates actually offered — the ladder's rung label says
            # which window it SEARCHED, not where the surviving slots landed
            # (live defect: note said "week of August 11", slot was Aug 26).
            offering = self._offered_dates_label() or str(
                self.diagnostics.get("expanded_window") or ""
            ).strip()
            if requested:
                # Labels like "in the next few weeks" already carry their own
                # preposition — "no availability for in ..." is word salad.
                lead = (
                    f"no availability {requested}"
                    if requested.lower().startswith(("in ", "over ", "during "))
                    else f"no availability for {requested}"
                )
                return lead + (
                    f" — offering {offering} instead" if offering else " — offering the next open times"
                )
            return "Requested window had no availability — offering the next open times."
        return ""

    def _offered_dates_label(self) -> str:
        from app.scheduling.pre_approval_gate import offered_dates_label

        return offered_dates_label(self.slots)


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
    min_slot_options: int | None = None,
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
    from app.scheduling.scheduling_plan import apply_guidance_window

    apply_guidance_window(plan, kory_scheduling_guidance)
    # The travel shift is desired behavior (V-3: don't book travel weeks), but
    # it must not be SILENT: it replaces the sender's window before the engine
    # runs, so the gate honestly reports slots "in window" and no expansion
    # warning ever fires (live C-4 — offers a week late with no caveat).
    pre_shift_label = plan.window.label if plan.window else ""
    plan = maybe_shift_plan_window(plan, calendar_context.get("busy_events"))
    travel_shifted = bool(
        plan.window is not None
        and plan.window.source == "travel_shift"
        and plan.window.label != pre_shift_label
    )

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
            body,  # the SENDER's text only — see guidance handling below
            guidance=kory_scheduling_guidance,
            calendar_context=calendar_context,
            intent=intent,
            subject=subj,
            plan=plan,
            # Unlabeled inbound times are written in the sender's own zone —
            # but only when detection is confident; uncertain stays MT.
            default_tz=(tz_result.tz_name() if not uncertain_tz else None),
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
        min_options=min_slot_options,
    )
    meeting_fmt = engine.meeting_format or meeting_format or ""

    # Kory-directed searches may return a single slot (see propose_meeting_slots);
    # escalating "not enough options" back at him after he already chose is circular.
    from app.scheduling.preferences import guidance_relaxes_slot_minimum

    required_slots = (
        1
        if (plan is not None and guidance_relaxes_slot_minimum(plan.kory_guidance))
        else MIN_SLOTS
    )
    if len(engine.slots) < required_slots:
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
    window_expanded = (
        bool(engine.diagnostics.get("window_expanded"))
        or bool(engine.diagnostics.get("morning_preference_relaxed"))
        or travel_shifted
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
        original_window_label=str(
            engine.diagnostics.get("original_window")
            or (pre_shift_label if travel_shifted else "")
        ),
        expanded_window_label=str(
            engine.diagnostics.get("expanded_window")
            or (plan.window.label if travel_shifted and plan.window else "")
        ),
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


def _bare_clock_from_guidance(guidance: str) -> tuple[int, int] | None:
    """A single dateless clock time in Kory's directive ('either day at 9
    mountain', 'let's do 2 pm'). None when absent or ambiguous (2+ times)."""
    import re as _re

    from app.scheduling.inbound_availability import (
        _infer_bare_hour_meridiem,
        _normalize_shared_meridiem_ranges,
    )

    text = _infer_bare_hour_meridiem(_normalize_shared_meridiem_ranges(guidance))
    times = _re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, _re.I)
    if len(times) != 1:
        return None
    hour, minute, meridiem = int(times[0][0]), int(times[0][1] or 0), times[0][2].lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return (hour, minute) if 6 <= hour <= 21 else None


def _try_inbound_slots(
    body: str,
    *,
    guidance: str = "",
    calendar_context: dict[str, Any],
    intent: str | None,
    subject: str,
    plan: Any = None,
    default_tz: str | None = None,
) -> ScheduleFromContextResult | None:
    from app.scheduling.inbound_availability import (
        extract_inbound_time_candidates,
        validate_inbound_candidates,
    )

    # Sender candidates come from the sender's text alone. Kory's guidance
    # contributes a candidate when he names an explicit clock time ("offer
    # Monday 10:30 only" — live H-4) OR an explicit calendar DATE ("Most of
    # the 19th is open" — real Curtis thread). Weekday-only guidance
    # ("Tuesday or Wednesday, 45 minutes") stays a CONSTRAINT, not a
    # candidate — it used to stage a phantom 9 AM default instead of running
    # the engine with constraints (adversarial-review concern).
    candidates = extract_inbound_time_candidates(body, default_tz=default_tz)

    # A weekday that disagrees with its date ("Wednesday, September 10" when the
    # 10th is a Thursday) is a contradiction we must not resolve by picking one.
    # Drop those candidates so nothing is proposed off them, and hand Kory the
    # specific question — he can see the thread and we cannot.
    contradiction_notes: list[str] = []
    kept: list[dict] = []
    for cand in candidates:
        mismatch = cand.get("weekday_mismatch")
        if mismatch:
            contradiction_notes.append(
                f'They wrote "{mismatch["text"]}", but {mismatch["date"]} is a '
                f'{mismatch["actual"]}, not a {mismatch["stated"]}. '
                f"Which did they mean?"
            )
        else:
            kept.append(cand)
    candidates = kept

    if guidance.strip():
        seen = {c["start"] for c in candidates}
        guidance_cands = extract_inbound_time_candidates(
            guidance, include_flags=True  # Kory writes MT — no sender tz here
        )
        for cand in guidance_cands:
            explicit = cand.pop("explicit_time", False)
            kind = cand.pop("kind", "")
            if (explicit or kind in ("month", "ordinal", "mdy")) and cand[
                "start"
            ] not in seen:
                candidates.append(cand)
        if not guidance_cands and candidates:
            # "Either day works at 9 [mountain]" (real Curtis thread): a bare
            # clock with no date applies to the days already under
            # discussion — retime each sender-proposed day to Kory's hour.
            bare = _bare_clock_from_guidance(guidance)
            if bare is not None:
                from datetime import datetime as _dt

                hour, minute = bare
                retimed = []
                seen_days = set()
                for cand in candidates:
                    try:
                        start = _dt.fromisoformat(cand["start"])
                    except ValueError:
                        continue
                    if start.date() in seen_days:
                        continue
                    seen_days.add(start.date())
                    end = _dt.fromisoformat(cand["end"])
                    new_start = start.replace(hour=hour, minute=minute)
                    retimed.append(
                        {
                            "start": new_start.isoformat(),
                            "end": (new_start + (end - start)).isoformat(),
                            "source": "kory_guidance_clock",
                        }
                    )
                if retimed:
                    candidates = retimed
        # "make it 45 minutes" must resize the candidate slots too, or the
        # gate (which honors guided duration) refuses the sizes this path
        # staged (review concern: safe-side deadlock).
        from datetime import datetime, timedelta

        from app.scheduling.calendar_intelligence import parse_duration_from_text

        g_dur = parse_duration_from_text(guidance)
        if g_dur:
            for cand in candidates:
                try:
                    start_dt = datetime.fromisoformat(cand["start"])
                except (KeyError, ValueError):
                    continue
                cand["end"] = (start_dt + timedelta(minutes=g_dur)).isoformat()
    if not candidates and contradiction_notes:
        # Every time they named was self-contradictory. Do not fall through to
        # the engine and quietly invent times they never asked for — say what is
        # wrong and let Kory answer it.
        return ScheduleFromContextResult(
            ok=False,
            path="inbound_availability",
            status="weekday_date_contradiction",
            inbound_notes=contradiction_notes,
            failure_message=" ".join(contradiction_notes),
        )
    if not candidates:
        return None

    valid, invalid, notes = validate_inbound_candidates(
        candidates,
        calendar_context=calendar_context,
        intent=intent,
        subject=subject,
        body=body,
    )
    notes = contradiction_notes + list(notes)
    if valid:
        slots = valid[:MAX_SLOT_OPTIONS]
        # plan carries kory_guidance — without it the gate's 2-slot minimum
        # rejects a single Kory-directed time ("offer 10:30 only", live H-4).
        gate = verify_before_kory_approval(
            slots=slots,
            calendar_context=calendar_context,
            plan=plan,
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
