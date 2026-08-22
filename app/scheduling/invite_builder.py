"""Build Outlook hold/invite payloads from meeting intent and Kory's rules."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import rules as kory_rules

from app.scheduling.calendar_title import (
    build_confirmed_calendar_title,
    build_hold_calendar_title,
    merge_invite_attendees,
    parse_guest_profile,
)

VIRTUAL_INTENTS = frozenset(
    {
        "virtual_30",
        "meeting_request",
        "referral_or_intro",
        "internal_sync",
        "delegation",
        "reschedule",
        "unknown",
        "pitch",
        "new_client",
        "podcast",
    }
)
IN_PERSON_INTENTS = frozenset({"coffee", "happy_hour", "dinner", "dinner_request", "lunch", "lunch_request"})


def _normalize_intent(intent: str | None) -> str:
    key = (intent or "unknown").lower().replace(" ", "_")
    mapping = {"dinner_request": "dinner", "lunch_request": "lunch", "pitch": "new_client"}
    return mapping.get(key, key)


def _venue_with_address(name: str) -> str:
    address = kory_rules.VENUE_ADDRESSES.get(name)
    return f"{name} ({address})" if address else name


def _venue_open_at(name: str, slot_start: str | None) -> bool:
    """True unless the venue's opening time is after the slot start."""
    opens = kory_rules.VENUE_OPENS_AT.get(name)
    if not opens or not slot_start:
        return True
    try:
        from datetime import datetime

        local = datetime.fromisoformat(slot_start.replace("Z", "+00:00"))
        return local.strftime("%H:%M") >= opens
    except (TypeError, ValueError):
        return True


def default_location_for_intent(intent: str | None, slot_start: str | None = None) -> str:
    """Coffee/lunch → Cherry Creek venue; calls → Teams; dinner/happy hour → venue."""
    key = _normalize_intent(intent)
    if key in VIRTUAL_INTENTS:
        return "Microsoft Teams"
    if key == "coffee":
        locations = kory_rules.MEETING_TYPES.get("coffee", {}).get("locations") or []
        return _venue_with_address(str(locations[0])) if locations else "Cherry Creek (TBD)"
    if key == "happy_hour":
        locations = [
            str(v)
            for v in (kory_rules.MEETING_TYPES.get("happy_hour", {}).get("locations") or [])
            if _venue_open_at(str(v), slot_start)
        ]
        return _venue_with_address(locations[0]) if locations else "Cherry Creek Grill"
    if key == "dinner":
        return str(kory_rules.MEETING_TYPES.get("dinner", {}).get("location_preference") or "Cherry Creek")
    if key in {"lunch", "lunch_request"}:
        return "Cherry Creek (TBD)"
    return "Microsoft Teams"


def is_online_meeting(intent: str | None, location: str) -> bool:
    key = _normalize_intent(intent)
    if key in IN_PERSON_INTENTS:
        return False
    loc = (location or "").lower()
    configured_venues = tuple(v.lower() for v in kory_rules.VENUE_ADDRESSES)
    if any(v in loc for v in ("cherry creek", "restaurant", *configured_venues)):
        return False
    return "teams" in loc or "zoom" in loc or key in VIRTUAL_INTENTS


def hold_subject(
    meeting_subject: str | None,
    *,
    option_index: int | None = None,
    intent: str | None = None,
    sender: str | None = None,
    body: str = "",
) -> str:
    """Legacy wrapper — prefer build_hold_calendar_title via build_hold_action."""
    guest = parse_guest_profile(sender=sender, subject=meeting_subject or "", body=body)
    return build_hold_calendar_title(
        intent=intent,
        guest=guest,
        subject=meeting_subject or "",
        body=body,
        option_index=option_index,
    )


def confirmed_invite_subject(
    meeting_subject: str | None,
    sender: str | None = None,
    *,
    intent: str | None = None,
    slot_start: str = "",
    body: str = "",
    recipient_timezone: ZoneInfo | None = None,
) -> str:
    """Legacy wrapper — prefer build_confirmed_calendar_title via build_invite_action."""
    guest = parse_guest_profile(sender=sender, subject=meeting_subject or "", body=body)
    return build_confirmed_calendar_title(
        intent=intent,
        guest=guest,
        slot_start=slot_start,
        subject=meeting_subject or "",
        body=body,
        recipient_timezone=recipient_timezone,
    )


def build_hold_action(
    *,
    slot: dict[str, str],
    meeting_subject: str | None,
    intent: str | None,
    option_index: int,
    sender: str | None = None,
    body: str = "",
) -> dict[str, Any]:
    # The meeting TYPE was decided at offer time from the ask's own words —
    # "grab coffee" with intent=referral_or_intro is a COFFEE (in-person,
    # Cherry Creek), and the offer email said so. Re-deriving from the raw
    # intent alone here sent a Teams-link invite titled "Intro:" for a coffee
    # the email promised in person (live 10563). Resolve the same way the
    # offer did, subject and body included.
    from app.scheduling.meeting_type import normalize_scheduling_intent

    intent = normalize_scheduling_intent(
        intent, subject=meeting_subject or "", body=body or ""
    )
    location = default_location_for_intent(intent, slot.get("start"))
    guest = parse_guest_profile(sender=sender, subject=meeting_subject or "", body=body)
    title = build_hold_calendar_title(
        intent=intent,
        guest=guest,
        subject=meeting_subject or "",
        body=body,
        option_index=option_index,
    )
    return {
        "title": title,
        "start": slot["start"],
        "end": slot["end"],
        "attendees": [],
        "location": location,
        "body": (
            f"Lexi tentative hold (option {option_index}) while Kory offers times.\n"
            f"Slot: {slot['start']} → {slot['end']}"
        ),
        "is_online_meeting": False,
    }


def build_invite_action(
    *,
    slot: dict[str, str],
    meeting_subject: str | None,
    intent: str | None,
    attendee_email: str | None,
    sender_display: str | None = None,
    body_note: str = "",
    body: str = "",
    extra_attendees: list[str] | None = None,
    recipient_timezone: ZoneInfo | None = None,
) -> dict[str, Any]:
    # The meeting TYPE was decided at offer time from the ask's own words —
    # "grab coffee" with intent=referral_or_intro is a COFFEE (in-person,
    # Cherry Creek), and the offer email said so. Re-deriving from the raw
    # intent alone here sent a Teams-link invite titled "Intro:" for a coffee
    # the email promised in person (live 10563). Resolve the same way the
    # offer did, subject and body included.
    from app.scheduling.meeting_type import normalize_scheduling_intent

    intent = normalize_scheduling_intent(
        intent, subject=meeting_subject or "", body=body or ""
    )
    location = default_location_for_intent(intent, slot.get("start"))
    online = is_online_meeting(intent, location)
    combined_text = f"{meeting_subject or ''}\n{body or ''}\n{body_note or ''}"
    attendees = merge_invite_attendees(
        attendee_email,
        extra_attendees,
        text=combined_text,
        intent=intent,
    )
    guest = parse_guest_profile(
        sender=sender_display,
        subject=meeting_subject or "",
        body=body or body_note,
    )
    title = build_confirmed_calendar_title(
        intent=intent,
        guest=guest,
        slot_start=slot.get("start", ""),
        subject=meeting_subject or "",
        body=body or body_note,
        recipient_timezone=recipient_timezone,
    )
    note = body_note.strip() or "Scheduled by Lexi after Kory approved the calendar invite."
    if online:
        note += "\n\nMicrosoft Teams meeting — link will be on the invite."
    if len(attendees) > 1:
        note += f"\n\nAttendees: {', '.join(attendees)}"
    return {
        "title": title,
        "start": slot["start"],
        "end": slot["end"],
        "attendees": attendees,
        "location": "Microsoft Teams" if online else location,
        "body": note,
        "is_online_meeting": online,
    }
