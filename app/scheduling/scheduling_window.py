"""Infer requested scheduling window from email subject/body (e.g. 'next week')."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

MT = ZoneInfo(settings.scheduling_timezone)


@dataclass(frozen=True)
class SchedulingWindow:
    start: date  # inclusive (local MT)
    end: date  # inclusive (local MT)
    source: str
    label: str


def _week_bounds(anchor: date) -> tuple[date, date]:
    """Monday–Sunday week containing anchor."""
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


_WEEKDAY_INDEX = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
}

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Longest-first so "sept" wins over "sep" and "march" over "mar".
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_RE = "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th)?"


def _resolve_month_day(month: int, day: int, today: date) -> date | None:
    """A named month/day → the sensible year. Rolls forward when clearly past."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None  # e.g. February 30 — not a date, don't guess
        # Tolerate a fortnight in the past: senders write "August 1" on August 3
        # meaning this year, not next.
        if candidate >= today - timedelta(days=14):
            return candidate
    return None


def _resolve_day_of_month(day: int, today: date) -> date | None:
    """A bare ordinal ('the 12th') → that day this month, else the next month."""
    for offset in (0, 1, 2):
        month = today.month + offset
        year = today.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _range_label(start: date, end: date) -> str:
    if start == end:
        return f"{start.strftime('%A, %B')} {start.day}"
    if start.month == end.month:
        return f"{start.strftime('%B')} {start.day}–{end.day}"
    return f"{start.strftime('%B')} {start.day} – {end.strftime('%B')} {end.day}"


def _infer_calendar_date_window(combined: str, today: date) -> SchedulingWindow | None:
    """Windows stated as calendar dates rather than relative phrases.

    Everything here used to fall through to None, which the slot engine reads as
    "no constraint" and answers from a 60–120 day horizon. A sender writing
    "the week of the 10th" got offers three weeks out with no error raised.
    Ordered most-specific first: a range like "August 10-14" must not be read as
    the single date "August 10".
    """
    # "August 10 to August 14", "Aug 10-14", "August 10 through the 14th"
    match = re.search(
        rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}}){_ORDINAL}\s*"
        rf"(?:-|–|—|to|through|thru|until|til)\s*"
        rf"(?:({_MONTH_RE})\.?\s+)?(?:the\s+)?(\d{{1,2}}){_ORDINAL}\b",
        combined,
    )
    if match:
        start_month, start_day, end_month, end_day = match.groups()
        start = _resolve_month_day(_MONTHS[start_month], int(start_day), today)
        end = _resolve_month_day(
            _MONTHS[end_month] if end_month else _MONTHS[start_month], int(end_day), today
        )
        if start and end and end >= start:
            return SchedulingWindow(
                start=start, end=end, source="body", label=_range_label(start, end)
            )

    # "week of the 10th", "week of August 10", "week of Monday the 10th"
    match = re.search(
        rf"\bweek\s+of\s+(?:the\s+)?(?:(?:{_WEEKDAY_RE})\s+)?(?:the\s+)?"
        rf"(?:({_MONTH_RE})\.?\s+)?(\d{{1,2}}){_ORDINAL}\b",
        combined,
    )
    if match:
        month_name, day = match.groups()
        anchor = (
            _resolve_month_day(_MONTHS[month_name], int(day), today)
            if month_name
            else _resolve_day_of_month(int(day), today)
        )
        if anchor:
            start, end = _week_bounds(anchor)
            return SchedulingWindow(
                start=start,
                end=end,
                source="body",
                label=f"week of {start.strftime('%B')} {start.day}",
            )

    # A single named date: "August 12", "on Aug 12th"
    match = re.search(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}}){_ORDINAL}\b", combined)
    if match:
        day_date = _resolve_month_day(_MONTHS[match.group(1)], int(match.group(2)), today)
        if day_date:
            return SchedulingWindow(
                start=day_date,
                end=day_date,
                source="body",
                label=_range_label(day_date, day_date),
            )

    # A bare ordinal: "how about the 12th?"
    match = re.search(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", combined)
    if match:
        day_date = _resolve_day_of_month(int(match.group(1)), today)
        if day_date:
            return SchedulingWindow(
                start=day_date,
                end=day_date,
                source="body",
                label=_range_label(day_date, day_date),
            )

    # "next Tuesday" / "this Thursday" / "does Tuesday work?"
    match = re.search(rf"\b(next|this)?\s*({_WEEKDAY_RE})\b", combined)
    if match:
        qualifier, name = match.groups()
        index = _WEEKDAY_INDEX[name]
        if (qualifier or "").strip() == "next":
            # Match the "next week" branch above: the weekday of the following week.
            this_monday, _ = _week_bounds(today)
            target = this_monday + timedelta(days=7 + index)
        else:
            delta = (index - today.weekday()) % 7
            target = today + timedelta(days=delta or 7)  # "Tuesday" on a Tuesday means next one
        return SchedulingWindow(
            start=target, end=target, source="body", label=_range_label(target, target)
        )

    return None


def infer_scheduling_window(
    *,
    subject: str = "",
    body: str = "",
    now: datetime | None = None,
) -> SchedulingWindow | None:
    """Return a date window when the sender names one; else None (use full horizon)."""
    combined = f"{subject}\n{body}".lower()
    today = (now or datetime.now(tz=MT)).astimezone(MT).date()

    if re.search(r"\bbefore\s+i\s+(?:take\s+off|head\s+to|leave)\b", combined):
        start = today + timedelta(days=1) if today.weekday() < 5 else today
        this_monday, this_sunday = _week_bounds(today)
        end = this_sunday
        if re.search(r"\b(?:on\s+)?saturday\b", combined):
            days_to_sat = (5 - today.weekday()) % 7
            if days_to_sat == 0:
                days_to_sat = 7
            travel_sat = today + timedelta(days=days_to_sat)
            end = min(end, travel_sat - timedelta(days=1))
        return SchedulingWindow(
            start=start,
            end=end,
            source="body",
            label="before travel",
        )

    # "this week or next" must come before the bare "this week" branch, or the
    # compound phrase collapses to this week alone (live C-2: "this week or
    # next" became Aug 5-9 and the ladder walked clean out of both weeks).
    if re.search(r"\bthis\s+week\s+or\s+(?:the\s+)?next(?:\s+week)?\b", combined):
        start, end = _week_bounds(today)
        if today > start:
            start = today + timedelta(days=1)  # skip today for scheduling
        return SchedulingWindow(
            start=start,
            end=end + timedelta(days=7),
            source="body",
            label="this week or next",
        )

    if re.search(r"\bthis\s+week\b", combined):
        start, end = _week_bounds(today)
        if today > start:
            start = today + timedelta(days=1)  # skip today for scheduling
        return SchedulingWindow(start=start, end=end, source="body", label="this week")

    if re.search(r"\bnext\s+week\b", combined) and not re.search(
        r"\bnext\s+(?:week\s+or\s+(?:two|so)|couple\s+(?:of\s+)?weeks?)\b", combined
    ):
        this_monday, this_sunday = _week_bounds(today)
        next_monday = this_monday + timedelta(days=7)
        next_sunday = this_sunday + timedelta(days=7)
        return SchedulingWindow(
            start=next_monday,
            end=next_sunday,
            source="body",
            label="next week",
        )

    if re.search(
        r"\bnext\s+(?:week\s+or\s+(?:two|so)|couple\s+(?:of\s+)?weeks?)\b", combined
    ):
        this_monday, this_sunday = _week_bounds(today)
        next_monday = this_monday + timedelta(days=7)
        end_sunday = this_sunday + timedelta(days=14)
        return SchedulingWindow(
            start=next_monday,
            end=end_sunday,
            source="body",
            label="next week or two",
        )

    if re.search(r"\b(?:in\s+)?two\s+weeks?\b", combined) or re.search(
        r"\bweek\s+after\s+next\b", combined
    ):
        this_monday, _ = _week_bounds(today)
        start_monday = this_monday + timedelta(days=14)
        end_sunday = start_monday + timedelta(days=6)
        return SchedulingWindow(
            start=start_monday,
            end=end_sunday,
            source="body",
            label="two weeks out",
        )

    if re.search(r"\btomorrow\b", combined):
        d = today + timedelta(days=1)
        return SchedulingWindow(start=d, end=d, source="body", label="tomorrow")

    if re.search(r"\btoday\b", combined) and "not today" not in combined:
        return SchedulingWindow(start=today, end=today, source="body", label="today")

    # Relative phrasing is handled above; fall through to explicit calendar dates
    # ("the week of the 10th", "August 10-14", "next Tuesday") before giving up.
    return _infer_calendar_date_window(combined, today)


@dataclass(frozen=True)
class TimeOfDayWindow:
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    label: str = ""

    def earliest_minutes(self) -> int:
        return self.start_hour * 60 + self.start_minute

    def latest_start_minutes(self, block_minutes: int) -> int:
        return self.end_hour * 60 + self.end_minute - block_minutes




def _parse_clock_token(hour: int, minute: int, ampm: str | None) -> tuple[int, int]:
    h = hour
    token = (ampm or "").lower()
    if token == "pm" and h != 12:
        h += 12
    elif token == "am" and h == 12:
        h = 0
    elif not token and 1 <= h <= 7:
        h += 12
    return h, minute


def _lower_start_for_explicit_am(window: TimeOfDayWindow, combined: str) -> TimeOfDayWindow:
    """Widen a morning window downward when the sender names an earlier time.

    "Early morning — even 7 AM works" must make 7:00 offerable. Only ever
    lowers the start (an explicit 10 AM mention never shrinks the window), and
    floors at 7:00 — Kory's earliest for outside meetings (ruling V-1).
    """
    from dataclasses import replace

    earliest = window.earliest_minutes()
    for match in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*a\.?m\.?\b", combined):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not 1 <= hour <= 11:
            continue
        total = max(hour * 60 + minute, 7 * 60)
        if total < earliest:
            earliest = total
    if earliest == window.earliest_minutes():
        return window
    return replace(window, start_hour=earliest // 60, start_minute=earliest % 60)


def infer_time_of_day_window(
    *,
    subject: str = "",
    body: str = "",
) -> TimeOfDayWindow | None:
    """Parse 'between 9 AM and 4:30 PM' or soft preferences like 'mornings work best'."""
    combined = f"{subject}\n{body}".lower()
    match = re.search(
        r"\bbetween\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+and\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        combined,
    )
    if match:
        sh, sm, sampm, eh, em, eampm = match.groups()
        start_h, start_m = _parse_clock_token(int(sh), int(sm or 0), sampm)
        end_h, end_m = _parse_clock_token(int(eh), int(em or 0), eampm or sampm)
        if end_h * 60 + end_m > start_h * 60 + start_m:
            return TimeOfDayWindow(
                start_hour=start_h,
                start_minute=start_m,
                end_hour=end_h,
                end_minute=end_m,
                label=match.group(0).strip(),
            )

    # "early morning" contains the word "morning", so this branch must run
    # before the generic mornings branch — the old order returned an 8:00 floor
    # for "early morning, even 7 AM works" and a genuinely free 7:00 slot was
    # rejected as "outside time-of-day window" (live C-2).
    if re.search(r"\b(early\s+morning|early\s+am)\b", combined):
        return _lower_start_for_explicit_am(
            TimeOfDayWindow(
                start_hour=7,
                start_minute=0,
                end_hour=11,
                end_minute=0,
                label="early morning",
            ),
            combined,
        )

    if re.search(r"\b(mornings?|morning\s+works?)\b", combined) and not re.search(
        r"\b(afternoon|evening|after\s+\d|pm)\b", combined
    ):
        return _lower_start_for_explicit_am(
            TimeOfDayWindow(
                start_hour=8,
                start_minute=0,
                end_hour=12,
                end_minute=0,
                label="mornings",
            ),
            combined,
        )

    if re.search(r"\bafternoons?\b", combined) and not re.search(r"\b(morning|evening)\b", combined):
        return TimeOfDayWindow(
            start_hour=12,
            start_minute=0,
            end_hour=17,
            end_minute=0,
            label="afternoons",
        )

    if re.search(r"\b(at\s+)?6:?\s*30\s*pm\b|\b6:30\s*pm\b", combined) and not re.search(
        r"\bother\s+options\b", combined
    ):
        return TimeOfDayWindow(
            start_hour=18,
            start_minute=30,
            end_hour=19,
            end_minute=0,
            label="6:30 PM",
        )

    return None


def infer_allowed_weekdays(
    *,
    subject: str = "",
    body: str = "",
) -> set[int] | None:
    """Parse 'Monday through Wednesday' (or Tue/Wed only) weekday constraints."""
    combined = f"{subject}\n{body}".lower()
    range_match = re.search(
        r"\b("
        r"monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|friday|fri"
        r")\s+through\s+("
        r"monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|friday|fri"
        r")\b",
        combined,
    )
    if range_match:
        start = _WEEKDAY_INDEX[range_match.group(1)]
        end = _WEEKDAY_INDEX[range_match.group(2)]
        if start <= end:
            return set(range(start, end + 1))
        return set(range(start, 7)) | set(range(0, end + 1))

    days: set[int] = set()
    for token in re.findall(
        r"\b(monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|friday|fri)\b",
        combined,
    ):
        if token in {
            "monday",
            "mon",
            "tuesday",
            "tue",
            "tues",
            "wednesday",
            "wed",
            "thursday",
            "thu",
            "thur",
            "thurs",
            "friday",
            "fri",
        }:
            days.add(_WEEKDAY_INDEX[token])
    if len(days) >= 2 and re.search(r"\b(?:or|and)\b", combined):
        return days
    return None


def slot_start_in_time_window(
    start_local: datetime,
    window: TimeOfDayWindow,
    *,
    block_minutes: int,
) -> bool:
    start_minutes = start_local.hour * 60 + start_local.minute
    return (
        window.earliest_minutes()
        <= start_minutes
        <= window.latest_start_minutes(block_minutes)
    )


def slot_date_in_window(slot: dict[str, str], window: SchedulingWindow) -> bool:
    try:
        start = datetime.fromisoformat(str(slot.get("start", "")).replace("Z", "+00:00"))
        local = start.astimezone(MT).date()
    except (TypeError, ValueError):
        return False
    return window.start <= local <= window.end
