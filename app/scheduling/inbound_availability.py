"""Parse and validate times proposed by the email sender (inbound availability)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.rules.validators import validate_proposal_slots
from app.scheduling.busy_intervals import slot_conflicts_busy
from app.scheduling.meeting_type import resolve_meeting_type
from app.scheduling.scheduling_plan import build_scheduling_plan

MT = ZoneInfo(settings.scheduling_timezone)

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Time-of-day phrases → a default hour (MT), used when a date is given without a
# clock time (e.g. "coffee late afternoon on Wednesday"). Order: most specific first.
_TIME_OF_DAY = [
    ("late afternoon", 16), ("early afternoon", 13), ("mid afternoon", 15),
    ("afternoon", 14), ("late morning", 11), ("early morning", 8),
    ("mid morning", 10), ("morning", 9), ("early evening", 17), ("evening", 18),
    ("midday", 12), ("mid-day", 12), ("noon", 12), ("lunch", 12),
    ("end of day", 16), ("cob", 16), ("first thing", 8),
]


def _default_hour_from_body(text: str) -> int | None:
    low = (text or "").lower()
    for phrase, hour in _TIME_OF_DAY:
        if phrase in low:
            return hour
    return None


# Markers that begin quoted thread history / reply headers. Everything from the
# first marker onward is prior-message text (with stray dates and client
# timestamps like "On Thu, Jul 24 at 12:27 AM ... wrote:") that must NOT be
# parsed as the sender's newly-proposed availability.
_QUOTE_CUT_MARKERS = (
    re.compile(r"^\s*on\b.{0,200}\bwrote:\s*$", re.I | re.M),
    # Gmail wraps long attributions across two lines ("On Thu, Jul 24 ... Lexi
    # Knightly\n<lexi@...> wrote:") — the one-line pattern missed them and the
    # attribution's own date/time was mined as the sender's proposal (B9).
    re.compile(r"^\s*on\b[^\n]{0,200}\n[^\n]{0,120}\bwrote:\s*$", re.I | re.M),
    re.compile(r"^-{2,}\s*original message\s*-{2,}", re.I | re.M),
    re.compile(r"^\s*from:\s.+$", re.I | re.M),
    re.compile(r"^_{5,}\s*$", re.M),
    re.compile(r"^\s*>", re.M),
    re.compile(r"^\s*sent from my \w+", re.I | re.M),
)


def strip_quoted_reply(body: str) -> str:
    """Return only the new reply text, cutting quoted thread history and reply
    headers so their dates/timestamps don't leak into inbound-time parsing."""
    text = (body or "").replace("\r", "")
    cut = len(text)
    for marker in _QUOTE_CUT_MARKERS:
        hit = marker.search(text)
        if hit:
            cut = min(cut, hit.start())
    return text[:cut].strip()


# "1:00–1:30 PM" / "2:00-2:30pm" — the meridiem is written once, on the range
# end, so the am/pm-required time regex skipped the start and read the END as
# the proposed time (surfaced 2026-08-15: an offer draft's "1:00–1:30 PM MT"
# parsed as 1:30). Rewrite the start with its inherited meridiem so the normal
# take-the-range-start behavior sees it. A range crossing noon ("11:00–1:30 PM")
# flips the start to the other half.
_SHARED_MERIDIEM_RANGE_RE = re.compile(
    r"(?<![/\d.])\b(\d{1,2})(?::(\d{2}))?\s*[–—\-]\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)",
    re.I,
)


# "11:00 AM–11:30 AM ET" — the zone label is written once, after the range END,
# but the parser reads the range START and looks for a label immediately after
# it, so the ET was lost and the time read as MT (surfaced live 2026-08-15: the
# send gate refused a correct ET-first offer draft as a slot mismatch).
# Propagate the trailing label onto the start time.
_RANGE_TZ_LABEL_RE = re.compile(
    r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\s*[–—\-]\s*"
    r"(\d{1,2}:\d{2}\s*(?:am|pm))\s+"
    r"(et|est|edt|eastern|ct|cst|cdt|central|mt|mst|mdt|mountain"
    r"|pt|pst|pdt|pacific)\b",
    re.I,
)


def _normalize_shared_meridiem_ranges(text: str) -> str:
    def _fix(match: re.Match[str]) -> str:
        h1, m1, h2, m2, meridiem = match.groups()
        mer = "pm" if "p" in meridiem.lower() else "am"
        start_mer = mer
        if int(h1) != 12 and int(h1) > int(h2):
            start_mer = "am" if mer == "pm" else "pm"
        return (
            f"{h1}:{m1 or '00'} {start_mer} – {h2}:{m2 or '00'} {mer}"
        )

    text = _SHARED_MERIDIEM_RANGE_RE.sub(_fix, text)
    return _RANGE_TZ_LABEL_RE.sub(r"\1 \3 – \2 \3", text)


def _writing_zone(default_tz: str | None) -> ZoneInfo:
    """Zone an unlabeled time is written in: the sender's stored zone, else MT."""
    if default_tz:
        try:
            return ZoneInfo(str(default_tz))
        except Exception:  # noqa: BLE001 — junk stored tz must not break parsing
            pass
    return MT


# "at 2" / "around 3:30" — a clock time with no meridiem. Only trusted when
# anchored by at/around (bare numbers are usually counts, suite numbers, or
# fractions), and never when a slash/colon/digit follows (dates, scores).
_BARE_HOUR_RE = re.compile(
    r"\b(at|around)\s+(\d{1,2})(:\d{2})?\b(?!\s*(?:am|pm|a\.m|p\.m|[:./\d-]))",
    re.I,
)


def _infer_bare_hour_meridiem(text: str) -> str:
    """Rewrite 'at 2' as 'at 2 pm' (business-hours reading: 1–7 → PM, 8–11 →
    AM, 12 → PM) so the meridiem-requiring time regex sees it. 'Wednesday at 2
    works' used to DROP the 2 and substitute a 9 AM default (audit B9)."""

    def _fix(match: re.Match[str]) -> str:
        anchor, hour_s, minute_s = match.group(1), match.group(2), match.group(3) or ""
        hour = int(hour_s)
        if not 1 <= hour <= 12:
            return match.group(0)
        meridiem = "pm" if (1 <= hour <= 7 or hour == 12) else "am"
        return f"{anchor} {hour_s}{minute_s} {meridiem}"

    return _BARE_HOUR_RE.sub(_fix, text)


def extract_inbound_time_candidates(
    body: str,
    *,
    reference: datetime | None = None,
    default_tz: str | None = None,
    include_flags: bool = False,
) -> list[dict[str, str]]:
    """Heuristic parse of prospect-proposed times from email body.

    default_tz is the sender's stored timezone: an unlabeled "Thursday at 2"
    from a Boston counterpart means 2 PM Eastern, not 2 PM MT — the offer email
    rendered their zone first, so their reply is in it. An explicit zone label
    next to the time still wins. Output slots are always MT ISO strings.
    """
    now = (reference or datetime.now(tz=MT)).astimezone(MT)
    zone = _writing_zone(default_tz)
    # Parse only the sender's new text — quoted history leaks stray dates/times.
    text = _infer_bare_hour_meridiem(
        _normalize_shared_meridiem_ranges(strip_quoted_reply(body))
    )
    prefer_next_week = bool(re.search(r"\bnext\s+week\b", text, re.I))
    tod_hour = _default_hour_from_body(text)
    # A date named with no clock time (e.g. "August 25th") only becomes a candidate
    # when the surrounding text is clearly about scheduling — then default to 9 AM.
    has_sched_cue = bool(re.search(
        r"\b(meet|meeting|coffee|call|avail|schedul|works?\b|free|lunch|dinner|"
        r"connect|calendar|can (?:he|kory|you)|could (?:he|kory|you)|do you have|grab)",
        text, re.I))
    date_only_default = 9 if has_sched_cue else None
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(slot: dict[str, str] | None) -> None:
        if slot and slot["start"] not in seen:
            seen.add(slot["start"])
            candidates.append(slot)

    month_alt = "|".join(sorted(_MONTHS, key=len, reverse=True))
    # Explicit clock time (optionally a range "12:30pm-3pm" — we take the start).
    time_re = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)"

    patterns = [
        # Month-name date: "August 25th", "July 30 at 12:30pm", "Aug 7th".
        ("month", re.compile(
            rf"\b({month_alt})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
            rf"(?:[^.\n]{{0,25}}?{time_re})?", re.I)),
        # Weekday: "Wednesday at 2pm", "Tue 9". Skip when an explicit date follows
        # ("Wednesday, 8/5" / "Wednesday August 5") so the explicit date wins.
        ("weekday", re.compile(
            r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Mon|Tue|Tues|Wed|Thu|Thurs|Fri)\b"
            rf"(?!\s*,?\s*(?:\d{{1,2}}/\d{{1,2}}|(?:{month_alt})\.?\s+\d))"
            rf"(?:[^.\n]{{0,40}}?{time_re})?", re.I)),
        # Time first, day second: "3pm on Wednesday" / "2 PM next Tuesday".
        ("time_weekday", re.compile(
            rf"{time_re}\s+(?:on\s+|this\s+|next\s+)?"
            r"(Monday|Tuesday|Wednesday|Thursday|Friday|Mon|Tue|Tues|Wed|Thu|Thurs|Fri)\b",
            re.I)),
        # Numeric date: "8/25 at 9am". "1/2 hour call at 3pm" is a duration
        # fraction, not January 2 (audit B9) — the lookahead refuses it.
        ("mdy", re.compile(
            rf"\b(\d{{1,2}})/(\d{{1,2}})(?!\s*(?:hour|hr)s?\b)"
            rf"(?:/(\d{{2,4}}))?(?:[^.\n]{{0,20}}?{time_re})?", re.I)),
    ]

    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            if _refers_to_past_meeting(text, match.start()):
                # "Great talking Monday at 3, how about Friday at 2?" — the
                # Monday is the call that already HAPPENED; parsing it made
                # next Monday 3pm a bookable candidate (audit B9).
                continue
            slot = _match_to_slot(match, now=now, kind=kind,
                                  prefer_next_week=prefer_next_week, tod_hour=tod_hour,
                                  date_only_default=date_only_default, zone=zone)
            _add(slot)
            if slot:
                # Continuation times share the named day: "Monday August 17 at
                # 10:30 AM MT and 3:30 PM MT" is TWO candidates (live H-4 —
                # the second time was silently dropped).
                for cont in _continuation_times(text[match.end():match.end() + 60]):
                    _add(_slot_at_same_day(slot, cont, zone=zone))
            if len(candidates) >= 5:
                break
    # "That Monday doesn't work, but Tuesday at 3:30 PM ET?" — the bare
    # weekday spawned a phantom Monday-9AM candidate (live H-3). When any
    # candidate carries an explicit clock time, defaulted ones are noise.
    explicit = [c for c in candidates if c.get("explicit_time")]
    chosen = explicit if explicit else candidates
    if not include_flags:
        for c in chosen:
            c.pop("explicit_time", None)
    return chosen[:5]


_PAST_REFERENCE_RE = re.compile(
    r"(?:great|good|nice|enjoyed|loved)\s+(?:talking|chatting|catching up|"
    r"speaking|meeting|call|conversation)[^.!?\n]{0,25}$"
    r"|(?:thanks?|thank you)\s+for[^.!?\n]{0,30}$"
    r"|\b(?:we|you and i)\s+(?:spoke|talked|met|chatted)[^.!?\n]{0,20}$"
    r"|\bsince\s+(?:we|our)[^.!?\n]{0,20}$",
    re.I,
)


def _refers_to_past_meeting(text: str, pos: int) -> bool:
    """True when the time at `pos` refers to a meeting that already happened."""
    return bool(_PAST_REFERENCE_RE.search(text[max(0, pos - 45):pos]))


_CONTINUATION_RE = re.compile(
    r"\s*(?:m[sd]?t|mountain(?:\s+time)?|e[sd]?t|eastern|c[sd]?t|central"
    r"|p[sd]?t|pacific)?\.?\s*(?:,\s*)?(?:and|or|,)\s+(?:at\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)"
    r"(?:\s*\(?\s*(e[sd]?t|eastern|c[sd]?t|central|m[sd]?t|mountain"
    r"|p[sd]?t|pacific)\b)?",
    re.I,
)


def _continuation_times(tail: str) -> list[tuple[int, int, str | None]]:
    """(hour24, minute, tz_label) list from 'and 3:30 PM [ET]' continuations."""
    out: list[tuple[int, int, str | None]] = []
    rest = tail
    while True:
        m = _CONTINUATION_RE.match(rest)
        if not m:
            break
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3).lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        out.append((hour, minute, (m.group(4) or "").lower() or None))
        rest = rest[m.end():]
    return out


def _slot_at_same_day(
    slot: dict[str, str],
    clock: tuple[int, int, str | None],
    *,
    zone: ZoneInfo,
) -> dict[str, str] | None:
    """A continuation time on the first candidate's day — the wall clock is in
    the continuation's own labeled zone, else the sender's writing zone."""
    try:
        start = datetime.fromisoformat(slot["start"])
        end = datetime.fromisoformat(slot["end"])
    except (KeyError, ValueError):
        return None
    duration = end - start
    label = clock[2]
    wall_zone = zone
    if label and label in _LABEL_ZONES:
        wall_zone = ZoneInfo(_LABEL_ZONES[label])
    new_start = (
        start.astimezone(wall_zone)
        .replace(hour=clock[0], minute=clock[1])
        .astimezone(MT)
    )
    return {
        "start": new_start.isoformat(),
        "end": (new_start + duration).isoformat(),
        "source": slot.get("source", "inbound_availability"),
        "explicit_time": True,
    }


def _clock(match: re.Match[str], h_grp: int, m_grp: int, ap_grp: int,
           tod_hour: int | None, date_only_default: int | None = None) -> tuple[int, int] | None:
    """Resolve (hour, minute) from a time match, or fall back to a time-of-day
    default. Returns None when neither a clock time nor a default is available."""
    if match.group(h_grp) is None:
        fallback = tod_hour if tod_hour is not None else date_only_default
        return (fallback, 0) if fallback is not None else None
    hour = int(match.group(h_grp))
    minute = int(match.group(m_grp) or 0)
    ampm = (match.group(ap_grp) or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and 1 <= hour <= 6:
        # Bare afternoon hour ("meet at 2") — business hours are PM.
        hour += 12
    return hour, minute


def _match_to_slot(
    match: re.Match[str],
    *,
    now: datetime,
    kind: str,
    prefer_next_week: bool = False,
    tod_hour: int | None = None,
    date_only_default: int | None = None,
    zone: ZoneInfo = MT,
) -> dict[str, str] | None:
    try:
        # The wall time is written in the sender's zone (an explicit label
        # right after the time wins — live H-3: "3:30 PM ET" parsed as 15:30
        # MT and hit the travel rule). The unlabeled default used to be MT
        # even for a counterpart Lexi KNOWS is Eastern (audit B9 family).
        label_zone = _tz_label_after(match)
        wall_zone = label_zone or zone
        if kind in ("weekday", "time_weekday"):
            if kind == "weekday":
                token = match.group(1).lower()
                hm = _clock(match, 2, 3, 4, tod_hour, date_only_default)
            else:  # "3pm on Wednesday" — time first, day second
                token = match.group(4).lower()
                hm = _clock(match, 1, 2, 3, tod_hour, date_only_default)
            target_wd = _WEEKDAYS.get(token[:3], _WEEKDAYS.get(token))
            if target_wd is None:
                return None
            if hm is None:
                return None
            hour, minute = hm
            day = now.date()
            # "next Tuesday" means Tuesday of NEXT week, not the nearest
            # Tuesday (audit B9): anchor the search at next week's Monday.
            preceding = match.string[max(0, match.start() - 8):match.start()]
            next_prefixed = bool(
                re.search(r"\bnext\s+$", preceding, re.I)
                or re.search(r"\bnext\s+\w+$", match.group(0), re.I)
            )
            if prefer_next_week or next_prefixed:
                day = (day - timedelta(days=day.weekday())) + timedelta(days=7)
            for _ in range(14):
                if day.weekday() == target_wd and day >= now.date():
                    break
                day += timedelta(days=1)
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=wall_zone)
            if start < now + timedelta(hours=2):
                start += timedelta(days=7)
        elif kind == "month":
            month = _MONTHS.get(match.group(1).lower())
            if not month:
                return None
            day_num = int(match.group(2))
            hm = _clock(match, 3, 4, 5, tod_hour, date_only_default)
            if hm is None:
                return None
            hour, minute = hm
            year = now.year
            start = datetime(year, month, day_num, hour, minute, tzinfo=wall_zone)
            if start < now - timedelta(hours=12):
                start = start.replace(year=year + 1)
        else:  # mdy
            month = int(match.group(1))
            day_num = int(match.group(2))
            year = int(match.group(3)) if match.group(3) else now.year
            if year < 100:
                year += 2000
            hm = _clock(match, 4, 5, 6, tod_hour, date_only_default)
            if hm is None:
                return None
            hour, minute = hm
            start = datetime(year, month, day_num, hour, minute, tzinfo=wall_zone)
            if not match.group(3) and start < now - timedelta(hours=12):
                start = start.replace(year=year + 1)
        # Reject implausible clock times (e.g. "12:27 AM" pulled from a quoted
        # reply header) — real meeting proposals fall in business hours. The
        # check runs on the WALL clock the sender wrote (a legitimate
        # "6 AM ET" is 4 AM MT — plausible to them is what matters here).
        if not (6 <= start.hour <= 21):
            return None
        start = start.astimezone(MT)
        end = start + timedelta(minutes=30)
        explicit = any(
            match.group(i) is not None
            for i in range(1, (match.lastindex or 0) + 1)
            if str(match.group(i) or "").lower().replace(".", "") in {"am", "pm"}
        )
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "source": "inbound_availability",
            "explicit_time": explicit,
        }
    except (TypeError, ValueError):
        return None


_TZ_LABEL_AFTER_RE = re.compile(
    r"^\s*\(?\s*(et|est|edt|eastern|ct|cst|cdt|central|mt|mst|mdt|mountain"
    r"|pt|pst|pdt|pacific)\b",
    re.I,
)
_LABEL_ZONES = {
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "eastern": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "central": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "mountain": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
}


def _tz_label_after(match: re.Match[str]):
    from zoneinfo import ZoneInfo

    m = _TZ_LABEL_AFTER_RE.match(match.string[match.end():match.end() + 16])
    if not m:
        return None
    return ZoneInfo(_LABEL_ZONES[m.group(1).lower()])


def validate_inbound_candidates(
    candidates: list[dict[str, str]],
    *,
    calendar_context: dict[str, Any],
    intent: str | None,
    subject: str = "",
    body: str = "",
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Return (valid_slots, invalid_slots, violation_summaries)."""
    if not candidates:
        return [], [], []

    meeting = resolve_meeting_type(intent=intent, subject=subject, body=body)
    from app.scheduling.slot_engine import infer_meeting_format

    meeting_format = infer_meeting_format(
        meeting.type_key,
        subject=subject,
        body=body,
    )
    duration = meeting.duration_minutes
    reserve = meeting.calendar_block_minutes
    busy = list(calendar_context.get("busy_events") or [])
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    notes: list[str] = []

    for raw in candidates:
        try:
            start = datetime.fromisoformat(str(raw["start"]).replace("Z", "+00:00"))
            end = start + timedelta(minutes=duration)
        except (TypeError, ValueError):
            invalid.append(raw)
            notes.append("unparseable inbound time")
            continue
        slot = {"start": start.isoformat(), "end": end.isoformat(), "source": "inbound_availability"}
        if slot_conflicts_busy(slot, busy, reserve_minutes=reserve):
            invalid.append(slot)
            notes.append(f"busy at {start.strftime('%A %I:%M %p')}")
            continue
        check = validate_proposal_slots(
            [slot],
            intent=meeting.type_key,
            meeting_format=meeting_format,
            busy_events=busy,
        )
        if check.valid:
            valid.append(slot)
        else:
            invalid.append(slot)
            notes.extend(check.violations[:2])
    return valid, invalid, notes


_SCAN_HOURS = [8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 13, 13.5, 14, 14.5,
               15, 15.5, 16, 16.5, 17, 17.5]


def find_compliant_slots_on_date(
    when: Any,
    *,
    calendar_context: dict[str, Any],
    intent: str | None,
    subject: str = "",
    body: str = "",
    near_hour: int | None = None,
    limit: int = 3,
) -> list[dict[str, str]]:
    """Up to `limit` free + rule-compliant slots on the given date, preferring times
    near near_hour. Lets Lexi offer a couple of options ON the date a prospect
    proposed (mirroring how Heidi picked a time on the requested day)."""
    if isinstance(when, str):
        anchor = datetime.fromisoformat(when[:19]).date()
    elif isinstance(when, datetime):
        anchor = when.date()
    else:
        anchor = when
    hours = sorted(
        _SCAN_HOURS,
        key=(lambda h: (abs(h - near_hour), h)) if near_hour is not None else (lambda h: h),
    )
    out: list[dict[str, str]] = []
    for h in hours:
        start = datetime(anchor.year, anchor.month, anchor.day, int(h), int((h % 1) * 60), tzinfo=MT)
        cand = [{"start": start.isoformat(), "end": start.isoformat(), "source": "inbound_availability"}]
        valid, _, _ = validate_inbound_candidates(
            cand, calendar_context=calendar_context, intent=intent, subject=subject, body=body
        )
        if valid:
            out.append(valid[0])
            if len(out) >= limit:
                break
    return out


def find_compliant_slot_on_date(when: Any, **kwargs: Any) -> dict[str, str] | None:
    kwargs.pop("limit", None)
    slots = find_compliant_slots_on_date(when, limit=1, **kwargs)
    return slots[0] if slots else None


def body_looks_like_inbound_availability(body: str) -> bool:
    combined = strip_quoted_reply(body).lower()
    if extract_inbound_time_candidates(body):
        return True
    cues = (
        "here are my times",
        "i'm available",
        "i am available",
        "my availability",
        "works for me:",
        "how about",
        "i can do",
        "i'm free",
        "i am free",
    )
    return any(c in combined for c in cues) and bool(
        re.search(r"\b(mon|tue|wed|thu|fri|am|pm|\d:\d)\b", combined)
    )
