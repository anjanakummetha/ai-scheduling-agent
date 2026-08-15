"""Named Outlook calendar resolution, multi-calendar reads, and targeted writes."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from app.config import ROOT_DIR, settings
from app.integrations.composio_client import execute_read_tool, execute_write_tool
from app.integrations.outlook_calendar import (
    OUTLOOK_TIMEZONE,
    SCHEDULING_TIMEZONE,
    _convert_iso_timezone,
    _coerce_data,
    _events_to_scheduling_timezone,
    is_blocking_event,
)

logger = logging.getLogger(__name__)

CALENDARS_CONFIG_PATH = ROOT_DIR / "config" / "calendars.yaml"
ConnectionRole = Literal["read", "write"]
_LIST_ALL_CALENDARS_TTL_SEC = 600.0
_list_all_calendars_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_resolve_calendar_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}


def _load_calendar_config() -> dict[str, Any]:
    if not CALENDARS_CONFIG_PATH.exists():
        return {}
    with CALENDARS_CONFIG_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _execute_calendar_tool(
    tool_slug: str,
    arguments: dict[str, Any],
    *,
    role: ConnectionRole,
) -> dict[str, Any]:
    if role == "read":
        return execute_read_tool(tool_slug, arguments)
    return execute_write_tool(tool_slug, arguments)


def clear_calendar_list_cache() -> None:
    """Drop cached Outlook calendar listings (tests / forced refresh)."""
    _list_all_calendars_cache.clear()
    _resolve_calendar_cache.clear()


def list_all_calendars(*, role: ConnectionRole = "read") -> list[dict[str, Any]]:
    """List primary + calendar-group calendars for read or write connection."""
    now = time.monotonic()
    cached = _list_all_calendars_cache.get(role)
    if cached and now - cached[0] < _LIST_ALL_CALENDARS_TTL_SEC:
        return cached[1]

    calendars = _fetch_all_calendars(role=role)
    _list_all_calendars_cache[role] = (now, calendars)
    return calendars


def _fetch_all_calendars(*, role: ConnectionRole) -> list[dict[str, Any]]:
    calendars: dict[str, dict[str, Any]] = {}

    def _add(item: dict[str, Any], *, source: str) -> None:
        cal_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not cal_id or not name:
            return
        calendars[cal_id] = {
            "id": cal_id,
            "name": name,
            "can_edit": item.get("canEdit", item.get("can_edit")),
            "owner": item.get("owner"),
            "source": source,
        }

    try:
        primary = _execute_calendar_tool("OUTLOOK_LIST_CALENDARS", {"user_id": "me"}, role=role)
        for item in (_coerce_data(primary.get("data")).get("value") or []):
            if isinstance(item, dict):
                _add(item, source="primary")
    except Exception as exc:
        logger.warning("OUTLOOK_LIST_CALENDARS failed (%s): %s", role, exc)

    try:
        groups = _execute_calendar_tool("OUTLOOK_LIST_CALENDAR_GROUPS", {"user_id": "me"}, role=role)
        for group in (_coerce_data(groups.get("data")).get("value") or []):
            if not isinstance(group, dict):
                continue
            group_id = group.get("id")
            if not group_id:
                continue
            try:
                nested = _execute_calendar_tool(
                    "OUTLOOK_LIST_CALENDAR_GROUP_CALENDARS",
                    {"user_id": "me", "calendar_group_id": group_id},
                    role=role,
                )
                for item in (_coerce_data(nested.get("data")).get("value") or []):
                    if isinstance(item, dict):
                        _add(item, source=f"group:{group.get('name', '')}")
            except Exception as exc:
                logger.debug("Group calendar list failed for %s: %s", group_id, exc)
    except Exception as exc:
        logger.warning("OUTLOOK_LIST_CALENDAR_GROUPS failed (%s): %s", role, exc)

    return sorted(calendars.values(), key=lambda c: c["name"].lower())


def _normalize_name(value: str) -> str:
    value = value.strip().lower().replace("'", "'").replace("&", " and ")
    value = value.replace("calender", "calendar")
    value = re.sub(r"[^\w\s'-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_GENERIC_CALENDAR_NAMES = frozenset({"calendar", "default"})


def _names_match(target_norm: str, calendar_norm: str) -> bool:
    if target_norm == calendar_norm:
        return True
    # Avoid matching generic "Calendar" to "Kory Master Calendar (ALL)" via substring.
    if target_norm in _GENERIC_CALENDAR_NAMES or calendar_norm in _GENERIC_CALENDAR_NAMES:
        return target_norm == calendar_norm
    if target_norm in calendar_norm or calendar_norm in target_norm:
        return True
    # Deal Activity / Deal Activities, etc.
    if target_norm.rstrip("s") == calendar_norm.rstrip("s"):
        return True
    return False


def resolve_calendar_name(
    calendar_name: str,
    *,
    role: ConnectionRole = "write",
) -> dict[str, Any] | None:
    """Resolve display name or alias to a calendar record."""
    if not calendar_name.strip():
        return None

    cache_key = (role, calendar_name.strip().lower())
    now = time.monotonic()
    cached = _resolve_calendar_cache.get(cache_key)
    if cached and now - cached[0] < _LIST_ALL_CALENDARS_TTL_SEC:
        return cached[1]

    config = _load_calendar_config()
    aliases = config.get("aliases") or {}
    key = calendar_name.strip().lower().replace(" ", "_")
    target_name = aliases.get(key) or calendar_name.strip()
    target_norm = _normalize_name(target_name)

    for cal in list_all_calendars(role=role):
        cal_norm = _normalize_name(cal["name"])
        if _names_match(target_norm, cal_norm):
            _resolve_calendar_cache[cache_key] = (now, cal)
            return cal
    _resolve_calendar_cache[cache_key] = (now, None)
    return None


def primary_calendar_name() -> str:
    """Kory's primary write calendar (work Calendar)."""
    config = _load_calendar_config()
    return str(
        config.get("work_calendar")
        or config.get("primary_calendar")
        or config.get("default_write")
        or "Calendar"
    )


def work_calendar_name() -> str:
    config = _load_calendar_config()
    return str(config.get("work_calendar") or config.get("default_write") or "Calendar")


def personal_calendar_name() -> str:
    config = _load_calendar_config()
    return str(config.get("personal_calendar") or "Kory Master Calendar (ALL)")


def resolve_write_calendar_for_intent(intent: str | None) -> str:
    """Map scheduling intent to Outlook calendar display name."""
    config = _load_calendar_config()
    intent_key = (intent or "unknown").lower().replace(" ", "_")
    by_intent = config.get("write_by_intent") or {}
    if intent_key in by_intent:
        return str(by_intent[intent_key])
    return default_write_calendar_name()


def default_write_calendar_name() -> str:
    config = _load_calendar_config()
    return str(config.get("default_write") or work_calendar_name())


def _is_master_calendar_name(name: str) -> bool:
    lowered = (name or "").strip().lower()
    master = personal_calendar_name().strip().lower()
    return lowered == master or ("master" in lowered and "calendar" in lowered)


def _coerce_write_target(name: str) -> str:
    """Never write to Master — redirect any Master-targeted write to the work calendar."""
    if _is_master_calendar_name(name):
        work = work_calendar_name()
        logger.warning(
            "Write to Master calendar %r blocked by policy — redirecting to work calendar %r.",
            name,
            work,
        )
        return work
    return name


def conflict_calendar_names() -> list[str]:
    config = _load_calendar_config()
    names = config.get("default_read_for_conflicts") or []
    return [str(n) for n in names if n]


def get_calendar_events_by_name(
    calendar_name: str,
    start_iso: str,
    end_iso: str,
    *,
    role: ConnectionRole = "read",
    resolved: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch events from a named calendar."""
    cal = resolved or resolve_calendar_name(calendar_name, role=role)
    if not cal:
        return [], None
    return get_calendar_events_for_resolved(cal, start_iso, end_iso, role=role)


def get_calendar_events_for_resolved(
    resolved: dict[str, Any],
    start_iso: str,
    end_iso: str,
    *,
    role: ConnectionRole = "read",
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch events when the calendar record is already resolved."""
    start = _convert_iso_timezone(start_iso, SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE)
    end = _convert_iso_timezone(end_iso, SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE)

    for tool, extra in (
        ("OUTLOOK_LIST_USER_CALENDAR_VIEW", {"calendar_id": resolved["id"]}),
        ("OUTLOOK_GET_CALENDAR_VIEW", {"calendar_id": resolved["id"]}),
    ):
        try:
            result = _execute_calendar_tool(
                tool,
                {
                    "user_id": "me",
                    "start_datetime": start,
                    "end_datetime": end,
                    "timezone": OUTLOOK_TIMEZONE,
                    "top": 250,
                    **extra,
                },
                role=role,
            )
            data = _coerce_data(result.get("data"))
            events = data.get("value") or data.get("events") or []
            if isinstance(events, list):
                normalized = _events_to_scheduling_timezone(events)
                for event in normalized:
                    event["calendar_name"] = resolved["name"]
                return normalized, result.get("log_id")
        except Exception:
            continue
    return [], None


def get_merged_conflict_events(start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Raw busy blocks from configured Outlook calendars (pre-intelligence)."""
    merged: dict[str, dict[str, Any]] = {}
    for name in conflict_calendar_names():
        resolved = resolve_calendar_name(name, role="read")
        if not resolved:
            logger.debug("Conflict calendar not on account (skipped): %s", name)
            continue
        events, _ = get_calendar_events_by_name(name, start_iso, end_iso, role="read")
        for event in events:
            if not is_blocking_event(event):
                continue
            event_id = str(event.get("id") or f"{name}-{event.get('subject')}")
            event["calendar_name"] = resolved["name"]
            merged[event_id] = event
    return list(merged.values())


def fetch_events_chunked(
    start_iso: str,
    end_iso: str,
    *,
    chunk_days: int = 14,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read named conflict calendars in chunks (Graph top=250 safety)."""
    # Keep the window timezone-AWARE. Stripping tzinfo left a naive value that
    # still held UTC numbers, which get_calendar_events_for_resolved then
    # re-interpreted as Denver (via _convert_iso_timezone's naive->Denver
    # assumption), shifting the whole busy window +6-7h — so the conflict
    # calendars were blind to the next several hours (audit 2026-08-15, B3).
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    resolved_cals: list[dict[str, Any]] = []
    for name in conflict_calendar_names():
        resolved = resolve_calendar_name(name, role="read")
        if resolved:
            resolved_cals.append(resolved)

    merged: dict[str, dict[str, Any]] = {}
    chunks = 0
    cursor = start_dt
    while cursor < end_dt:
        chunk_end = min(cursor + timedelta(days=chunk_days), end_dt)
        for resolved in resolved_cals:
            events, _ = get_calendar_events_for_resolved(
                resolved,
                cursor.isoformat(),
                chunk_end.isoformat(),
                role="read",
            )
            for event in events:
                if not is_blocking_event(event):
                    continue
                name = str(resolved.get("name") or "")
                event_id = str(event.get("id") or f"{name}-{event.get('subject')}-{chunks}")
                event["calendar_name"] = resolved["name"]
                merged[event_id] = event
        chunks += 1
        cursor = chunk_end

    return list(merged.values()), {"chunks": chunks, "chunk_days": chunk_days}


def add_conflict_calendar(calendar_name: str) -> dict[str, Any]:
    """Add a calendar to default_read_for_conflicts after verifying it exists on Kory's account."""
    name = calendar_name.strip()
    if not name:
        return {"ok": False, "error": "calendar_name is required."}

    resolved = resolve_calendar_name(name, role="read")
    if not resolved:
        available = [c["name"] for c in list_all_calendars(role="read")]
        return {
            "ok": False,
            "error": (
                f"Calendar '{name}' not found on Kory's Outlook. "
                "Check the name in Outlook or call lexi_list_calendars."
            ),
            "available_calendars": available,
        }

    actual = resolved["name"]
    config = _load_calendar_config()
    names = [str(n) for n in (config.get("default_read_for_conflicts") or []) if n]
    if any(_names_match(_normalize_name(actual), _normalize_name(n)) for n in names):
        return {
            "ok": True,
            "already_configured": True,
            "calendar": actual,
            "calendars_consulted": calendars_consulted_for_conflicts(),
        }

    names.append(actual)
    config["default_read_for_conflicts"] = names
    _save_calendar_config(config)
    logger.info("Added conflict calendar to config: %s", actual)
    return {
        "ok": True,
        "already_configured": False,
        "calendar": actual,
        "calendars_consulted": calendars_consulted_for_conflicts(),
        "message": f"Added '{actual}' to conflict calendars. Lexi will include it on the next availability read.",
    }


def _save_calendar_config(config: dict[str, Any]) -> None:
    CALENDARS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CALENDARS_CONFIG_PATH.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)


def calendars_consulted_for_conflicts() -> list[dict[str, Any]]:
    """Outlook calendars Lexi checks for busy/free (resolved on Kory's account)."""
    primary = primary_calendar_name()
    primary_norm = _normalize_name(primary)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for name in conflict_calendar_names():
        resolved = resolve_calendar_name(name, role="read")
        is_primary = _names_match(primary_norm, _normalize_name(name))
        if not resolved:
            result.append(
                {
                    "configured_name": name,
                    "resolved": False,
                    "reason": "not_on_account",
                    "primary": is_primary,
                }
            )
            continue
        cal_id = resolved["id"]
        if cal_id in seen:
            continue
        seen.add(cal_id)
        result.append(
            {
                "configured_name": name,
                "resolved": True,
                "name": resolved["name"],
                "id": cal_id,
                "can_edit": resolved.get("can_edit"),
                "primary": is_primary
                or _names_match(primary_norm, _normalize_name(resolved["name"])),
            }
        )
    return result


def create_event_on_calendar(
    calendar_action: dict[str, Any],
    *,
    calendar_name: str | None = None,
    role: ConnectionRole = "write",
) -> tuple[str | None, str | None]:
    """Create event on named calendar; falls back to default write calendar.

    Policy: Lexi only writes meetings/holds to the work calendar (they sync to
    Master). Master is read-only conflict truth — any write that would target it
    is coerced to the work calendar.
    """
    from app.integrations.outlook_calendar import create_calendar_event

    target = (calendar_name or "").strip() or default_write_calendar_name()
    target = _coerce_write_target(target)
    resolved = resolve_calendar_name(target, role=role)

    if not resolved:
        logger.warning("Calendar %r not found — using default OUTLOOK_CREATE_ME_EVENT", target)
        return create_calendar_event(calendar_action)

    start = _convert_iso_timezone(calendar_action["start"], SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE)
    end = _convert_iso_timezone(calendar_action["end"], SCHEDULING_TIMEZONE, OUTLOOK_TIMEZONE)
    attendees = [
        {"emailAddress": {"address": email}, "type": "required"}
        for email in calendar_action.get("attendees", [])
    ]

    location = calendar_action.get("location", "Microsoft Teams")
    is_online = calendar_action.get("is_online_meeting")
    if is_online is None:
        is_online = str(location).lower() in {"teams", "microsoft teams", "zoom"}
    payload: dict[str, Any] = {
        "user_id": "me",
        "calendar_id": resolved["id"],
        "subject": calendar_action.get("title", "Meeting with Kory"),
        "start": {"dateTime": start, "timeZone": OUTLOOK_TIMEZONE},
        "end": {"dateTime": end, "timeZone": OUTLOOK_TIMEZONE},
        "location": {"displayName": location},
        "attendees": attendees,
        "body": {
            "contentType": "text",
            "content": calendar_action.get("body") or "Created by Lexi.",
        },
        "isOnlineMeeting": bool(is_online),
    }
    if is_online:
        payload["onlineMeetingProvider"] = "teamsForBusiness"

    if settings.lexi_dry_run:
        return create_calendar_event(calendar_action)

    result = _execute_calendar_tool(
        "OUTLOOK_CREATE_CALENDAR_EVENT_IN_CALENDAR",
        payload,
        role=role,
    )
    # Lexi's own write must be visible to her next slot search. The other create
    # paths (outlook_calendar.create_calendar_event) invalidate, but this — the
    # production path holds and invites actually take — returned without it, so a
    # slot she just held stayed "free" in the 30-min context cache and could be
    # offered again to a second counterpart (audit 2026-08-15, B2).
    from app.integrations.outlook_calendar import _invalidate_scheduling_cache

    _invalidate_scheduling_cache()
    data = _coerce_data(result.get("data"))
    event_id = data.get("id")
    if not event_id and isinstance(data.get("event"), dict):
        event_id = data["event"].get("id")
    return event_id, result.get("log_id")
