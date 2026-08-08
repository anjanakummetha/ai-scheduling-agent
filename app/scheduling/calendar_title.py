"""Kory-style Outlook calendar titles for holds and confirmed invites."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduling.email_format import TZ_ABBREV, recipient_display_name

# Domain → display company (calendar title), when not in email signature.
DOMAIN_COMPANY_HINTS: dict[str, str] = {
    "newportadvisors.co": "Newportadvisors",
    "billd.com": "Billd",
    "evergreensurety.com": "Evergreen Surety",
    "bloomatree.com": "Blooma Tree",
    "iconicfounders.com": "Iconic Founders",
    "ifg.vc": "IFG",
}

TZ_TITLE_ABBREV = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
    "Europe/London": "UK",
}


@dataclass(frozen=True)
class GuestProfile:
    name: str
    company: str | None = None
    email: str | None = None


def parse_guest_profile(
    *,
    sender: str | None = None,
    subject: str = "",
    body: str = "",
) -> GuestProfile:
    """Best-effort guest name + company from From header, signature, or domain."""
    email = _extract_email(sender) or ""
    display = (sender or "").strip()

    name = ""
    company: str | None = None
    if "<" in display:
        name = display.split("<", 1)[0].strip().strip('"')
        paren = re.search(r"\(([^)]+)\)\s*<", display)
        if paren:
            company = paren.group(1).strip()
    elif email and display.lower() == email:
        name = _name_from_email(email)
    else:
        name = display

    if not name or "@" in name:
        name = recipient_display_name(email, body)

    if not company and email:
        company = _company_from_domain(email)

    if not company:
        company = _company_from_body(body) or _company_from_body(subject)

    name = _clean_person_name(name) or _name_from_email(email) or "Guest"
    return GuestProfile(name=name, company=company, email=email or None)


def extract_requested_attendees(
    text: str,
    *,
    primary_email: str | None = None,
    intent: str | None = None,
) -> list[str]:
    """Emails Kory asked Lexi to include (add / include / loop in / CC)."""
    if not text.strip():
        return []

    primary = (primary_email or "").strip().lower()
    kory_addrs = {e.strip().lower() for e in settings.kory_sender_emails if e.strip()}
    lexi = (settings.lexi_mailbox_email or "").strip().lower()
    blocked = kory_addrs | {lexi, primary}
    found: list[str] = []
    seen: set[str] = set()

    patterns = (
        r"(?:add|include|also\s+invite|loop\s+in|copy|cc)\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
        r"(?:add|include|also\s+invite|loop\s+in)\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
        r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
    )
    for pattern in patterns[:2]:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            addr = match.group(1).strip().lower()
            if addr and addr not in blocked and addr not in seen:
                seen.add(addr)
                found.append(addr)

    key = (intent or "").lower().replace(" ", "_")
    if key == "coffee" and re.search(r"\bkory/matt\b|\bmatt\s+will\s+join\b", text, re.I):
        for addr in _coffee_co_attendee_emails():
            if addr not in blocked and addr not in seen:
                seen.add(addr)
                found.append(addr)

    return found


def build_hold_calendar_title(
    *,
    intent: str | None,
    guest: GuestProfile,
    subject: str = "",
    body: str = "",
    option_index: int | None = None,
) -> str:
    """e.g. HOLD: Intro call w/ Anthony Garcia (podcast guest)"""
    key = _normalize_intent(intent)
    call_phrase = _hold_call_phrase(key, subject=subject, body=body)
    guest_clause = _guest_with_context(guest, key, subject=subject, body=body)
    title = f"HOLD: {call_phrase} w/ {guest_clause}"
    if option_index and option_index > 1:
        title = f"{title} (option {option_index})"
    return title[:250]


def build_confirmed_calendar_title(
    *,
    intent: str | None,
    guest: GuestProfile,
    slot_start: str,
    subject: str = "",
    body: str = "",
    recipient_timezone: ZoneInfo | None = None,
) -> str:
    """e.g. Intro: Chris Doyle (Billd) <> Kory Mitchell (IFG) | The Turn | 4:30 pm CT"""
    key = _normalize_intent(intent)
    type_label = _confirmed_type_label(key)
    guest_part = guest.name
    if guest.company:
        guest_part = f"{guest.name} ({guest.company})"

    kory_part = _kory_side_label(key, subject=subject, body=body)
    time_suffix = _format_slot_time_for_title(slot_start, recipient_timezone)
    podcast = key == "podcast" or _podcast_context(subject, body)

    base = f"{type_label}: {guest_part} <> {kory_part}"
    if podcast and time_suffix:
        return f"{base} | The Turn | {time_suffix}"[:250]
    if podcast:
        return f"{base} | The Turn"[:250]
    if time_suffix:
        if guest.company:
            return f"{base} - {time_suffix}"[:250]
        return f"{base} | {time_suffix}"[:250]
    return base[:250]


def merge_invite_attendees(
    primary_email: str | None,
    extra: list[str] | None,
    *,
    text: str = "",
    intent: str | None = None,
) -> list[str]:
    """Primary guest + any extra addresses Kory requested."""
    attendees: list[str] = []
    seen: set[str] = set()
    primary = (primary_email or "").strip().lower()
    if primary and "@" in primary:
        seen.add(primary)
        attendees.append(primary)

    for addr in extract_requested_attendees(text, primary_email=primary, intent=intent):
        if addr not in seen:
            seen.add(addr)
            attendees.append(addr)

    for addr in extra or []:
        normalized = addr.strip().lower()
        if normalized and "@" in normalized and normalized not in seen:
            seen.add(normalized)
            attendees.append(normalized)

    return attendees


def _normalize_intent(intent: str | None) -> str:
    key = (intent or "unknown").lower().replace(" ", "_")
    mapping = {"dinner_request": "dinner", "lunch_request": "lunch", "pitch": "new_client"}
    return mapping.get(key, key)


def _hold_call_phrase(intent_key: str, *, subject: str, body: str) -> str:
    if intent_key == "coffee":
        return "Coffee"
    if intent_key == "happy_hour":
        return "Happy hour"
    if intent_key == "dinner":
        return "Dinner"
    if intent_key in {"lunch", "lunch_request"}:
        return "Lunch"
    if intent_key == "new_client":
        return "Client meeting"
    if intent_key == "podcast" or _podcast_context(subject, body):
        return "Intro call"
    return "Intro call"


def _guest_with_context(
    guest: GuestProfile,
    intent_key: str,
    *,
    subject: str,
    body: str,
) -> str:
    if intent_key == "podcast" or _podcast_context(subject, body):
        if guest.company:
            return f"{guest.name} ({guest.company})"
        return f"{guest.name} (podcast guest)"
    if guest.company:
        return f"{guest.name} ({guest.company})"
    return guest.name


def _confirmed_type_label(intent_key: str) -> str:
    labels = {
        "coffee": "Coffee",
        "happy_hour": "Happy hour",
        "dinner": "Dinner",
        "lunch": "Lunch",
        "lunch_request": "Lunch",
        "new_client": "Meeting",
        "podcast": "Intro",
    }
    return labels.get(intent_key, "Intro")


def _kory_side_label(intent_key: str, *, subject: str = "", body: str = "") -> str:
    # "Kory/Matt" only when the thread says Matt is joining — the same cue that
    # adds him as an attendee. A title naming someone who isn't on the invite
    # reads as a mistake to the guest.
    if intent_key == "coffee" and re.search(
        r"\bkory/matt\b|\bmatt\s+will\s+join\b", f"{subject}\n{body}", re.I
    ):
        return "Kory/Matt (Iconic Founders)"
    return "Kory Mitchell (IFG)"


def _podcast_context(subject: str, body: str) -> bool:
    combined = f"{subject}\n{body}".lower()
    return any(cue in combined for cue in ("the turn", "podcast", "pre-interview", "recording"))


def _format_slot_time_for_title(
    slot_start: str,
    recipient_timezone: ZoneInfo | None,
) -> str:
    if not slot_start:
        return ""
    try:
        dt = datetime.fromisoformat(slot_start.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    tz = recipient_timezone or ZoneInfo(settings.scheduling_timezone)
    local = dt.astimezone(tz)
    hour = local.strftime("%-I").lstrip("0") or "12"
    minute = local.strftime("%M")
    if minute == "00":
        time_str = f"{hour} {local.strftime('%p').lower()}"
    else:
        time_str = local.strftime("%-I:%M %p").lower()
    tz_key = str(tz)
    abbrev = TZ_TITLE_ABBREV.get(tz_key) or TZ_ABBREV.get(tz_key, "MT")
    if abbrev in {"Eastern", "Central", "Mountain", "Pacific"}:
        abbrev = {"Eastern": "ET", "Central": "CT", "Mountain": "MT", "Pacific": "PT"}.get(
            abbrev, abbrev
        )
    return f"{time_str} {abbrev}"


def _extract_email(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", value)
    return match.group(0).lower() if match else None


def _clean_person_name(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    text = re.sub(r"^(mr|ms|mrs|dr)\.?\s+", "", text, flags=re.I)
    return text.title() if text else ""


def _name_from_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    local = re.sub(r"[._+-]+", " ", local).strip()
    return local.title()


def _company_from_domain(email: str) -> str | None:
    domain = email.split("@", 1)[-1].lower()
    if domain in DOMAIN_COMPANY_HINTS:
        return DOMAIN_COMPANY_HINTS[domain]
    if domain.endswith(".co.uk"):
        return None
    stem = domain.split(".", 1)[0]
    if stem in {"gmail", "yahoo", "hotmail", "outlook", "icloud", "example"}:
        return None
    return stem.replace("-", " ").title()


def _company_from_body(text: str) -> str | None:
    if not text:
        return None
    for pattern in (
        r"\b(?:at|from|with)\s+([A-Z][A-Za-z0-9 &.'-]{2,40}(?:\s+(?:LLC|Inc|Co)\.?)?)\b",
        r"\|\s*([A-Z][A-Za-z0-9 &.'-]{2,30})\s*\|",
    ):
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1).strip()
            if candidate.lower() not in {"kory", "lexi", "teams", "zoom", "the turn"}:
                return candidate
    return None


def _coffee_co_attendee_emails() -> tuple[str, ...]:
    import os

    raw = os.getenv("LEXI_COFFEE_CO_ATTENDEES", "matt@ifg.vc").strip()
    return tuple(e.strip().lower() for e in raw.split(",") if e.strip() and "@" in e)
