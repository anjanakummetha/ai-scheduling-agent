"""Outlook calendar helpers backed by Composio tools."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from copy import deepcopy
from typing import Any
from zoneinfo import ZoneInfo

import logging

from app.config import settings
from app.integrations.composio_client import execute_read_tool, execute_write_tool

logger = logging.getLogger(__name__)

SCHEDULING_TIMEZONE = settings.scheduling_timezone
OUTLOOK_TIMEZONE = settings.outlook_timezone
TIMEZONE = SCHEDULING_TIMEZONE
NON_BLOCKING_OBSERVANCES = {
    "good friday",
    "palm sunday",
    "easter day",
    "tax day",
}
NON_BLOCKING_ALL_DAY_PREFIXES = (
    "stay at ",
)
HOLD_SUBJECT_RE = re.compile(r"^hold\s*:", re.IGNORECASE)


def get_calendar_events(start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], str | None]:
    result = execute_read_tool(
        "OUTLOOK_GET_CALENDAR_VIEW",
        {
            "user_id": "me",
            "start_datetime": _convert_iso_timezone(start_iso, SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE),
            "end_datetime": _convert_iso_timezone(end_iso, SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE),
            "timezone": OUTLOOK_TIMEZONE,
            "top": 250,
            "select": [
                "id",
                "subject",
                "start",
                "end",
                "showAs",
                "isCancelled",
                "isAllDay",
                # Needed by the pre-meeting brief to identify who Kory is
                # meeting; without them it fell back to using the whole
                # subject line as a person's name.
                "attendees",
                "organizer",
            ],
        },
    )
    data = _coerce_data(result["data"])
    events = data.get("value") or data.get("events") or data.get("data") or []
    if isinstance(events, dict):
        events = events.get("value", [])
    return _events_to_scheduling_timezone(events) if isinstance(events, list) else [], result.get("log_id")


def create_calendar_event(calendar_action: dict[str, Any]) -> tuple[str | None, str | None]:
    if settings.lexi_dry_run:
        preview_id = f"dry-run-event-{calendar_action.get('start', '')[:19]}"
        logger.info(
            "[DRY RUN] Would create Outlook event: %s",
            calendar_action,
        )
        print(
            "\n[Lexi DRY RUN] Calendar event NOT created. Would have scheduled:\n"
            f"  Title: {calendar_action.get('title')}\n"
            f"  Start: {calendar_action.get('start')}\n"
            f"  End:   {calendar_action.get('end')}\n"
            f"  Attendees: {calendar_action.get('attendees')}\n",
            flush=True,
        )
        return preview_id, "dry-run-no-log"

    start = _convert_iso_timezone(calendar_action["start"], SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE)
    end = _convert_iso_timezone(calendar_action["end"], SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE)
    attendees = [
        {
            "emailAddress": {"address": email},
            "type": "required",
        }
        for email in calendar_action.get("attendees", [])
    ]

    location = calendar_action.get("location", "Microsoft Teams")
    is_online = calendar_action.get("is_online_meeting")
    if is_online is None:
        is_online = str(location).lower() in {"teams", "microsoft teams", "zoom"}
    body_text = (
        calendar_action.get("body")
        or "Created by AI Scheduling Agent after dashboard approval."
    )
    payload: dict[str, Any] = {
        "user_id": "me",
        "subject": calendar_action.get("title", "Meeting with Kory"),
        "start": {"dateTime": start, "timeZone": OUTLOOK_TIMEZONE},
        "end": {"dateTime": end, "timeZone": OUTLOOK_TIMEZONE},
        "location": {"displayName": location},
        "attendees": attendees,
        "isOnlineMeeting": bool(is_online),
        "body": {
            "contentType": "text",
            "content": body_text,
        },
    }
    if is_online:
        payload["onlineMeetingProvider"] = "teamsForBusiness"

    result = execute_write_tool("OUTLOOK_CREATE_ME_EVENT", payload)
    data = _coerce_data(result["data"])
    _invalidate_scheduling_cache()
    return data.get("id"), result.get("log_id")


def delete_calendar_event(event_id: str) -> str | None:
    if settings.lexi_dry_run:
        if event_id.startswith("hold-pending-") or event_id.startswith("dry-run-"):
            return "dry-run-no-log"
        logger.info("[DRY RUN] Would delete Outlook event: %s", event_id)
        print(f"\n[Lexi DRY RUN] Would delete calendar event: {event_id}\n", flush=True)
        return "dry-run-no-log"
    result = execute_write_tool(
        "OUTLOOK_DELETE_CALENDAR_EVENT",
        {
            "user_id": "me",
            "event_id": event_id,
        },
    )
    _invalidate_scheduling_cache()
    return result.get("log_id")


def _invalidate_scheduling_cache() -> None:
    """Lexi's own calendar writes must be visible to her next slot search —
    the context cache now lives 30 minutes, long enough to offer a slot she
    herself just held if we didn't clear it here."""
    try:
        from app.scheduling.calendar_context import clear_scheduling_calendar_context_cache

        clear_scheduling_calendar_context_cache()
    except Exception:  # noqa: BLE001 — cache clearing must never break a write
        pass


def get_write_calendar_events(start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], str | None]:
    """Calendar on write mailbox (sandbox in pilot)."""
    result = execute_write_tool(
        "OUTLOOK_GET_CALENDAR_VIEW",
        {
            "user_id": "me",
            "start_datetime": _convert_iso_timezone(start_iso, SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE),
            "end_datetime": _convert_iso_timezone(end_iso, SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE),
            "timezone": OUTLOOK_TIMEZONE,
            "top": 250,
            "select": [
                "id",
                "subject",
                "start",
                "end",
                "showAs",
                "isCancelled",
                "isAllDay",
                # Needed by the pre-meeting brief to identify who Kory is
                # meeting; without them it fell back to using the whole
                # subject line as a person's name.
                "attendees",
                "organizer",
            ],
        },
    )
    data = _coerce_data(result["data"])
    events = data.get("value") or data.get("events") or data.get("data") or []
    if isinstance(events, dict):
        events = events.get("value", [])
    return _events_to_scheduling_timezone(events) if isinstance(events, list) else [], result.get("log_id")


def has_write_calendar_conflict(
    calendar_action: dict[str, Any],
    *,
    ignore_event_ids: list[str] | None = None,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    start = calendar_action["start"]
    end = calendar_action["end"]
    start_dt = _slot_datetime(start)
    end_dt = _slot_datetime(end)
    if not start_dt or not end_dt:
        return True, [], None
    window_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    window_end = (end_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
    events, log_id = get_write_calendar_events(window_start, window_end)
    ignored = set(ignore_event_ids or [])

    conflicts = [
        event
        for event in events
        if event.get("id") not in ignored
        and is_blocking_event(event)
        and _event_overlaps(event, start_dt, end_dt)
    ]
    return bool(conflicts), conflicts, log_id


def has_conflict(
    calendar_action: dict[str, Any],
    *,
    ignore_event_ids: list[str] | None = None,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    start = calendar_action["start"]
    end = calendar_action["end"]
    start_dt = _slot_datetime(start)
    end_dt = _slot_datetime(end)
    if not start_dt or not end_dt:
        return True, [], None
    window_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    window_end = (end_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
    events, log_id = get_calendar_events(window_start, window_end)
    ignored = set(ignore_event_ids or [])

    conflicts = [
        event
        for event in events
        if event.get("id") not in ignored
        and is_blocking_event(event)
        and _event_overlaps(event, start_dt, end_dt)
    ]
    return bool(conflicts), conflicts, log_id


def is_scheduling_hold(event: dict[str, Any]) -> bool:
    subject = str(event.get("subject") or "").strip()
    return bool(HOLD_SUBJECT_RE.match(subject))


def is_blocking_event(event: dict[str, Any]) -> bool:
    """Busy events that block scheduling — includes Lexi HOLD: blocks on Master/work calendars."""
    return _is_busy(event) and not _is_demo_observance(event)


def _is_busy(event: dict[str, Any]) -> bool:
    return not event.get("isCancelled") and (event.get("showAs") or "busy").lower() != "free"


def _is_demo_observance(event: dict[str, Any]) -> bool:
    subject = str(event.get("subject") or "").removeprefix("[DEMO KORY]").strip().lower()
    is_non_blocking = subject in NON_BLOCKING_OBSERVANCES or subject.startswith(NON_BLOCKING_ALL_DAY_PREFIXES)
    if not is_non_blocking:
        return False

    event_start = _event_datetime(event.get("start"))
    event_end = _event_datetime(event.get("end"))
    if not event_start or not event_end:
        return False

    return bool(event.get("isAllDay")) or event_end - event_start >= timedelta(hours=23)


def _event_overlaps(event: dict[str, Any], start: datetime, end: datetime) -> bool:
    event_start = _event_datetime(event.get("start"))
    event_end = _event_datetime(event.get("end"))
    if not event_start or not event_end:
        return True
    return event_start < end and event_end > start


def _slot_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(ZoneInfo(SCHEDULING_TIMEZONE)).replace(tzinfo=None)
    return parsed


def _event_datetime(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("dateTime")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _coerce_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return {"value": data}


def _events_to_scheduling_timezone(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_event_to_scheduling_timezone(event) for event in events]


def _event_to_scheduling_timezone(event: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(event)
    for key in ("start", "end"):
        value = normalized.get(key)
        if isinstance(value, dict) and isinstance(value.get("dateTime"), str):
            source_tz = value.get("timeZone") or OUTLOOK_TIMEZONE
            value["dateTime"] = _convert_iso_timezone(
                value["dateTime"],
                source_tz,
                SCHEDULING_TIMEZONE,
            )
            value["timeZone"] = SCHEDULING_TIMEZONE
    return normalized


def _convert_iso_timezone(value: str, from_timezone: str, to_timezone: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(from_timezone))
    return dt.astimezone(ZoneInfo(to_timezone)).replace(tzinfo=None).isoformat(timespec="seconds")
