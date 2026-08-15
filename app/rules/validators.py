"""Deterministic Kory scheduling validators (rules.py → runtime checks)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import rules as kory_rules
from app.scheduling.busy_intervals import (
    events_on_local_date,
    intervals_overlap,
    local_dt,
    parse_event_datetime,
    parse_iso_datetime,
    slot_interval,
)
from app.scheduling.preferences import SchedulingPreferences, load_scheduling_preferences

DINNER_INTENTS = frozenset({"dinner_request", "dinner"})
EVENING_INTENTS = frozenset(DINNER_INTENTS | {"happy_hour"})
IN_PERSON_INTENTS = frozenset(
    {"coffee", "happy_hour", "dinner", "dinner_request", "lunch", "lunch_request", "new_client"}
)
VIRTUAL_INFORMAL_INTENTS = frozenset(
    {
        "virtual_30",
        "referral_or_intro",
        "meeting_request",
        "internal_sync",
        "delegation",
        "reschedule",
        "unknown",
        "podcast",
    }
)


@dataclass
class ValidationResult:
    valid: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rules_checked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "violations": self.violations,
            "warnings": self.warnings,
            "rules_checked": self.rules_checked,
        }


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = (int(x) for x in value.split(":"))
    return hour, minute


def _slot_overlaps_block(
    start_local: datetime,
    end_local: datetime,
    *,
    block_start: str,
    block_end: str,
) -> bool:
    bs_h, bs_m = _parse_hhmm(block_start)
    be_h, be_m = _parse_hhmm(block_end)
    block_start_dt = start_local.replace(hour=bs_h, minute=bs_m, second=0, microsecond=0)
    block_end_dt = start_local.replace(hour=be_h, minute=be_m, second=0, microsecond=0)
    return start_local < block_end_dt and end_local > block_start_dt


def _check_timed_hard_blocks(
    start_local: datetime,
    end_local: datetime,
    weekday: str,
    prefix: str,
    result: ValidationResult,
) -> None:
    result.rules_checked.append("hard_blocks")
    for block in kory_rules.HARD_BLOCKS:
        days = block.get("days")
        block_start = block.get("start")
        block_end = block.get("end")
        if not days or not block_start or not block_end:
            continue
        if weekday not in days:
            continue
        if _slot_overlaps_block(
            start_local,
            end_local,
            block_start=block_start,
            block_end=block_end,
        ):
            result.valid = False
            result.violations.append(
                f"{prefix}: overlaps hard block '{block.get('name')}' "
                f"({block_start}–{block_end} {weekday})."
            )


_TRAVEL_SUBJECT_HINTS = (
    "flight to",
    "flight from",
    "stay at",
    "safari",
    "hotel check-in",
)

# A trip leg (a flight) only rules out the hours around it; an all-day or
# multi-hour travel block ("Kory in CA - All Day") rules out the whole day.
_TRAVEL_DAY_MIN_HOURS = 6
_TRAVEL_LEG_BUFFER = timedelta(hours=3)


def _looks_like_travel_event(event: dict[str, Any]) -> bool:
    if str(event.get("blocking_class") or "") == "travel_blocking":
        return True
    subject = str(event.get("subject") or "").lower()
    return any(hint in subject for hint in _TRAVEL_SUBJECT_HINTS)


def _travel_blocks_slot(
    slot_start: datetime,
    slot_end: datetime,
    busy_events: list[dict[str, Any]],
) -> bool:
    """True when travel genuinely rules this slot out.

    Previously any travel-ish event blocked its whole day, so a 9:05 PM flight
    killed an 8:00 AM meeting. "check-in" was also treated as a travel hint,
    which matched Kory's recurring "IFG + Sujash | Check-in" and quietly marked
    ordinary workdays as travel.
    """
    for event in events_on_local_date(busy_events, slot_start):
        if not _looks_like_travel_event(event):
            continue
        start = parse_event_datetime(event.get("start"))
        end = parse_event_datetime(event.get("end"))
        if not start or not end:
            return True  # can't bound it — stay conservative
        start_local, end_local = local_dt(start), local_dt(end)
        spans_day = bool(event.get("isAllDay")) or (
            end_local - start_local
        ) >= timedelta(hours=_TRAVEL_DAY_MIN_HOURS)
        if spans_day:
            return True
        if (
            slot_end > start_local - _TRAVEL_LEG_BUFFER
            and slot_start < end_local + _TRAVEL_LEG_BUFFER
        ):
            return True
    return False


def _week_key(day_local: datetime) -> str:
    iso = day_local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _count_weekly_pattern(
    busy_events: list[dict[str, Any]],
    week_key: str,
    pattern: re.Pattern[str],
) -> int:
    count = 0
    for event in busy_events:
        start = parse_event_datetime(event.get("start"))
        if not start:
            continue
        if _week_key(local_dt(start)) != week_key:
            continue
        if pattern.search(str(event.get("subject") or "")):
            count += 1
    return count


def _happy_hour_on_day(day_local: datetime, busy_events: list[dict[str, Any]]) -> datetime | None:
    for event in events_on_local_date(busy_events, day_local):
        if re.search(r"happy\s*hour", str(event.get("subject") or ""), re.I):
            start = parse_event_datetime(event.get("start"))
            if start:
                return local_dt(start)
    return None


_COFFEE_SUBJECT = re.compile(r"\bcoffee\b", re.I)
_IN_PERSON_EVENT = re.compile(
    r"coffee|happy\s*hour|\bdinner\b|\blunch\b|cherry creek|olive|aviano|grill|hillstone|in person",
    re.I,
)
_BREAK_EVENT = re.compile(r"\bwob\b|inbox\s+review|personal\s+training|trainer", re.I)


def _is_in_person_event(event: dict[str, Any]) -> bool:
    subject = str(event.get("subject") or "")
    if _IN_PERSON_EVENT.search(subject):
        return True
    return str(event.get("blocking_class") or "") == "in_person"


def _is_break_event(event: dict[str, Any]) -> bool:
    return bool(_BREAK_EVENT.search(str(event.get("subject") or "")))


def _default_drive_minutes() -> int:
    return int(kory_rules.TRAVEL_TIMES.get("Cherry Creek", {}).get("drive_minutes", 15))


def _drive_time_conflict(
    slot_start: datetime,
    slot_end: datetime,
    busy_events: list[dict[str, Any]],
    *,
    fmt: str,
    intent_key: str,
) -> bool:
    """Require drive gap between consecutive in-person meetings (Cherry Creek default)."""
    if fmt != "in_person" and intent_key not in IN_PERSON_INTENTS:
        return False
    drive = _default_drive_minutes()
    for event in events_on_local_date(busy_events, slot_start):
        if not _is_in_person_event(event):
            continue
        event_start = parse_event_datetime(event.get("start"))
        event_end = parse_event_datetime(event.get("end"))
        if not event_start or not event_end:
            continue
        event_start = local_dt(event_start)
        event_end = local_dt(event_end)
        if event_end <= slot_start:
            gap = int((slot_start - event_end).total_seconds() // 60)
            if 0 <= gap < drive:
                return True
        if event_start >= slot_end:
            gap = int((event_start - slot_end).total_seconds() // 60)
            if 0 <= gap < drive:
                return True
    return False


def _virtual_back_to_back_conflict(
    slot_start: datetime,
    busy_events: list[dict[str, Any]],
) -> bool:
    """After 2 hours of back-to-back meetings, Kory needs a 30-minute break."""
    max_block = int(kory_rules.BUFFER_RULES.get("max_back_to_back_block_hours", 2)) * 60
    break_minutes = int(kory_rules.BUFFER_RULES.get("break_after_block_minutes", 30))
    day_events: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for event in events_on_local_date(busy_events, slot_start):
        if _is_break_event(event):
            continue
        event_start = parse_event_datetime(event.get("start"))
        event_end = parse_event_datetime(event.get("end"))
        if not event_start or not event_end:
            continue
        day_events.append((local_dt(event_start), local_dt(event_end), event))
    day_events.sort(key=lambda item: item[1])

    chain_end = slot_start
    chain_minutes = 0
    for event_start, event_end, _event in reversed(day_events):
        if event_end > chain_end:
            continue
        gap = int((chain_end - event_end).total_seconds() // 60)
        if gap > 5:
            break
        chain_minutes += int((event_end - event_start).total_seconds() // 60)
        chain_end = event_start
        if chain_minutes >= max_block:
            break

    if chain_minutes < max_block:
        return False
    gap_before = int((slot_start - chain_end).total_seconds() // 60)
    return gap_before < break_minutes


def _is_coffee_event(event: dict[str, Any]) -> bool:
    return bool(_COFFEE_SUBJECT.search(str(event.get("subject") or "")))


def _coffee_post_buffer_minutes() -> int:
    return int(kory_rules.BUFFER_RULES.get("coffee_post_buffer_minutes", 30))


def _coffee_meeting_duration_minutes() -> int:
    return int(kory_rules.MEETING_TYPES.get("coffee", {}).get("duration_minutes") or 60)


def _coffee_buffer_conflict(
    slot_start: datetime,
    busy_events: list[dict[str, Any]],
) -> bool:
    """True when slot starts inside the post-coffee buffer of an existing coffee."""
    buffer_minutes = _coffee_post_buffer_minutes()
    for event in busy_events:
        if not _is_coffee_event(event):
            continue
        event_start = parse_event_datetime(event.get("start"))
        event_end = parse_event_datetime(event.get("end"))
        if not event_start or not event_end:
            continue
        event_end_local = local_dt(event_end)
        buffer_end = event_end_local + timedelta(minutes=buffer_minutes)
        slot_start_local = local_dt(slot_start)
        if event_end_local <= slot_start_local < buffer_end:
            return True
    return False


def _infer_format(intent_key: str, meeting_format: str | None) -> str:
    if meeting_format in {"virtual", "in_person"}:
        return meeting_format
    if intent_key in IN_PERSON_INTENTS and intent_key not in VIRTUAL_INFORMAL_INTENTS:
        if intent_key in {"lunch", "lunch_request"}:
            return "in_person"
        return "in_person"
    return "virtual"


def _preferred_coffee_times() -> set[str]:
    raw = kory_rules.MEETING_TYPES.get("coffee", {}).get("preferred_times", ["08:30", "09:00"])
    return {str(value).strip() for value in raw if str(value).strip()}


# Meetings Kory needs decompression time after (Kory's rule 2026-08-11, saved
# in kory_memory as more_braver_buffer): nothing may START inside the buffer
# window following the event's end.
_POST_MEETING_BUFFER_RULES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bmore\s+braver\b", re.IGNORECASE), 30),
)


def _post_meeting_buffer_violation(
    slot_start: datetime, busy_events: list[dict[str, Any]]
) -> str | None:
    for event in busy_events:
        subject = str(event.get("subject") or "")
        for pattern, buffer_minutes in _POST_MEETING_BUFFER_RULES:
            if not pattern.search(subject):
                continue
            event_end = parse_event_datetime(event.get("end"))
            if not event_end:
                continue
            if event_end <= slot_start < event_end + timedelta(minutes=buffer_minutes):
                return (
                    f"starts within {buffer_minutes} min of \"{subject.strip()}\" ending — "
                    f"Kory needs a {buffer_minutes}-minute buffer after it."
                )
    return None


def validate_proposal_slots(
    slots: list[dict[str, str]],
    *,
    intent: str | None = None,
    meeting_format: str | None = None,
    urgent: bool = False,
    east_coast: bool = False,
    busy_events: list[dict[str, Any]] | None = None,
    preferences: SchedulingPreferences | None = None,
    batch_slots: list[dict[str, str]] | None = None,
) -> ValidationResult:
    """Validate proposed slots against Kory rules before staging approval."""
    result = ValidationResult(valid=True)
    intent_key = (intent or "unknown").lower().replace(" ", "_")
    fmt = _infer_format(intent_key, meeting_format)
    busy = list(busy_events or [])
    prefs = preferences or load_scheduling_preferences()
    all_batch = list(batch_slots or slots)

    if not slots:
        result.valid = False
        result.violations.append("No slots proposed.")
        return result

    # Ruling 2026-08-08: urgency in a sender's email NEVER self-relaxes gates
    # (anyone could type "urgent"). It surfaces as a warning so Kory can grant
    # the exception; the lunch/travel/6AM rules below hold regardless.
    if urgent:
        result.warnings.append(
            "Sender flags urgency — no rules were auto-relaxed. If Kory wants an "
            "exception (lunch, travel week, 6 AM), tell Lexi and she will re-run."
        )

    if intent_key in {"lunch", "lunch_request"} and not prefs.lunch_allowed:
        result.valid = False
        result.violations.append("Lunch meetings are exception-only — route to Kory.")

    for index, slot in enumerate(slots, start=1):
        start = parse_iso_datetime(str(slot.get("start") or ""))
        end = parse_iso_datetime(str(slot.get("end") or ""))
        if not start or not end:
            result.valid = False
            result.violations.append(f"Option {index}: invalid ISO start/end.")
            continue

        start_local = local_dt(start)
        end_local = local_dt(end)
        weekday = start_local.strftime("%A")
        prefix = f"Option {index} ({weekday} {start_local.strftime('%H:%M')} MT)"

        result.rules_checked.append("calendar_conflict")
        if busy and slot_conflicts_busy(slot, busy):
            result.valid = False
            result.violations.append(f"{prefix}: overlaps Kory's calendar.")

        result.rules_checked.append("post_meeting_buffer")
        buffer_hit = _post_meeting_buffer_violation(start, busy)
        if buffer_hit:
            result.valid = False
            result.violations.append(f"{prefix}: {buffer_hit}")

        _check_timed_hard_blocks(start_local, end_local, weekday, prefix, result)

        result.rules_checked.append("travel_day")
        if _travel_blocks_slot(start_local, end_local, busy):
            result.valid = False
            result.violations.append(
                f"{prefix}: Kory is traveling this day — hold for Kory. Travel weeks are "
                f"2–3 critical check-ins only, so confirm with him before offering a time."
            )

        result.rules_checked.append("weekend_availability")
        day_rules = kory_rules.DAILY_AVAILABILITY.get(weekday, {})
        if day_rules.get("available") is False and intent_key not in DINNER_INTENTS:
            result.valid = False
            result.violations.append(f"{prefix}: weekend meetings are not allowed by default.")

        result.rules_checked.append("six_pm_cutoff")
        if intent_key not in DINNER_INTENTS:
            if start_local.hour >= 18 or (end_local.hour == 18 and end_local.minute > 0):
                result.valid = False
                result.violations.append(
                    f"{prefix}: after 6 PM is only allowed for planned dinners."
                )
            elif end_local.hour > 18:
                result.valid = False
                result.violations.append(f"{prefix}: ends after 6 PM (non-dinner).")

        result.rules_checked.append("earliest_by_day")
        if weekday in kory_rules.WORKOUT_DAYS:
            if fmt == "virtual" and intent_key in VIRTUAL_INFORMAL_INTENTS | {"virtual_30", "unknown"}:
                earliest = day_rules.get("earliest_virtual_informal", "08:00")
            else:
                earliest = day_rules.get("earliest_formal_inperson", "09:30")
            eh, em = _parse_hhmm(earliest)
            if start_local.hour < eh or (start_local.hour == eh and start_local.minute < em):
                result.valid = False
                result.violations.append(f"{prefix}: earliest on {weekday} is {earliest} for this format.")
        elif weekday in kory_rules.EARLY_START_DAYS:
            # Kory's spec for Tue/Thu: 7:00 AM is an acceptable occasional early start;
            # 6:00 AM is allowed for East-Coast contacts or urgent requests; anything
            # earlier is out. Coffee still prefers its 8:30/9:00 windows.
            if intent_key == "coffee" and start_local.strftime("%H:%M") in _preferred_coffee_times():
                pass
            elif start_local.hour < 6:
                result.valid = False
                result.violations.append(
                    f"{prefix}: earliest on {weekday} is 6:00 AM (East Coast) / 7:00 AM."
                )
            elif start_local.hour == 6 and not east_coast:
                result.valid = False
                result.violations.append(
                    f"{prefix}: 6 AM on {weekday} is only for East Coast contacts."
                )

        result.rules_checked.append("happy_hour_rules")
        if intent_key == "happy_hour":
            if end_local.hour > 18 or (end_local.hour == 18 and end_local.minute > 0):
                result.valid = False
                result.violations.append(f"{prefix}: happy hour must end by 6:00 PM.")
            if weekday == "Friday":
                result.warnings.append(f"{prefix}: happy hour on Friday is discouraged.")
            week = _week_key(start_local)
            existing_hh = _count_weekly_pattern(busy, week, re.compile(r"happy\s*hour", re.I))
            # These slots are alternative times for ONE happy hour — only one will be
            # booked, so the new meeting counts once, not once per offered option.
            proposed_hh = 1
            if existing_hh + proposed_hh > prefs.happy_hour_max_per_week:
                result.valid = False
                result.violations.append(
                    f"{prefix}: exceeds happy hour cap ({prefs.happy_hour_max_per_week}/week)."
                )
            # Reverse of the family-time rule: a happy hour is the LAST work
            # commitment of the day. Warn (not block — the later event may be
            # personal) when something already sits after this slot's end.
            later_same_evening = []
            for event in busy:
                raw_start = parse_event_datetime(event.get("start"))
                if not raw_start:
                    continue
                evt_start = local_dt(raw_start)
                if (
                    evt_start.date() == start_local.date()
                    and end_local <= evt_start
                    and evt_start.hour < 21
                ):
                    later_same_evening.append(event)
            if later_same_evening:
                result.warnings.append(
                    f"{prefix}: Kory already has a commitment later that evening — "
                    "happy hour is meant to be his last work stop of the day."
                )

        result.rules_checked.append("dinner_rules")
        if intent_key in DINNER_INTENTS:
            week = _week_key(start_local)
            existing_din = _count_weekly_pattern(
                busy, week, re.compile(r"\bdinner\b|dinner request", re.I)
            )
            # Alternative times for ONE dinner — counts once, not per offered option.
            proposed_din = 1
            if existing_din + proposed_din > prefs.dinner_max_per_week:
                result.valid = False
                result.violations.append(
                    f"{prefix}: exceeds dinner cap ({prefs.dinner_max_per_week}/week)."
                )

        result.rules_checked.append("post_happy_hour")
        hh_start = _happy_hour_on_day(start_local, busy)
        if hh_start and intent_key not in DINNER_INTENTS:
            if start_local >= hh_start:
                result.valid = False
                result.violations.append(
                    f"{prefix}: nothing scheduled after happy hour (family time)."
                )

        result.rules_checked.append("coffee_buffer")
        if intent_key == "coffee":
            meeting_end = start_local + timedelta(minutes=_coffee_meeting_duration_minutes())
            buffer_end = meeting_end + timedelta(minutes=_coffee_post_buffer_minutes())
            for event in busy:
                event_start = parse_event_datetime(event.get("start"))
                event_end = parse_event_datetime(event.get("end"))
                if not event_start or not event_end:
                    continue
                if intervals_overlap(meeting_end, buffer_end, event_start, event_end):
                    result.valid = False
                    result.violations.append(
                        f"{prefix}: coffee requires 30 min after — calendar conflict in buffer."
                    )

        result.rules_checked.append("post_coffee_buffer")
        if _coffee_buffer_conflict(start_local, busy):
            result.valid = False
            result.violations.append(
                f"{prefix}: cannot schedule immediately after a coffee meeting (30 min buffer)."
            )

        result.rules_checked.append("drive_time")
        if _drive_time_conflict(
            start_local, end_local, busy, fmt=fmt, intent_key=intent_key
        ):
            result.valid = False
            result.violations.append(
                f"{prefix}: needs { _default_drive_minutes() } min drive time between in-person meetings."
            )

        result.rules_checked.append("virtual_back_to_back")
        if fmt == "virtual" and intent_key in VIRTUAL_INFORMAL_INTENTS | {"virtual_30", "new_client"}:
            if _virtual_back_to_back_conflict(start_local, busy):
                result.valid = False
                result.violations.append(
                    f"{prefix}: needs 30 min break after 2 hours of back-to-back meetings."
                )

    return result


def slot_conflicts_busy(slot: dict[str, str], busy_events: list[dict[str, Any]]) -> bool:
    from app.scheduling.busy_intervals import slot_conflicts_busy as _conflicts

    return _conflicts(slot, busy_events)


def filter_slots_by_rules(
    slots: list[dict[str, str]],
    *,
    intent: str | None = None,
    meeting_format: str | None = None,
    urgent: bool = False,
    busy_events: list[dict[str, Any]] | None = None,
    preferences: SchedulingPreferences | None = None,
) -> tuple[list[dict[str, str]], ValidationResult]:
    """Return slots that pass hard validators; attach aggregate result."""
    safe: list[dict[str, str]] = []
    aggregate = ValidationResult(valid=True)
    prefs = preferences or load_scheduling_preferences()

    for slot in slots:
        check = validate_proposal_slots(
            [slot],
            intent=intent,
            meeting_format=meeting_format,
            urgent=urgent,
            busy_events=busy_events,
            preferences=prefs,
            batch_slots=slots,
        )
        aggregate.rules_checked.extend(check.rules_checked)
        aggregate.warnings.extend(check.warnings)
        if check.valid:
            safe.append(slot)
        else:
            aggregate.violations.extend(check.violations)

    aggregate.valid = len(safe) >= 2
    if len(safe) < 2:
        aggregate.violations.append(
            f"Only {len(safe)} slot(s) passed Kory rule validation; need at least 2."
        )
    return safe, aggregate
