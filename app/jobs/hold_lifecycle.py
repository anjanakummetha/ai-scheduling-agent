"""Hold reminder, expiry release, and Friday cleanup for offered slots."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.integrations.outlook_calendar import delete_calendar_event
from app.storage.lexi_db import get_lexi_connection

import rules as kory_rules

logger = logging.getLogger(__name__)

# Statuses whose holds age out. pending_invite is deliberately NOT here: the
# prospect already picked a slot, and releasing its hold while Kory decides
# leaves the chosen time unprotected (and the "no reply" notice would be
# false). Those holds are settled at confirm time instead.
RELEASABLE_HOLD_STATUSES = ("pending_approval", "offer_sent")
RELEASED_STATUS = "released"
PENDING_APPROVAL = "pending_approval"


def run_hold_lifecycle_cycle() -> dict[str, Any]:
    """Release expired holds; stage hold reminders; optional Friday cleanup."""
    from app.scheduling.hold_reminder import process_due_hold_reminders

    reminders = process_due_hold_reminders()
    released = _release_expired_holds()
    friday = _friday_cleanup_next_week_holds()
    return {
        "hold_reminders_staged": len(reminders),
        "reminders": reminders,
        "released_expired": released,
        "friday_cleanup": friday,
    }


def _release_expired_holds() -> int:
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    released_proposals: set[int] = set()
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT h.id, h.proposal_id, h.event_id, h.slot_start, h.expires_at,
                   p.status AS proposal_status, p.intent_classification,
                   e.subject, e.sender
            FROM holds AS h
            INNER JOIN proposals AS p ON p.id = h.proposal_id
            LEFT JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE h.expires_at IS NOT NULL
              AND h.expires_at != ?
              AND h.expires_at <= ?
              AND p.status IN ({placeholders})
              AND h.event_id NOT LIKE 'hold-pending-%'
              AND COALESCE(h.event_id, '') != ''
            """.format(placeholders=",".join("?" * len(RELEASABLE_HOLD_STATUSES))),
            (RELEASED_STATUS, now, *RELEASABLE_HOLD_STATUSES),
        ).fetchall()

        for row in rows:
            event_id = str(row["event_id"] or "")
            if not event_id or event_id.startswith("dry-run-"):
                continue
            try:
                delete_calendar_event(event_id)
            except Exception as exc:
                logger.warning("Failed to delete expired hold event %s: %s", event_id, exc)

            conn.execute(
                "UPDATE holds SET expires_at = ? WHERE id = ?",
                (RELEASED_STATUS, row["id"]),
            )
            _audit(
                conn,
                proposal_id=row["proposal_id"],
                message=(
                    f"Released expired hold for proposal {row['proposal_id']} "
                    f"(slot {row['slot_start']})."
                ),
                payload={
                    "event_id": event_id,
                    "expires_at": row["expires_at"],
                    "subject": row["subject"],
                    "sender": row["sender"],
                    "held_days": _held_days_for_intent(row["intent_classification"]),
                },
            )
            count += 1
            released_proposals.add(int(row["proposal_id"]))
            _maybe_notify_hold_released(row)

        if count:
            conn.commit()

    # "Release the hold ... and re-remind them at the same time" (HOLD_RULES).
    # Stage the prospect follow-up draft for any offer that expired without a
    # reply — Kory approves the send, same as every other outbound.
    if released_proposals and kory_rules.HOLD_RULES.get("re_remind_on_release"):
        from app.scheduling.hold_reminder import stage_release_followups

        try:
            stage_release_followups(sorted(released_proposals))
        except Exception:
            logger.exception("Release follow-up staging failed.")
    return count


def _held_days_for_intent(intent: str | None) -> int:
    if (intent or "").lower() == "reschedule":
        return int(kory_rules.RESCHEDULE_RULES.get("reply_window_days", 1))
    return int(kory_rules.HOLD_RULES.get("release_hold_after_days", 3))


def _next_week_window_mt(now_mt: datetime) -> tuple[datetime, datetime]:
    """[Monday 00:00 MT of next week, the following Monday) — the "next calendar week"."""
    this_monday = (now_mt - timedelta(days=now_mt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start = this_monday + timedelta(days=7)
    return week_start, week_start + timedelta(days=7)


def _friday_cleanup_next_week_holds() -> int:
    """On Friday (Kory's Mountain Time), release pending holds that fall in next week.

    Keyed to Kory's home timezone, not UTC — a UTC-Friday check fires from ~5 PM
    Thursday MT and would release next-week holds a day early.
    """
    mt = ZoneInfo(settings.scheduling_timezone)
    now_mt = datetime.now(mt)
    if now_mt.weekday() != 4:  # Friday, Mountain Time
        return 0
    # "By END of every Friday" — sweeping at 00:01 Friday would strip a
    # Wednesday-offered hold barely a day into its window. 5 PM MT gives the
    # prospect the full business week to answer.
    if now_mt.hour < 17:
        return 0

    week_start, week_end = _next_week_window_mt(now_mt)
    count = 0

    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT h.id, h.proposal_id, h.event_id, h.slot_start
            FROM holds AS h
            INNER JOIN proposals AS p ON p.id = h.proposal_id
            WHERE p.status IN ({placeholders})
              AND h.expires_at != ?
              AND h.event_id NOT LIKE 'hold-pending-%'
              AND COALESCE(h.event_id, '') != ''
            """.format(placeholders=",".join("?" * len(RELEASABLE_HOLD_STATUSES))),
            (*RELEASABLE_HOLD_STATUSES, RELEASED_STATUS),
        ).fetchall()

        for row in rows:
            slot_start = _parse_iso(row["slot_start"])
            if not slot_start or not (week_start <= slot_start < week_end):
                continue
            event_id = str(row["event_id"] or "")
            try:
                delete_calendar_event(event_id)
            except Exception as exc:
                logger.warning("Friday cleanup failed for %s: %s", event_id, exc)
            conn.execute(
                "UPDATE holds SET expires_at = ? WHERE id = ?",
                (RELEASED_STATUS, row["id"]),
            )
            _audit(
                conn,
                proposal_id=row["proposal_id"],
                message="Friday cleanup released hold for next week.",
                payload={"event_id": event_id, "slot_start": row["slot_start"]},
            )
            count += 1

        if count:
            conn.commit()
    return count


def _maybe_notify_hold_released(row: Any) -> None:
    from app.safety.outbound_guard import teams_push_allowed

    if not teams_push_allowed():
        return
    try:
        from app.bot.teams_format import display_sender, display_subject
        from app.bot.teams_publisher import push_approval_text_to_teams
        import asyncio

        subject = display_subject(row["subject"] or "(no subject)")
        sender = display_sender(row["sender"] or "unknown")
        held_days = _held_days_for_intent(row["intent_classification"])
        day_word = "day" if held_days == 1 else "days"
        text = (
            f"**Lexi — hold released (no reply)**\n"
            f"**{subject}**\n"
            f"From {sender}\n"
            f"Slot: {row['slot_start']}\n\n"
            f"Held {held_days} {day_word} with no response — calendar hold removed. "
            f"Ask me to re-offer times for **{subject}** from {sender}."
        )
        coro = push_approval_text_to_teams(text, proposal_id=row["proposal_id"])
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
    except Exception as exc:
        logger.debug("Teams hold-release notify skipped: %s", exc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _audit(conn, *, proposal_id: int, message: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "hold_lifecycle",
            str(proposal_id),
            "INFO",
            message,
            json.dumps(payload, default=str),
        ),
    )
