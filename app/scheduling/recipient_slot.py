"""Parse which offered slot a recipient selected in their reply.

Offer emails render every slot recipient-local first with MT in parentheses,
so a reply naming a day or hour is in ONE of those two zones — never UTC.
parse_iso_datetime normalizes to aware UTC, which means every weekday/hour
token must be derived through an astimezone() into a rendered zone: a
Monday 6 PM MT dinner slot is Tuesday in UTC, and matching raw UTC fields
booked the wrong day. When the two zones disagree about which slot a reply
means, the reply is treated as unparsed (Kory reviews) rather than guessed.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.scheduling.busy_intervals import parse_iso_datetime


def _rendering_zones(recipient_tz: str | None) -> list[ZoneInfo]:
    """Zones the offer email showed times in: recipient-local first, then MT."""
    from app.config import settings

    mt = ZoneInfo(settings.scheduling_timezone)
    zones: list[ZoneInfo] = []
    if recipient_tz:
        try:
            zone = ZoneInfo(str(recipient_tz))
        except Exception:  # noqa: BLE001 — a junk stored tz must not break parsing
            zone = None
        if zone is not None and str(zone) != str(mt):
            zones.append(zone)
    zones.append(mt)
    return zones


def _unique_slot(matches: list[dict[str, str]]) -> dict[str, str] | None:
    """The single distinct slot in matches, or None when 0 or ambiguous."""
    distinct: list[dict[str, str]] = []
    seen: set[str] = set()
    for slot in matches:
        key = str(slot.get("start") or id(slot))
        if key not in seen:
            seen.add(key)
            distinct.append(slot)
    return distinct[0] if len(distinct) == 1 else None


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
    recipient_tz: str | None = None,
) -> dict[str, str] | None:
    """Return the matching slot dict if the reply picks one of the offered times.

    recipient_tz is the zone the offer email rendered first (stored on the
    proposal); day/hour tokens are matched in that zone AND in MT. A reply
    whose tokens fit different slots in different zones is ambiguous → None.
    """
    if not body or not proposed_slots:
        return None
    text = _reply_text_for_matching(body).lower()
    if not text:
        return None

    zones = _rendering_zones(recipient_tz)

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

    text_dates = _month_days_in_text(text)
    text_times = _clock_times_in_text(text)

    # Zones are tried in the order the offer email rendered them: the
    # recipient's own zone first, MT second. A reply hour is read in the
    # first zone that resolves it — "Tuesday at 4" from an Eastern
    # counterpart means 4 PM ET even if another slot sits at 4 PM MT.
    for zone in zones:
        zone_matches: list[dict[str, str]] = []
        for slot in proposed_slots:
            start = parse_iso_datetime(str(slot.get("start") or ""))
            if not start:
                continue
            # An explicit "august 17" in the reply must be this slot's date —
            # live H-4: "Monday, August 17 at 1:00 PM" nearly booked the
            # Aug 10 slot the recipient had declined.
            if text_dates and all(
                (start.astimezone(z).month, start.astimezone(z).day) not in text_dates
                for z in zones
            ):
                continue
            local = start.astimezone(zone)
            weekday = local.strftime("%A").lower()
            if weekday not in text:
                continue
            hour_token = str(int(local.strftime("%I")))
            minute_token = local.strftime("%M")
            # The hour must be a clock mention, not a date digit ("August 10"):
            # anchored by "at"/"@", or carrying :minutes or a meridiem.
            if re.search(
                rf"\b{re.escape(weekday)}\b[^\n]{{0,40}}?"
                rf"(?:(?:\bat|@)\s*{hour_token}(?::{minute_token})?\b"
                rf"|\b{hour_token}:{minute_token}\b"
                rf"|\b{hour_token}(?::{minute_token})?\s*(?:am|pm)\b)",
                text,
            ) and not _time_contradicts_slot(text_times, start):
                zone_matches.append(slot)
        chosen = _unique_slot(zone_matches)
        if chosen:
            return chosen
        if zone_matches:
            return None  # day+hour fits several slots in ONE zone — ask Kory
    for slot in proposed_slots:
        start = parse_iso_datetime(str(slot.get("start") or ""))
        if not start:
            continue
        if any(
            _day_phrase(start, zone) in text for zone in zones
        ):
            # Even an exact "monday, august 10" mention is a pick only if any
            # stated clock time is consistent with this slot.
            if not _time_contradicts_slot(text_times, start):
                return slot
            continue
        matched_zone = next(
            (
                zone
                for zone in zones
                if re.search(
                    rf"\b{re.escape(start.astimezone(zone).strftime('%A').lower())}\b",
                    text,
                )
            ),
            None,
        )
        if matched_zone is not None and len(proposed_slots) <= 3:
            # Bare weekday ("Monday works") counts only when it cannot be wrong
            # (live H-4: "Monday, August 17 at 1:00 PM" matched the Aug 10 slot
            # and nearly booked a time the recipient explicitly declined):
            # the weekday must be UNAMBIGUOUS among the offered slots — in the
            # same zone the match was made in — and any explicit date/time in
            # the reply must agree with this slot.
            weekday = start.astimezone(matched_zone).strftime("%A").lower()
            same_day = [
                s
                for s in proposed_slots
                if (ss := parse_iso_datetime(str(s.get("start") or "")))
                and ss.astimezone(matched_zone).strftime("%A").lower() == weekday
            ]
            if len(same_day) != 1 or same_day[0] is not slot:
                continue
            if text_dates and all(
                (start.astimezone(z).month, start.astimezone(z).day) not in text_dates
                for z in zones
            ):
                continue
            if _time_contradicts_slot(text_times, start):
                continue
            return slot

    if re.search(r"\b(any|either|all)\b.*\bwork", text) and proposed_slots:
        return proposed_slots[0]

    for zone in zones:  # recipient-rendered zone first, MT second
        hour_matches: list[dict[str, str]] = []
        for slot in proposed_slots:
            start = parse_iso_datetime(str(slot.get("start") or ""))
            if not start:
                continue
            local = start.astimezone(zone)
            hour12 = int(local.strftime("%I"))
            minute = local.strftime("%M")
            hour_variants = {str(hour12), f"{hour12:02d}"}
            matched = False
            for hour_token in hour_variants:
                for pattern in (
                    rf"\b{hour_token}:{minute}\b[^\n]{{0,30}}\b(?:works|is fine|sounds good|good for me)\b",
                    rf"\b{hour_token}(?::{minute})?\s*(?:am|pm)\b[^\n]{{0,30}}\b(?:works|is fine|sounds good)\b",
                ):
                    if re.search(pattern, text):
                        matched = True
                        break
                if matched:
                    break
            if matched:
                hour_matches.append(slot)
        chosen = _unique_slot(hour_matches)
        if chosen:
            return chosen
        if hour_matches:
            return None  # hour fits several slots in one zone — ask Kory
    return None


def _day_phrase(start: datetime, zone: ZoneInfo) -> str:
    """The email's day rendering ('monday, august 10') in the given zone."""
    local = start.astimezone(zone)
    return local.strftime("%A, %B %-d").lower()


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
    # Window shift = the offered times don't work: "could we look at the
    # following week instead?" (live H-5 — fell to unparsed, holds kept).
    r"\b(?:look at|do|try|move (?:it|this|to)|push (?:it|this)? ?to|how about|"
    r"switch to|go with)\s+(?:the\s+)?(?:following|next|another|a different)\s+week\b",
    r"\b(?:following|another|a different) week instead\b",
    r"\bthat (?:whole )?week\b[^.\n]{0,50}\b(?:messy|full|busy|bad|tough|"
    r"doesn'?t work|won'?t work|not (?:great|good|ideal))\b",
)


def recipient_times_rejected(body: str) -> bool:
    """True when the reply indicates offered slots don't work (not a slot pick).

    Only the sender's NEW text counts: the quoted history below a Gmail reply
    carries every earlier "none of those work" line, and scanning it turned a
    plain "Sounds good, thanks!" into a rejection (live H-6).
    """
    if not body.strip():
        return False
    text = _reply_text_for_matching(body).lower()
    if not text:
        return False
    return any(re.search(p, text) for p in _REJECTION_PATTERNS)
