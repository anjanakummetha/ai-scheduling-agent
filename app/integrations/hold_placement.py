"""Unified calendar hold placement for all offer paths (inbound + outbound)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.integrations.calendar_holds import place_tentative_hold
from app.scheduling.calendar_intelligence import resolve_write_calendar_name
from app.scheduling.invite_builder import build_hold_action


class HoldPlacementError(RuntimeError):
    """Raised when one or more offered slots could not be held."""


def _same_instant(graph_time: object, slot_iso: str) -> bool:
    """Compare a Graph event time ({dateTime, timeZone} or ISO string) to a slot ISO."""
    from zoneinfo import ZoneInfo

    try:
        if isinstance(graph_time, dict):
            naive = datetime.fromisoformat(str(graph_time.get("dateTime") or "")[:19])
            aware = naive.replace(tzinfo=ZoneInfo(str(graph_time.get("timeZone") or "UTC")))
        else:
            aware = datetime.fromisoformat(str(graph_time))
        return aware == datetime.fromisoformat(slot_iso)
    except Exception:  # noqa: BLE001 — a malformed time is simply not a match
        return False


def _find_own_orphan_hold(
    action: dict[str, object],
    conflicts: list[dict[str, object]],
    *,
    start: str,
    end: str,
) -> str | None:
    """Recognize this hold's own orphaned calendar event as a non-conflict.

    A partial earlier run can create the HOLD event and then die before the DB
    row lands (e.g. a database lock). The retry then sees that event as a
    conflict and can never finish. If EVERY conflicting event is exactly this
    hold (same HOLD: title, same interval), return its event id to adopt.
    """
    title = str(action.get("title") or "").strip()
    if not title.upper().startswith("HOLD:"):
        title = f"HOLD: {title}"
    adopted: str | None = None
    for event in conflicts:
        if not isinstance(event, dict):
            return None
        if str(event.get("subject") or "").strip() != title:
            return None
        if not _same_instant(event.get("start"), start) or not _same_instant(
            event.get("end"), end
        ):
            return None
        adopted = str(event.get("id") or "") or adopted
    return adopted


def hold_expires_at(intent_classification: str | None) -> str:
    intent = (intent_classification or "").lower()
    days = 1 if intent == "reschedule" else 3
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def place_offered_holds(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    slots: list[dict[str, str]],
    intent_classification: str | None,
    meeting_subject: str | None = None,
    calendar_name: str | None = None,
    sender: str | None = None,
    body: str = "",
) -> int:
    """Insert hold rows and create Outlook holds — every slot must succeed."""
    if not slots:
        return 0

    target_calendar = (calendar_name or "").strip() or resolve_write_calendar_name(
        intent=intent_classification
    )
    expires_at = hold_expires_at(intent_classification)
    # Per-slot resume: a partial earlier run may have landed some hold rows
    # before failing. Skip those slots so a retry completes the set instead of
    # bailing out (or double-holding).
    already_held = {
        str(row[0])
        for row in conn.execute(
            "SELECT slot_start FROM holds WHERE proposal_id = ?", (proposal_id,)
        )
    }
    placed = 0
    failures: list[str] = []

    for index, slot in enumerate(slots, start=1):
        start = str(slot.get("start") or "").strip()
        end = str(slot.get("end") or "").strip()
        if not start or not end:
            failures.append(f"option {index}: missing start/end")
            continue
        if start in already_held:
            placed += 1
            continue

        action = build_hold_action(
            slot={"start": start, "end": end},
            meeting_subject=meeting_subject,
            intent=intent_classification,
            option_index=index,
            sender=sender,
            body=body,
        )
        event_id = f"hold-pending-{proposal_id}-{index:02d}-{uuid.uuid4().hex[:8]}"

        if not settings.lexi_dry_run:
            hold_result = place_tentative_hold(action=action, calendar_name=target_calendar)
            if hold_result.get("ok") and hold_result.get("event_id"):
                event_id = str(hold_result["event_id"])
            else:
                reason = hold_result.get("error") or "unknown"
                conflicts = hold_result.get("conflicting_events") or []
                orphan_id = (
                    _find_own_orphan_hold(action, conflicts, start=start, end=end)
                    if reason == "conflict"
                    else None
                )
                if orphan_id:
                    event_id = orphan_id
                else:
                    detail = f"option {index} ({start}): {reason}"
                    if conflicts:
                        detail += f" — conflicts: {conflicts[:2]}"
                    failures.append(detail)
                    continue

        conn.execute(
            """
            INSERT INTO holds (proposal_id, event_id, slot_start, slot_end, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (proposal_id, event_id, start, end, expires_at),
        )
        placed += 1

    if failures or placed != len(slots):
        raise HoldPlacementError(
            f"Could only place {placed}/{len(slots)} hold(s): " + "; ".join(failures)
        )
    return placed
