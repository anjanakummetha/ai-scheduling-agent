"""Format Kory calendar events for chat summaries (read-only, tool-built text)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduling.busy_intervals import events_on_local_date, parse_event_datetime
from app.scheduling.scheduling_window import SchedulingWindow, infer_scheduling_window

MT = ZoneInfo(settings.scheduling_timezone)


def _hour_min(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    minute = dt.minute
    suffix = dt.strftime("%p")
    if minute:
        return f"{hour}:{minute:02d} {suffix}"
    return f"{hour} {suffix}"


def _format_time_range(start: datetime, end: datetime) -> str:
    start_mt = start.astimezone(MT)
    end_mt = end.astimezone(MT)
    if start_mt.date() != end_mt.date():
        return (
            f"{_hour_min(start_mt)}–"
            f"{end_mt.strftime('%b')} {end_mt.day}, {_hour_min(end_mt)} MT"
        )
    return f"{_hour_min(start_mt)}–{_hour_min(end_mt)} MT"


def _format_day_heading(day: date) -> str:
    return day.strftime("%A, %B ") + f"{day.day}, {day.year}"


def _event_sort_key(event: dict[str, Any]) -> tuple[datetime, str]:
    start = parse_event_datetime(event.get("start")) or datetime.min.replace(tzinfo=MT)
    return start, str(event.get("subject") or "")


def _format_event_line(event: dict[str, Any]) -> str:
    start = parse_event_datetime(event.get("start"))
    end = parse_event_datetime(event.get("end"))
    subject = str(event.get("subject") or "Busy").strip()
    if not start:
        return f"• {subject}"
    if not end or end <= start:
        end = start + timedelta(minutes=30)
    return f"• {_format_time_range(start, end)} — {subject}"


def build_calendar_window_summary(
    *,
    busy_events: list[dict[str, Any]],
    window: SchedulingWindow,
) -> dict[str, Any]:
    """Day-by-day summary from live busy events — no LLM date guessing."""
    days_out: list[dict[str, Any]] = []
    lines: list[str] = [
        f"Calendar ({window.label}): {_format_day_heading(window.start)} through "
        f"{_format_day_heading(window.end)} (Mountain Time).",
        "",
    ]

    cursor = window.start
    total_events = 0
    while cursor <= window.end:
        day_dt = datetime(cursor.year, cursor.month, cursor.day, tzinfo=MT)
        day_events = sorted(events_on_local_date(busy_events, day_dt), key=_event_sort_key)
        day_lines = [_format_event_line(event) for event in day_events]
        total_events += len(day_events)
        days_out.append(
            {
                "date": cursor.isoformat(),
                "heading": _format_day_heading(cursor),
                "event_count": len(day_events),
                "events": [
                    {
                        # Carried so a follow-up can act on an event by id —
                        # rescheduling needs one, and without it the summary was
                        # a dead end for anything past today. Structured field
                        # only; the formatted summary Kory reads stays clean.
                        "event_id": e.get("id"),
                        "subject": str(e.get("subject") or ""),
                        "start": e.get("start"),
                        "end": e.get("end"),
                        "calendar_name": e.get("calendar_name"),
                    }
                    for e in day_events
                ],
            }
        )
        lines.append(_format_day_heading(cursor))
        if day_lines:
            lines.extend(day_lines)
        else:
            lines.append("• No events found.")
        lines.append("")
        cursor += timedelta(days=1)

    formatted = "\n".join(lines).strip()
    return {
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "label": window.label,
            "source": window.source,
        },
        "days": days_out,
        "total_events": total_events,
        "formatted_summary": formatted,
        "sources": ["Kory Master Calendar (ALL)", "Calendar"],
    }


def infer_summary_window(*, query: str) -> SchedulingWindow | None:
    return infer_scheduling_window(body=query)
