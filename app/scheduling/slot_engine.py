"""Deterministic slot finder — calendar truth + Kory rules + optimized preferences."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import rules as kory_rules
from app.config import settings
from app.rules.validators import validate_proposal_slots
from app.scheduling.busy_intervals import local_dt, parse_iso_datetime, slot_conflicts_any_proposed, slot_conflicts_busy
from app.scheduling.calendar_intelligence import parse_duration_from_text
from app.scheduling.meeting_type import normalize_scheduling_intent, resolve_meeting_type
from app.scheduling.preferences import SchedulingPreferences, load_scheduling_preferences
from app.scheduling.scheduling_plan import SchedulingPlan
from app.scheduling.scheduling_window import (
    infer_allowed_weekdays,
    infer_scheduling_window,
    infer_time_of_day_window,
    slot_date_in_window,
    slot_start_in_time_window,
)

MT = ZoneInfo(settings.scheduling_timezone)
SLOT_STEP_MINUTES = 15
MIN_SLOT_OPTIONS = 2
MAX_SLOT_OPTIONS = 3

INTENT_NORMALIZE = {
    "dinner_request": "dinner",
    "lunch_request": "lunch",
    "pitch": "new_client",
    "meeting_request": "referral_or_intro",
    "referral": "referral_or_intro",
    "referral_or_intro": "referral_or_intro",
    "podcast": "podcast",
}


@dataclass
class SlotProposal:
    slots: list[dict[str, str]] = field(default_factory=list)
    meeting_format: str = "virtual"
    intent: str = "unknown"
    source: str = "slot_engine"
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _normalize_intent(intent: str | None, *, subject: str = "", body: str = "") -> str:
    if subject or body:
        return normalize_scheduling_intent(intent, subject=subject, body=body)
    key = (intent or "unknown").lower().replace(" ", "_")
    return INTENT_NORMALIZE.get(key, key)


def infer_meeting_format(
    intent: str,
    *,
    subject: str = "",
    body: str = "",
) -> str:
    """virtual vs in_person from intent and email cues."""
    key = _normalize_intent(intent)
    if key in {"coffee", "happy_hour", "dinner", "lunch"}:
        return "in_person"
    combined = f"{subject}\n{body}".lower()
    if any(
        cue in combined
        for cue in (
            "in person",
            "in-person",
            "at cherry creek",
            "coffee",
            "happy hour",
            "over dinner",
            "meet me at",
        )
    ):
        return "in_person"
    if re.search(r"\b(teams|zoom|virtual|phone call|call)\b", combined):
        return "virtual"
    return "virtual"


def _is_urgent(subject: str, body: str) -> bool:
    combined = f"{subject}\n{body}".lower()
    return any(
        cue in combined
        for cue in (
            "urgent",
            "asap",
            "time-sensitive",
            "this week only",
            "critical",
            "new client",
        )
    )


def _candidate_start_times(
    day_local: datetime,
    intent: str,
    meeting_format: str,
    *,
    east_coast: bool,
    urgent: bool,
    flexible_afternoon: bool = False,
) -> list[datetime]:
    weekday = day_local.strftime("%A")
    day_rules = kory_rules.DAILY_AVAILABILITY.get(weekday, {})
    if day_rules.get("available") is False and intent not in {"dinner"}:
        return []

    times: list[str] = []
    if intent == "coffee":
        times = list(kory_rules.MEETING_TYPES["coffee"].get("preferred_times", ["08:30", "09:00"]))
        if meeting_format == "in_person" and weekday in kory_rules.WORKOUT_DAYS:
            times = ["09:30"]
        if flexible_afternoon:
            times = list(dict.fromkeys(times + ["13:00", "14:00", "15:00"]))
    elif intent == "happy_hour":
        times = list(kory_rules.MEETING_TYPES["happy_hour"].get("preferred_times", ["15:30", "16:00"]))
    elif intent == "dinner":
        times = ["18:00", "18:30", "19:00"]
    elif intent == "podcast":
        times = ["07:00", "08:00", "09:00", "10:00", "11:00", "14:00", "15:00"]
        if weekday in kory_rules.EARLY_START_DAYS and (east_coast or urgent):
            times = ["06:00", "07:00"] + times
    elif intent in {"new_client"}:
        times = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]
    else:
        times = ["09:00", "10:00", "11:00", "13:00", "14:00", "15:00", "16:00"]
        if weekday in kory_rules.EARLY_START_DAYS:
            times = ["07:00", "08:00"] + times
            if east_coast or urgent:
                times = ["06:00"] + times
        if weekday in kory_rules.WORKOUT_DAYS and meeting_format == "virtual":
            times = ["08:00"] + [t for t in times if t >= "08:00"]

    starts: list[datetime] = []
    for hhmm in times:
        h, m = (int(x) for x in hhmm.split(":"))
        starts.append(
            day_local.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=MT)
        )
    return starts


def _score_slot(
    start_local: datetime,
    intent: str,
    meeting_format: str,
    *,
    east_coast: bool,
    day_index: int,
) -> float:
    score = 100.0 - day_index * 0.5
    weekday = start_local.strftime("%A")
    hm = start_local.strftime("%H:%M")

    if intent == "coffee" and hm in {"08:30", "09:00", "09:30"}:
        score += 20
    if intent == "happy_hour" and hm in {"15:30", "16:00"}:
        score += 20
    if intent == "dinner" and start_local.hour >= 18:
        score += 15
    if weekday in kory_rules.EARLY_START_DAYS and east_coast and start_local.hour in {6, 7}:
        score += 10
    if weekday == "Friday" and intent == "happy_hour":
        score -= 25
    if start_local.hour < 12:
        score += 5
    if meeting_format == "virtual" and start_local.hour in {9, 10, 11}:
        score += 3
    return score


def _slot_dict(start_local: datetime, block_minutes: int) -> dict[str, str]:
    end_local = start_local + timedelta(minutes=block_minutes)
    return {"start": start_local.isoformat(), "end": end_local.isoformat()}


def find_valid_slots(
    calendar_context: dict[str, Any],
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
    meeting_format: str | None = None,
    urgent: bool | None = None,
    min_slots: int = MIN_SLOT_OPTIONS,
    max_slots: int = MAX_SLOT_OPTIONS,
    preferences: SchedulingPreferences | None = None,
    reference_now: datetime | None = None,
    plan: SchedulingPlan | None = None,
    skip_time_of_day: bool = False,
) -> SlotProposal:
    """Return up to max_slots non-overlapping, rule-valid, calendar-free options."""
    intent_key = _normalize_intent(intent, subject=subject, body=body)
    meeting_spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
    fmt = (
        (plan.meeting_format if plan and plan.meeting_format else None)
        or meeting_format
        or infer_meeting_format(intent_key, subject=subject, body=body)
    )
    is_urgent = (
        plan.urgency
        if plan is not None
        else (urgent if urgent is not None else _is_urgent(subject, body))
    )
    prefs = preferences or load_scheduling_preferences(
        guidance=(plan.kory_guidance if plan else "")
    )
    busy = list(calendar_context.get("busy_events") or [])

    if calendar_context.get("status") != "available":
        return SlotProposal(
            intent=intent_key,
            meeting_format=fmt,
            diagnostics={"error": "calendar_unavailable"},
        )

    block_minutes = meeting_spec.duration_minutes
    reserve_minutes = meeting_spec.calendar_block_minutes
    if plan and plan.duration_minutes and not parse_duration_from_text(f"{subject}\n{body}"):
        if meeting_spec.type_key != "coffee":
            block_minutes = max(meeting_spec.duration_minutes, plan.duration_minutes)
            reserve_minutes = block_minutes

    horizon_days = int(calendar_context.get("horizon_days") or settings.lexi_calendar_search_days)
    now_mt = reference_now.astimezone(MT) if reference_now else datetime.now(tz=MT)
    earliest = now_mt + timedelta(hours=2)
    window = (plan.window if plan and plan.window else None)
    if window is None and not (plan and plan.source == "open_horizon"):
        # A plan that carries no window must not silently discard a timeframe the
        # sender stated ("next week") — that let offers land a week late.
        # "open_horizon" is the one exception: the fallback ladder strips the
        # window on purpose to search wider, and re-inferring it here rebuilt the
        # exact constraint the ladder was trying to escape.
        window = infer_scheduling_window(subject=subject, body=body, now=now_mt)
    if skip_time_of_day:
        time_window = None
    elif plan is not None and plan.time_window is not None:
        # LLM-extracted, code-clamped preference wins over the regex re-parse.
        time_window = plan.time_window
    else:
        time_window = infer_time_of_day_window(subject=subject, body=body)
    allowed_weekdays = infer_allowed_weekdays(subject=subject, body=body)
    east_coast = bool(
        re.search(r"\b(east coast|eastern|nyc|new york|boston|et)\b", f"{subject}\n{body}", re.I)
    )
    combined = f"{subject}\n{body}".lower()
    flexible_afternoon = intent_key == "coffee" and any(
        cue in combined for cue in ("afternoon", "early afternoon", "flexible", "either")
    )

    candidates: list[tuple[float, dict[str, str], str]] = []
    seen_days: set[str] = set()

    for day_offset in range(1, horizon_days + 1):
        day = (now_mt + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if window and not (window.start <= day.date() <= window.end):
            continue
        if allowed_weekdays is not None and day.weekday() not in allowed_weekdays:
            continue
        day_key = day.date().isoformat()
        for start_local in _candidate_start_times(
            day,
            intent_key,
            fmt,
            east_coast=east_coast,
            urgent=is_urgent,
            flexible_afternoon=flexible_afternoon,
        ):
            if start_local < earliest:
                continue
            if time_window and not slot_start_in_time_window(
                start_local, time_window, block_minutes=reserve_minutes
            ):
                continue
            slot = _slot_dict(start_local, block_minutes)
            if slot_conflicts_busy(slot, busy, reserve_minutes=reserve_minutes):
                continue
            check = validate_proposal_slots(
                [slot],
                intent=intent_key,
                meeting_format=fmt,
                urgent=is_urgent,
                east_coast=east_coast,
                busy_events=busy,
                preferences=prefs,
            )
            if not check.valid:
                continue
            score = _score_slot(
                start_local,
                intent_key,
                fmt,
                east_coast=east_coast,
                day_index=day_offset,
            )
            if day_key not in seen_days:
                score += 8
            candidates.append((score, slot, day_key))

    candidates.sort(key=lambda item: item[0], reverse=True)

    chosen: list[dict[str, str]] = []
    used_days: set[str] = set()
    used_cap_weeks: set[str] = set()

    def _slot_week_key(slot: dict[str, str]) -> str:
        from app.rules.validators import _week_key

        start = parse_iso_datetime(str(slot.get("start") or ""))
        return _week_key(local_dt(start)) if start else ""

    # First pass takes the best slot on each distinct day, so one bad day for the
    # recipient doesn't kill the whole offer. The fill pass below relaxes this
    # when a single day is all the calendar has.
    for score, slot, day_key in candidates:
        if day_key and day_key in used_days:
            continue
        if slot_conflicts_any_proposed(slot, chosen, reserve_minutes=reserve_minutes):
            continue
        if intent_key in {"happy_hour", "dinner", "dinner_request"}:
            week_key = _slot_week_key(slot)
            if week_key and week_key in used_cap_weeks:
                continue
        chosen.append(slot)
        used_days.add(day_key)
        if intent_key in {"happy_hour", "dinner", "dinner_request"}:
            week_key = _slot_week_key(slot)
            if week_key:
                used_cap_weeks.add(week_key)
        if len(chosen) >= max_slots:
            break

    if len(chosen) < max_slots:
        for score, slot, day_key in candidates:
            if slot in chosen:
                continue
            if window and not slot_date_in_window(slot, window):
                continue
            if slot_conflicts_any_proposed(slot, chosen, reserve_minutes=reserve_minutes):
                continue
            batch = chosen + [slot]
            check = validate_proposal_slots(
                [slot],
                intent=intent_key,
                meeting_format=fmt,
                urgent=is_urgent,
                east_coast=east_coast,
                busy_events=busy,
                preferences=prefs,
                batch_slots=batch,
            )
            if not check.valid:
                continue
            chosen.append(slot)
            if len(chosen) >= max_slots:
                break

    if len(chosen) < min_slots and intent_key == "coffee" and time_window and not skip_time_of_day:
        from app.scheduling.window_fallback import _plan_without_window

        relaxed_plan = _plan_without_window(plan) if plan else None
        relaxed = find_valid_slots(
            calendar_context,
            intent=intent,
            subject=subject,
            body=body,
            meeting_format=meeting_format,
            urgent=urgent,
            min_slots=min_slots,
            max_slots=max_slots,
            preferences=preferences,
            reference_now=reference_now,
            plan=relaxed_plan,
            skip_time_of_day=True,
        )
        if len(relaxed.slots) >= min_slots:
            relaxed.diagnostics["morning_preference_relaxed"] = True
            return relaxed

    # Offer them in the order the recipient will read them. Selection is by
    # score, which listed 11:00 AM above 7:00 AM in a real offer email.
    return SlotProposal(
        slots=sorted(chosen[:max_slots], key=lambda slot: str(slot.get("start") or "")),
        meeting_format=fmt,
        intent=intent_key,
        diagnostics={
            "candidates_scored": len(candidates),
            "urgent": is_urgent,
            "block_minutes": block_minutes,
            "reserve_minutes": reserve_minutes,
            "meeting_type": meeting_spec.type_key,
            "meeting_type_label": meeting_spec.label,
            "preferences_memory_count": len(prefs.memory_facts),
            "scheduling_window": (
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "source": window.source,
                    "label": window.label,
                }
                if window
                else None
            ),
            "time_of_day_window": (
                {
                    "start": f"{time_window.start_hour:02d}:{time_window.start_minute:02d}",
                    "end": f"{time_window.end_hour:02d}:{time_window.end_minute:02d}",
                    "label": time_window.label,
                }
                if time_window
                else None
            ),
            "allowed_weekdays": sorted(allowed_weekdays) if allowed_weekdays else None,
        },
    )


def propose_meeting_slots(
    calendar_context: dict[str, Any],
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
    meeting_format: str | None = None,
    urgent: bool | None = None,
    plan: SchedulingPlan | None = None,
) -> SlotProposal:
    """Public entry — returns validated slots or empty list."""
    from app.scheduling.scheduling_plan import SchedulingPlan
    from app.scheduling.window_fallback import _plan_without_window, _shift_plan_window

    result = find_valid_slots(
        calendar_context,
        intent=intent,
        subject=subject,
        body=body,
        meeting_format=meeting_format,
        urgent=urgent,
        plan=plan,
    )
    if len(result.slots) >= MIN_SLOT_OPTIONS:
        result.diagnostics["status"] = "ok"
        return result

    original_label = plan.window.label if plan and plan.window else None
    fallbacks: list[Any] = []
    if plan and plan.window:
        for week_offset in (1, 2, 3):
            fallbacks.append(_shift_plan_window(plan, week_offset=week_offset))
        fallbacks.append(_plan_without_window(plan))
    open_plan = _plan_without_window(plan) if plan else SchedulingPlan(
        task_type="offer_times",
        window=None,
        source="open_horizon",
    )
    fallbacks.append(open_plan)

    for alt_plan in fallbacks:
        alt = find_valid_slots(
            calendar_context,
            intent=intent,
            subject=subject,
            body=body,
            meeting_format=meeting_format,
            urgent=urgent,
            plan=alt_plan,
        )
        if len(alt.slots) >= MIN_SLOT_OPTIONS:
            alt.diagnostics["status"] = "ok"
            alt.diagnostics["window_expanded"] = True
            if original_label:
                alt.diagnostics["original_window"] = original_label
            if alt_plan and alt_plan.window:
                alt.diagnostics["expanded_window"] = alt_plan.window.label
            return alt

    result.diagnostics["status"] = "insufficient_slots"
    return result


def _duration_minutes_from_text(text: str) -> int | None:
    """Backward-compatible alias for tests and callers."""
    return parse_duration_from_text(text)
