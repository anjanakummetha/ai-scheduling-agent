"""Parse which offered slot a recipient selected in their reply."""

from __future__ import annotations

import re
from typing import Any

from app.scheduling.busy_intervals import parse_iso_datetime
from app.scheduling.email_format import format_slot_for_email


def _reply_text_for_matching(body: str) -> str:
    """Use only the recipient's new text — not quoted offer lines below."""
    text = (body or "").strip()
    if not text:
        return ""
    lower = text.lower()
    for marker in (
        "\nfrom:",
        "\n-----original message-----",
        "\n________________________________",
        "\n> ",
        "\non ",
        "[prior messages in this email chain]",
    ):
        idx = lower.find(marker)
        if idx > 0:
            text = text[:idx]
            lower = text.lower()
    return text.strip()


def match_recipient_slot_choice(
    body: str,
    proposed_slots: list[dict[str, str]],
    *,
    sender_email: str | None = None,
) -> dict[str, str] | None:
    """Return the matching slot dict if the reply picks one of the offered times."""
    if not body or not proposed_slots:
        return None
    text = _reply_text_for_matching(body).lower()
    if not text:
        return None

    for pattern, index in (
        (r"\boption\s*1\b", 0),
        (r"\boption\s*2\b", 1),
        (r"\boption\s*3\b", 2),
        (r"\bfirst (?:one|option|time|slot)\b", 0),
        (r"\bsecond (?:one|option|time|slot)\b", 1),
        (r"\bthird (?:one|option|time|slot)\b", 2),
        (r"\b1(?:st)?\s+(?:works|is fine|sounds good)\b", 0),
        (r"\b2(?:nd)?\s+(?:works|is fine|sounds good)\b", 1),
        (r"\b3(?:rd)?\s+(?:works|is fine|sounds good)\b", 2),
    ):
        if re.search(pattern, text) and index < len(proposed_slots):
            return proposed_slots[index]

    for slot in proposed_slots:
        start = parse_iso_datetime(str(slot.get("start") or ""))
        if not start:
            continue
        weekday = start.strftime("%A").lower()
        if weekday in text:
            hour_token = str(int(start.strftime("%I")))
            minute_token = start.strftime("%M")
            if re.search(
                rf"\b{re.escape(weekday)}\b[^\n]{{0,40}}\b{hour_token}(?::{minute_token})?\b",
                text,
            ):
                return slot

    text_dates = _month_days_in_text(text)
    text_times = _clock_times_in_text(text)
    for slot in proposed_slots:
        formatted = format_slot_for_email(slot, recipient_tz=None).lower()
        day_part = formatted.split(" at ", 1)[0] if " at " in formatted else ""
        start = parse_iso_datetime(str(slot.get("start") or ""))
        if day_part and day_part in text:
            # Even an exact "monday, august 10" mention is a pick only if any
            # stated clock time is consistent with this slot.
            if not _time_contradicts_slot(text_times, start):
                return slot
            continue
        if not start:
            continue
        weekday = start.strftime("%A").lower()
        if re.search(rf"\b{re.escape(weekday)}\b", text) and len(proposed_slots) <= 3:
            # Bare weekday ("Monday works") counts only when it cannot be wrong
            # (live H-4: "Monday, August 17 at 1:00 PM" matched the Aug 10 slot
            # and nearly booked a time the recipient explicitly declined):
            # the weekday must be UNAMBIGUOUS among the offered slots, and any
            # explicit date/time in the reply must agree with this slot.
            same_day = [
                s
                for s in proposed_slots
                if (ss := parse_iso_datetime(str(s.get("start") or "")))
                and ss.strftime("%A").lower() == weekday
            ]
            if len(same_day) != 1 or same_day[0] is not slot:
                continue
            if text_dates and (start.month, start.day) not in text_dates:
                continue
            if _time_contradicts_slot(text_times, start):
                continue
            return slot

    if re.search(r"\b(any|either|all)\b.*\bwork", text) and proposed_slots:
        return proposed_slots[0]

    for slot in proposed_slots:
        start = parse_iso_datetime(str(slot.get("start") or ""))
        if not start:
            continue
        from app.config import settings
        from zoneinfo import ZoneInfo

        local = start.astimezone(ZoneInfo(settings.scheduling_timezone))
        hour12 = int(local.strftime("%I"))
        minute = local.strftime("%M")
        hour_variants = {str(hour12), f"{hour12:02d}"}
        for hour_token in hour_variants:
            for pattern in (
                rf"\b{hour_token}:{minute}\b[^\n]{{0,30}}\b(?:works|is fine|sounds good|good for me)\b",
                rf"\b{hour_token}(?::{minute})?\s*(?:am|pm)\b[^\n]{{0,30}}\b(?:works|is fine|sounds good)\b",
            ):
                if re.search(pattern, text):
                    return slot

    return None


_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _month_days_in_text(text: str) -> set[tuple[int, int]]:
    """Explicit (month, day) mentions like 'august 17' / 'Aug 17th'."""
    found: set[tuple[int, int]] = set()
    for name, day in re.findall(
        r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text
    ):
        found.add((_MONTHS[name], int(day)))
    return found


_TZ_LABELS = {
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "eastern": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "central": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "mountain": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
}
_UNLABELED_ZONES = (
    "America/Denver",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
)


def _clock_times_in_text(text: str) -> set[tuple[int, int, str | None]]:
    """12h clock mentions as (hour12, minute, tz_label_or_None) — '1:00 pm et'."""
    found: set[tuple[int, int, str | None]] = set()
    pattern = (
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
        r"(?:\s*\(?\s*(" + "|".join(_TZ_LABELS) + r")\b)?"
    )
    for hour, minute, _ampm, label in re.findall(pattern, text):
        h = int(hour)
        if 1 <= h <= 12:
            found.add((h, int(minute or 0), label or None))
    return found


def _time_contradicts_slot(
    text_times: set[tuple[int, int, str | None]], start: Any
) -> bool:
    """True when the reply states clock times and NONE could mean this slot.

    The offer email renders every slot recipient-local first with MT in
    parentheses, so a genuine pick echoes one of those renderings. A zone
    label in the reply ('1:00 PM ET') is compared in THAT zone only;
    unlabeled times are compared across the common US zones.
    """
    if not text_times or start is None:
        return False
    from zoneinfo import ZoneInfo

    for hour, minute, label in text_times:
        zones = (_TZ_LABELS[label],) if label else _UNLABELED_ZONES
        for tz in zones:
            local = start.astimezone(ZoneInfo(tz))
            if (int(local.strftime("%I")), local.minute) == (hour, minute):
                return False
    return True


_REJECTION_PATTERNS = (
    r"\bnone of (?:the |those |these |them )?(?:times|options|slots)?\s*(?:quite\s+)?works?\b",
    r"\bnone of (?:the |those )?(?:times|options|slots)\b",
    r"\b(?:don'?t|do not|doesn'?t|does not|won'?t|will not|can'?t|cannot)\s+(?:quite\s+|really\s+)?work\b",
    r"\bnot (?:going to |gonna )?work\b",
    r"\bnone work\b",
    r"\bno (?:of the )?(?:times|options) work\b",
    r"\bneed (?:different|other|new) times\b",
    r"\b(?:different|other|new) times\b",
    r"\bnot available\b",
    r"\bwon't be available\b",
    r"\bcan't make (?:any|those|it)\b",
    r"\bunavailable (?:on|for|at)\b",
    r"\bwhat else (?:do you|have you) got\b",
    r"\bany other (?:times|options|availability)\b",
)


def recipient_times_rejected(body: str) -> bool:
    """True when the reply indicates offered slots don't work (not a slot pick)."""
    if not body.strip():
        return False
    text = body.lower()
    return any(re.search(p, text) for p in _REJECTION_PATTERNS)
