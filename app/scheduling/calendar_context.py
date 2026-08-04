"""Load merged, intelligence-filtered calendar context for scheduling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.integrations.family_google_calendar import get_family_busy_events
from app.integrations.named_calendars import (
    calendars_consulted_for_conflicts,
    fetch_events_chunked,
    list_all_calendars,
)
from app.integrations.outlook_calendar import get_calendar_events, is_blocking_event
from app.scheduling.calendar_intelligence import (
    dedupe_and_filter_blocking_events,
    resolve_calendar_horizon_days,
    summarize_blocking_events,
)

logger = logging.getLogger(__name__)

_CONTEXT_CACHE_TTL_SEC = 300.0
_context_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_scheduling_calendar_context_cache() -> None:
    """Drop cached calendar context (tests / forced refresh)."""
    _context_cache.clear()


# The busy picture depends ONLY on the horizon — the email text merely decides
# how many days to fetch. Keying the cache by subject/body hash gave it a
# near-zero hit rate: every distinct email re-fetched identical calendar data
# with ~70 sequential Composio calls (40-90s per scheduling run, live-measured).
# Horizons are bucketed so different asks share one fetch; fetching more days
# than asked is harmless — the engine filters by window.
_HORIZON_BUCKETS = (45, 90, 120)


def _bucket_horizon(days: int) -> int:
    for bucket in _HORIZON_BUCKETS:
        if days <= bucket:
            return bucket
    return days


def _context_cache_key(*, days: int) -> str:
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return f"{days}:{hour}"


def load_scheduling_calendar_context(
    *,
    subject: str = "",
    body: str = "",
    horizon_days: int | None = None,
) -> dict[str, Any]:
    """Full busy picture for auto-scheduler and availability tools."""
    days = _bucket_horizon(
        resolve_calendar_horizon_days(
            subject=subject,
            body=body,
            explicit_days=horizon_days,
        )
    )
    cache_key = _context_cache_key(days=days)
    now = time.monotonic()
    # A cached LARGER horizon is a superset of this request — serve it rather
    # than fetching a smaller window from scratch (the engine filters by
    # window, so extra days are harmless).
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    for cached_days in sorted({*_HORIZON_BUCKETS, days}):
        if cached_days < days:
            continue
        cached = _context_cache.get(f"{cached_days}:{hour}")
        if cached and now - cached[0] < _CONTEXT_CACHE_TTL_SEC:
            return cached[1]

    context = _load_scheduling_calendar_context_uncached(
        subject=subject,
        body=body,
        days=days,
    )
    _context_cache[cache_key] = (now, context)
    return context


def _load_scheduling_calendar_context_uncached(
    *,
    subject: str,
    body: str,
    days: int,
) -> dict[str, Any]:
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    try:
        raw_events: list[dict[str, Any]] = []

        named_events, named_meta = fetch_events_chunked(start_iso, end_iso)
        raw_events.extend(named_events)

        from app.integrations.named_calendars import conflict_calendar_names

        reads_master = any("master calendar" in n.lower() for n in conflict_calendar_names())
        log_id = named_meta.get("log_id")
        if not reads_master:
            primary_events, primary_log = get_calendar_events(start_iso, end_iso)
            log_id = log_id or primary_log
            for event in primary_events:
                if is_blocking_event(event):
                    event = dict(event)
                    event["calendar_name"] = event.get("calendar_name") or "Calendar"
                    event["source"] = "primary_calendar"
                    raw_events.append(event)

        for event in get_family_busy_events(start_iso, end_iso):
            event = dict(event)
            event["calendar_name"] = event.get("calendar_name") or "Family Google"
            event["source"] = "family_calendar"
            raw_events.append(event)

        blocking, classification_audit = dedupe_and_filter_blocking_events(raw_events)
        unavailable = _unavailable_configured_calendars()

        return {
            "status": "available",
            "source": "composio",
            "horizon_days": days,
            "range_start": start_iso,
            "range_end": end_iso,
            "busy_events": blocking,
            "busy_summary": summarize_blocking_events(blocking),
            "classification_skipped": len(classification_audit) - len(blocking),
            "calendars_consulted": calendars_consulted_for_conflicts(),
            "calendars_visible": [c["name"] for c in list_all_calendars(role="read")],
            "calendars_unavailable": unavailable,
            "composio_log_id": log_id,
            "named_read_meta": named_meta,
            "scheduling_timezone": settings.scheduling_timezone,
        }
    except Exception as exc:
        logger.exception("Calendar context load failed: %s", exc)
        return {
            "status": "unavailable",
            "source": "error",
            "horizon_days": days,
            "range_start": start_iso,
            "range_end": end_iso,
            "busy_events": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _unavailable_configured_calendars() -> list[dict[str, str]]:
    """Group/shared calendars configured but not on Composio read connection."""
    from app.integrations.named_calendars import _load_calendar_config

    config = _load_calendar_config()
    optional = [str(n) for n in (config.get("optional_group_calendars") or []) if n]
    visible = {c["name"].lower() for c in list_all_calendars(role="read")}
    missing: list[dict[str, str]] = []
    for name in optional:
        if not any(name.lower() in v or v in name.lower() for v in visible):
            missing.append(
                {
                    "configured_name": name,
                    "reason": "not_on_composio_account",
                    "hint": (
                        "Subscribe this calendar in Outlook for kory.mitchell@iconicfounders.com "
                        "or add to Kory Master rollup so Lexi sees blocks via Master/Calendar read."
                    ),
                }
            )
    return missing
