"""24h Kory nudges and scheduled 4:45 AM MT CEO briefing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)

_REMINDER_HOURS = int(__import__("os").getenv("LEXI_KORY_REMINDER_HOURS", "24"))
_BRIEFING_HOUR = int(__import__("os").getenv("LEXI_DAILY_BRIEFING_HOUR_MT", "4"))
_BRIEFING_MINUTE = int(__import__("os").getenv("LEXI_DAILY_BRIEFING_MINUTE_MT", "45"))
_BRIEFING_WINDOW_MIN = int(__import__("os").getenv("LEXI_DAILY_BRIEFING_WINDOW_MIN", "20"))


def _mt_now() -> datetime:
    try:
        tz = ZoneInfo(settings.scheduling_timezone)
    except Exception:
        tz = ZoneInfo("America/Denver")
    return datetime.now(tz)


def run_kory_briefing_cycle() -> dict[str, Any]:
    """Called from orchestrator each cycle — idempotent 24h nudges.

    The 4:45 AM CEO briefing was removed: the dashboard owns the morning
    package, and two systems deriving one from the same mailbox disagreed.
    """
    reminders = process_kory_24h_reminders()
    return {
        "kory_24h_reminders": len(reminders),
        "reminders": reminders,
    }


def process_kory_24h_reminders() -> list[dict[str, Any]]:
    """Teams nudge when Kory hasn't acted on Lexi items for 24h+."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_REMINDER_HOURS)
    cutoff_sql = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    staged: list[dict[str, Any]] = []

    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id AS proposal_id, p.status, p.created_at, e.subject, e.sender
            FROM proposals p
            INNER JOIN email_threads e ON e.thread_id = p.thread_id
            WHERE p.status IN ('pending_approval', 'awaiting_reply_prompt')
              AND datetime(p.created_at) <= datetime(?)
            """,
            (cutoff_sql,),
        ).fetchall()

        for row in rows:
            proposal_id = int(row["proposal_id"])
            if _kory_reminder_already_sent(conn, proposal_id):
                continue
            staged.append(
                {
                    "proposal_id": proposal_id,
                    "status": row["status"],
                    "subject": row["subject"],
                    "sender": row["sender"],
                }
            )
            _audit_reminder(conn, proposal_id, row["status"])
            _notify_kory_24h_reminder(
                proposal_id=proposal_id,
                subject=str(row["subject"] or ""),
                sender=str(row["sender"] or ""),
                status=str(row["status"] or ""),
            )
        if staged:
            conn.commit()
    return staged



def _kory_reminder_already_sent(conn, proposal_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM audit_log
        WHERE step_name = 'kory_24h_reminder' AND reference_id = ?
        LIMIT 1
        """,
        (str(proposal_id),),
    ).fetchone()
    return row is not None


def _audit_reminder(conn, proposal_id: int, status: str) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "kory_24h_reminder",
            str(proposal_id),
            "INFO",
            f"24h Kory reminder staged for {status}",
            json.dumps({"status": status}),
        ),
    )




def _notify_kory_24h_reminder(
    *,
    proposal_id: int,
    subject: str,
    sender: str,
    status: str,
) -> None:
    from app.safety.outbound_guard import teams_push_allowed

    if not teams_push_allowed():
        logger.info("24h reminder not pushed (dry-run/suppressed/teams-off) proposal=%s", proposal_id)
        return
    try:
        from app.bot.teams_format import display_sender, display_subject
        from app.bot.teams_publisher import push_approval_text_to_teams
        import asyncio

        who = display_sender(sender)
        topic = display_subject(subject)
        action = "approve or discard the draft" if status == "pending_approval" else "say yes/no on drafting a reply"
        text = (
            f"**Lexi — 24h reminder**\n"
            f"**{topic}** from {who}\n\n"
            f"This has been waiting 24+ hours — {action}."
        )
        coro = push_approval_text_to_teams(text, proposal_id=proposal_id)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
    except Exception as exc:
        logger.debug("24h Teams notify skipped: %s", exc)


