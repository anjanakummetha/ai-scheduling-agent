"""Classify Outlook events, dedupe Master copies, and route calendar writes.

Reads Kory's work Calendar + Master rollup with intelligence so kid-only /
informational Master items do not block business scheduling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from app.config import settings

import rules as kory_rules

_COPY_SUFFIX_RE = re.compile(r"\s*\(copy\)\s*$", re.IGNORECASE)
_DO_NOT_MOVE_RE = re.compile(r"do\s*not\s*move", re.IGNORECASE)

# Kory-owned personal blocks (always block).
_KORY_PERSONAL_BLOCK_PATTERNS = (
    r"\bkm\s+personal\s+training\b",
    r"\bkory\b.*\b(personal\s+)?training\b",
    r"\bkory\s+drop\s*off\b",
    r"\bkory\s+pick\s*up\b",
    r"\bdrop\s*off\b.*\bkory\b",
    r"\bpick\s*up\b.*\bkory\b",
    r"\bhrt\b",
    r"\bdr\.?\s*bruice\b",
    r"\bdo\s*not\s*move\b",
    r"\bbridget\b.*\b(do\s*not|non-negotiable)\b",
)

# School / family logistics where Kory is involved (not kid-only camp).
_KORY_LOGISTICS_PATTERNS = (
    r"\bdrop\s*off\b",
    r"\bpick\s*up\b",
    r"\bpickup\b",
    r"\bk\+b\b",
)

# Hard blocks that may exist only on Master (no work Calendar twin).
# Cover both spellings of the board name — the spec says "NextSite", the live
# calendar says "NexSite"; protect on either.
_MASTER_HARD_BLOCK_PATTERNS = (
    r"^doug\b",
    r"\bnex(t)?site\b",
    r"\bcanopy service\b",
    r"\bcapital\s+demo(lition)?\b",
    r"\bcapdemo\b",
    r"\bypo\b",
)

# Personal social / errands on Master that mean Kory is busy.
_PERSONAL_KORY_LEISURE_PATTERNS = (
    r"\beidson",
    r"\bmassage\b",
    r"\breservation at\b",
    r"\bbeckon\b",
    r"\bhorse clinic\b",
    r"\btouchup\b",
    r"\bnap\s*\+",
    r"😴",
    r"\bpickup wine\b",
    r"\bpickup sams\b",
)

# Kory labels Master events he attends (future-friendly).
_KORY_ATTENDANCE_LABEL_PATTERNS = (
    r"^\[kory\]",
    r"^kory\s*[\-–—:]",
    r"\bkory attends\b",
)

# Travel / out-of-office — block business scheduling across the span.
_TRAVEL_BLOCK_PATTERNS = (
    r"^stay at\b",
    r"\bflight to\b",
    r"\bflight from\b",
    r"\bua\s+\d+",
    r"\bin chicago\b",
    r"\bin travel\b",
    r"\bfamily safari\b",
    r"\bkory in [a-z]+\b",
    r"\bout of office\b",
    r"\booo\b",
    r"\bcheck-?in to check-?out\b",
    r"\bprivate\s+.*\btour\b",
    r"\bpeninsula tour\b",
    r"\bcape town\b",
    r"\bmount nelson\b",
    r"🏔|🍽|🏨|✈|🐧",
    r"\bdinner\s*[—\-]\s*",
    r"\bbreakfast\s*[—\-]\s*",
    r"\breturn home\b",
    r"\bpickup at\b",
    r"\bsafari\b",
    r"\broyal livingstone\b",
    r"\bheathrow express\b",
    r"\bopening social\b",
    r"\bcultural exchange\b",
    r"\bhelicopter tour\b",
    r"🦁|🚁|🥂|🍳|🌍|⚠️|😴",
)

# Kory-named meetings on Master — usually work copies, not personal blocks.
_KORY_NAMED_MEETING_PATTERNS = (
    r"\bkory\s+mitchell\s+and\b",
    r"\bkory\s+mitchell\s*\|",
    r"\bkory\s*\([^)]*(ifg|podcast)",
    r"\bkory\b.*\b<>\b",
    r"\b<>\s*kory\b",
    r"\| kory mitchell\b",
    r"\bwith kory mitchell\b",
    r"\bkory\s*\(ifg\)",
    r"\bintro\b.*\bkory\b",
    r"\bkory\b.*\bintro\b",
    r"\bpodcast\b",
)

# KM prefix / initials — positive personal signal (not sufficient alone).
_KM_PERSONAL_HINT_PATTERNS = (
    r"^km[\s:\-]",
    r"\bkm\s+(personal|training|drop|pick|gym|workout|trainer|dentist|doctor)\b",
)

# Kid / family activity on Master that does NOT mean Kory is busy.
_KID_ONLY_NON_BLOCKING_PATTERNS = (
    r"\bmaclain\b.*\b(camp|lesson|riding)\b",
    r"\bmaclain\s*@\s*isd\b",
    r"\bgracie\b",
    r"\bbecky\b.*\bkids\b",
    r"\bsummer camp\b",
    r"\bmystery camp\b",
    r"\bisd camp\b",
    r"\bisd summer\b",
    r"\bmyths\s*&\s*magic\b",
    r"\bnatalia\b.*\bwedding\b",
    r"\bbridget\b.*\b(?!kory|drop|pick)",
)

# Work signals — block even on Master when not a duplicate copy of work cal.
_WORK_SIGNAL_PATTERNS = (
    r"\bhold\s*:",
    r"\bintro\b",
    r"\bifg\b",
    r"\bdiligence\b",
    r"\bpipeline\b",
    r"\bboard\b",
    r"\bypo\b",
    r"\bcapital demo\b",
    r"\bcapdemo\b",
    r"\bteams\b",
    r"\bzoom\b",
    r"\b@\s*kory",
    r"\bkory\s*\|",
    r"\bkory\s+mitchell\b",
    r"\bwith kory\b",
    r"\b<>\b",
    r"\bjosh hood\b",
    r"\bheidi\b",
    r"\bdoug\b",
    r"\bpatrick\b",
    r"\bwob\b",
    r"\binbox review\b",
    r"\bweekly\s+stand[\s-]?up\b",
    r"\bstand[\s-]?up\b",
    r"\bshift\b",
    r"\bsierra\b",
    r"\bproject\s+sierra\b",
)

_BIRTHDAY_CALENDAR_NAMES = frozenset({"birthdays"})


class EventBlockingClass(str, Enum):
    WORK_BLOCKING = "work_blocking"
    PERSONAL_KORY_BLOCKING = "personal_kory_blocking"
    TRAVEL_BLOCKING = "travel_blocking"
    FAMILY_DO_NOT_MOVE = "family_do_not_move"
    DUPLICATE_COPY = "duplicate_copy"
    KID_ONLY_NON_BLOCKING = "kid_only_non_blocking"
    INFORMATIONAL = "informational"
    UNKNOWN_BLOCKING = "unknown_blocking"


@dataclass(frozen=True)
class ClassifiedEvent:
  raw: dict[str, Any]
  blocking_class: EventBlockingClass
  blocks_kory: bool
  calendar_name: str
  normalized_subject: str
  dedupe_key: str


def _normalize_subject(subject: str) -> str:
    text = _COPY_SUFFIX_RE.sub("", (subject or "").strip().lower())
    return re.sub(r"\s+", " ", text)


def _calendar_name(event: dict[str, Any]) -> str:
    return str(event.get("calendar_name") or event.get("source_calendar") or "").strip()


def _is_work_calendar(name: str) -> bool:
    norm = name.lower().replace("'", "'")
    return norm in {"calendar", "kory master calendar (all)"} and norm == "calendar"


def _is_master_calendar(name: str) -> bool:
    return "master calendar" in name.lower()


def _subject_matches(patterns: tuple[str, ...], subject: str) -> bool:
    return any(re.search(p, subject, re.IGNORECASE) for p in patterns)


def _km_personal_hint(subject: str, norm_subject: str) -> bool:
    """KM / initials lean personal — overridden by work, kid, or travel signals."""
    raw = (subject or "").strip()
    if _subject_matches(_KM_PERSONAL_HINT_PATTERNS, raw) or _subject_matches(
        _KM_PERSONAL_HINT_PATTERNS, norm_subject
    ):
        return True
    return bool(re.match(r"^km\b", raw, re.IGNORECASE))


def _is_kory_logistics(norm_subject: str) -> bool:
    if _subject_matches(_KORY_LOGISTICS_PATTERNS, norm_subject):
        if re.search(r"\bpickup at\b", norm_subject, re.IGNORECASE):
            return False
        return True
    return False


def _is_kid_only_subject(norm_subject: str) -> bool:
    if _is_kory_logistics(norm_subject):
        return False
    return _subject_matches(_KID_ONLY_NON_BLOCKING_PATTERNS, norm_subject)


def _kory_explicit_attendance_label(subject: str, norm_subject: str) -> bool:
    """Kory prefixes Master events he attends: 'Kory - …' or '[Kory] …'."""
    raw = (subject or "").strip()
    if not (
        re.match(r"^kory\s*[\-–—:]", raw, re.IGNORECASE)
        or re.match(r"^\[kory\]", raw, re.IGNORECASE)
        or _subject_matches(_KORY_ATTENDANCE_LABEL_PATTERNS, raw)
    ):
        return False
    if _subject_matches(_KORY_NAMED_MEETING_PATTERNS, norm_subject):
        return False
    if _subject_matches(_WORK_SIGNAL_PATTERNS, norm_subject):
        return False
    return True


def _classified(
    event: dict[str, Any],
    *,
    blocking_class: EventBlockingClass,
    blocks_kory: bool,
    calendar_name: str,
    norm_subject: str,
) -> ClassifiedEvent:
    return ClassifiedEvent(
        raw=event,
        blocking_class=blocking_class,
        blocks_kory=blocks_kory,
        calendar_name=calendar_name,
        normalized_subject=norm_subject,
        dedupe_key=_dedupe_key(event, norm_subject),
    )


def classify_event(event: dict[str, Any]) -> ClassifiedEvent:
    """Classify one Outlook event for Kory busy/free."""
    subject = str(event.get("subject") or "")
    norm_subject = _normalize_subject(subject)
    cal_name = _calendar_name(event)
    cal_lower = cal_name.lower()

    if cal_lower in _BIRTHDAY_CALENDAR_NAMES or subject.lower().startswith("birthday"):
        return _classified(
            event,
            blocking_class=EventBlockingClass.INFORMATIONAL,
            blocks_kory=False,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if re.search(r"\bbirthday\b", norm_subject, re.IGNORECASE):
        if re.search(r"birthday\s*\(\d{4}\)", norm_subject) or re.search(
            r"^[\w\s]+\s*-\s*birthday\b", norm_subject, re.IGNORECASE
        ):
            return _classified(
                event,
                blocking_class=EventBlockingClass.INFORMATIONAL,
                blocks_kory=False,
                calendar_name=cal_name,
                norm_subject=norm_subject,
            )

    if _DO_NOT_MOVE_RE.search(subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.FAMILY_DO_NOT_MOVE,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _subject_matches(_TRAVEL_BLOCK_PATTERNS, norm_subject) or _subject_matches(
        _TRAVEL_BLOCK_PATTERNS, subject
    ):
        return _classified(
            event,
            blocking_class=EventBlockingClass.TRAVEL_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _subject_matches(_KORY_PERSONAL_BLOCK_PATTERNS, norm_subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.PERSONAL_KORY_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_kory_logistics(norm_subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.PERSONAL_KORY_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _subject_matches(_MASTER_HARD_BLOCK_PATTERNS, norm_subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.WORK_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_master_calendar(cal_name) and _kory_explicit_attendance_label(subject, norm_subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.PERSONAL_KORY_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_master_calendar(cal_name) and _is_kid_only_subject(norm_subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.KID_ONLY_NON_BLOCKING,
            blocks_kory=False,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_work_calendar(cal_name) or _subject_matches(_WORK_SIGNAL_PATTERNS, norm_subject):
        return _classified(
            event,
            blocking_class=EventBlockingClass.WORK_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_master_calendar(cal_name) and _subject_matches(
        _KORY_NAMED_MEETING_PATTERNS, norm_subject
    ):
        return _classified(
            event,
            blocking_class=EventBlockingClass.WORK_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_master_calendar(cal_name) and _subject_matches(
        _PERSONAL_KORY_LEISURE_PATTERNS, norm_subject
    ):
        return _classified(
            event,
            blocking_class=EventBlockingClass.PERSONAL_KORY_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    if _is_master_calendar(cal_name) and _km_personal_hint(subject, norm_subject):
        if not _subject_matches(_WORK_SIGNAL_PATTERNS, norm_subject):
            return _classified(
                event,
                blocking_class=EventBlockingClass.PERSONAL_KORY_BLOCKING,
                blocks_kory=True,
                calendar_name=cal_name,
                norm_subject=norm_subject,
            )

    if _is_master_calendar(cal_name):
        return _classified(
            event,
            blocking_class=EventBlockingClass.UNKNOWN_BLOCKING,
            blocks_kory=True,
            calendar_name=cal_name,
            norm_subject=norm_subject,
        )

    return _classified(
        event,
        blocking_class=EventBlockingClass.UNKNOWN_BLOCKING,
        blocks_kory=True,
        calendar_name=cal_name,
        norm_subject=norm_subject,
    )


def _dedupe_key(event: dict[str, Any], norm_subject: str) -> str:
    start = _event_start_iso(event)
    end = _event_end_iso(event)
    return f"{start}|{end}|{norm_subject}"


def _event_start_iso(event: dict[str, Any]) -> str:
    start = event.get("start")
    if isinstance(start, dict):
        return str(start.get("dateTime") or start.get("date") or "")
    return str(start or "")


def _event_end_iso(event: dict[str, Any]) -> str:
    end = event.get("end")
    if isinstance(end, dict):
        return str(end.get("dateTime") or end.get("date") or "")
    return str(end or "")


def dedupe_and_filter_blocking_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ClassifiedEvent]]:
    """Dedupe cross-calendar copies; return Kory-blocking events with metadata."""
    classified = [classify_event(e) for e in events]
    seen_work_keys: set[str] = set()
    blocking: list[dict[str, Any]] = []
    audit: list[ClassifiedEvent] = []

    # Prefer work Calendar entries over Master (copy) for the same slot.
    work_first = sorted(
        classified,
        key=lambda c: (0 if _is_work_calendar(c.calendar_name) else 1, c.dedupe_key),
    )

    for item in work_first:
        if not item.blocks_kory:
            audit.append(item)
            continue
        if item.dedupe_key in seen_work_keys:
            audit.append(
                ClassifiedEvent(
                    raw=item.raw,
                    blocking_class=EventBlockingClass.DUPLICATE_COPY,
                    blocks_kory=False,
                    calendar_name=item.calendar_name,
                    normalized_subject=item.normalized_subject,
                    dedupe_key=item.dedupe_key,
                )
            )
            continue
        seen_work_keys.add(item.dedupe_key)
        enriched = dict(item.raw)
        enriched["blocking_class"] = item.blocking_class.value
        enriched["blocks_kory"] = True
        blocking.append(enriched)
        audit.append(item)

    return blocking, audit


# propose_meeting_slots retries the requested window at +1w, +2w and +3w before
# giving up, so the loaded calendar must reach at least three weeks past it.
_LADDER_HEADROOM_DAYS = 24


def resolve_calendar_horizon_days(
    *,
    subject: str = "",
    body: str = "",
    explicit_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """How many days ahead to load — default from settings, extend for far-future asks."""
    from app.config import settings
    from app.scheduling.scheduling_window import infer_scheduling_window
    from zoneinfo import ZoneInfo

    base = explicit_days or settings.lexi_calendar_search_days
    base = max(7, min(base, settings.lexi_calendar_search_days_max))
    combined = f"{subject}\n{body}".lower()

    mt = ZoneInfo(settings.scheduling_timezone)
    today = (now or datetime.now(tz=mt)).astimezone(mt).date()
    window = infer_scheduling_window(subject=subject, body=body, now=now)
    if window:
        days_needed = (window.end - today).days + 2
        # Headroom for the fallback ladder. When the requested window is too full,
        # propose_meeting_slots retries +1w/+2w/+3w — but it can only offer times
        # the loaded context covers, so trimming the horizon to the window end
        # left those retries searching dates with no calendar data and finding
        # nothing. Coffee hit this every time: ~1 slot a week means the ladder is
        # the normal path, not the exception.
        days_needed = max(7, days_needed + _LADDER_HEADROOM_DAYS)
        base = min(base, days_needed)

    # Month names / relative far-future cues
    month_hints = (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        "next month", "following month", "in a month", "few weeks",
        "couple of weeks", "later this summer", "this fall", "this winter",
    )
    if any(h in combined for h in month_hints):
        return settings.lexi_calendar_search_days_max

    if re.search(r"\b(in|during)\s+\d+\s+weeks?\b", combined):
        return settings.lexi_calendar_search_days_max

    return base


def resolve_write_calendar_name(
    *,
    intent: str | None = None,
    explicit: str | None = None,
) -> str:
    """Route holds/invites: work → Calendar, personal → Master."""
    from app.integrations.named_calendars import resolve_write_calendar_for_intent

    if explicit and explicit.strip():
        return explicit.strip()
    return resolve_write_calendar_for_intent(intent)


def summarize_blocking_events(events: list[dict[str, Any]], *, limit: int = 80) -> dict[str, Any]:
    """Summary for Hermes / debugging."""
    counts: dict[str, int] = {}
    for event in events:
        key = str(event.get("blocking_class") or "unclassified")
        counts[key] = counts.get(key, 0) + 1
    return {
        "blocking_count": len(events),
        "by_class": counts,
        "events": events[:limit],
    }


def parse_duration_from_text(text: str) -> int | None:
    """Read explicit duration cues from email subject/body (10–240 minutes)."""
    if not (text or "").strip():
        return None
    lowered = text.lower()
    if re.search(r"\bhalf[- ]?(?:an? )?hour\b", lowered):
        return 30
    if re.search(r"\bquarter[- ]?(?:of an? )?hour\b", lowered):
        return 15
    if re.search(r"\b(?:one|1)\s*[- ]?hours?\b", lowered):
        return 60
    hour_match = re.search(r"\b(\d{1,2})\s*[- ]?(?:hours?|hrs?)\b", lowered)
    if hour_match:
        hours = int(hour_match.group(1))
        if 1 <= hours <= 4:
            return hours * 60
    minute_match = re.search(r"\b(\d{1,3})\s*[- ]?\s*(?:min(?:ute)?s?|m)\b", text, re.I)
    if minute_match:
        value = int(minute_match.group(1))
        if 10 <= value <= 240:
            return value
    return None


def infer_duration_from_email(
    *,
    subject: str = "",
    body: str = "",
    intent: str | None = None,
    plan_duration_minutes: int | None = None,
) -> int:
    """Email text wins, then scheduling plan, then meeting-type defaults."""
    from app.scheduling.meeting_type import calendar_block_minutes_for_context

    explicit = parse_duration_from_text(f"{subject}\n{body}")
    if explicit:
        from app.scheduling.meeting_type import resolve_meeting_type

        spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
        if spec.type_key == "coffee":
            return spec.calendar_block_minutes
        return explicit
    if plan_duration_minutes and plan_duration_minutes > 0:
        from app.scheduling.meeting_type import resolve_meeting_type

        spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
        if spec.type_key == "coffee":
            return spec.calendar_block_minutes
        return plan_duration_minutes
    from app.scheduling.meeting_type import calendar_block_minutes_for_context

    return calendar_block_minutes_for_context(
        intent=intent, subject=subject, body=body
    )


def infer_meeting_duration_minutes(intent: str | None) -> int:
    """Default meeting duration (not calendar block) from meeting type."""
    from app.scheduling.meeting_type import resolve_meeting_type

    return resolve_meeting_type(intent=intent).duration_minutes


def calendar_block_minutes_for_intent(
    intent: str | None,
    *,
    subject: str = "",
    body: str = "",
) -> int:
    """Calendar block size including buffers (e.g. coffee = 90)."""
    from app.scheduling.meeting_type import calendar_block_minutes_for_context

    return calendar_block_minutes_for_context(
        intent=intent, subject=subject, body=body
    )


def slot_duration_minutes(slot: dict[str, str]) -> int | None:
    from app.scheduling.busy_intervals import slot_interval

    interval = slot_interval(slot)
    if not interval:
        return None
    start, end = interval
    minutes = int((end - start).total_seconds() // 60)
    return minutes if minutes > 0 else None
