"""Detect proposals stranded in a non-terminal state.

Audit 2026-08-15 (C1): pending_invite / pending_reoffer / needs_kory /
needs_scheduling_guidance have no sweeper — if Kory misses the one prompt, the
held slots block his calendar forever, the counterpart waits on an invite that
never comes, and nothing reports it. This is the missing detector.

Deliberately conservative so it can't re-introduce the notification-spam class
the notification fixes closed: it fires only after AGE_THRESHOLD_HOURS, at most
once per proposal per RENUDGE_DAYS (deduped via an audit marker), and only when
Teams pushes are allowed. Off-switch: LEXI_STUCK_PROPOSAL_SWEEP=false.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)

# Non-terminal states where Kory (or the counterpart) still owes an action and
# holds/threads can strand. Terminal states (executed, rejected, cancelled,
# no_reply_needed, offer_sent) are excluded — offer_sent is handled by the
# hold-reminder / expiry path, not here.
_STUCK_STATUSES = (
    "pending_invite",
    "pending_reoffer",
    "needs_kory",
    "needs_scheduling_guidance",
    "pending_approval",
    "awaiting_reply_prompt",
)

_AGE_THRESHOLD_HOURS = int(os.getenv("LEXI_STUCK_PROPOSAL_HOURS", "48"))
_RENUDGE_DAYS = int(os.getenv("LEXI_STUCK_PROPOSAL_RENUDGE_DAYS", "3"))
_NUDGE_STEP = "stuck_proposal_nudged"


def _enabled() -> bool:
    return os.getenv("LEXI_STUCK_PROPOSAL_SWEEP", "true").strip().lower() in {"1", "true", "yes"}


def _status_hint(status: str, proposal_id: int) -> str:
    if status == "pending_invite":
        return f"the guest picked a time — say **send invite #{proposal_id}** or **show #{proposal_id}**"
    if status == "pending_reoffer":
        return f"needs fresh times — say **retry scheduling #{proposal_id}** or drop it"
    if status in {"needs_kory", "needs_scheduling_guidance"}:
        return f"waiting on your guidance — reply here, or **reject #{proposal_id} — reason** to drop it"
    if status == "pending_approval":
        return f"a draft is waiting — **show draft #{proposal_id}**, then **approve #{proposal_id}** or **reject #{proposal_id}**"
    return f"a new ask is waiting — **show #{proposal_id}**"


def sweep_stuck_proposals() -> list[dict[str, Any]]:
    """One Teams nudge per aged, non-terminal, not-recently-nudged proposal."""
    if not _enabled():
        return []
    from app.safety.outbound_guard import teams_push_allowed

    if not teams_push_allowed():
        return []

    placeholders = ",".join("?" * len(_STUCK_STATUSES))
    with get_lexi_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT p.id AS proposal_id, p.status, e.subject, e.sender,
                   ROUND(julianday('now') - julianday(p.updated_at), 1) AS age_days
            FROM proposals AS p
            LEFT JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status IN ({placeholders})
              AND julianday('now') - julianday(p.updated_at) >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM audit_log a
                  WHERE a.step_name = ?
                    AND a.reference_id = CAST(p.id AS TEXT)
                    AND julianday('now') - julianday(a.timestamp) < ?
              )
            ORDER BY p.updated_at ASC
            LIMIT 20
            """,
            (
                *_STUCK_STATUSES,
                _AGE_THRESHOLD_HOURS / 24.0,
                _NUDGE_STEP,
                _RENUDGE_DAYS,
            ),
        ).fetchall()

        nudged: list[dict[str, Any]] = []
        for row in rows:
            pid = int(row["proposal_id"])
            status = str(row["status"] or "")
            age = row["age_days"]
            _notify_stuck(pid, status=status, subject=row["subject"], sender=row["sender"], age_days=age)
            conn.execute(
                """
                INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
                VALUES (?, ?, 'INFO', ?, '{}')
                """,
                (_NUDGE_STEP, str(pid), f"Stuck-proposal nudge for {pid} (status={status}, {age}d)."),
            )
            nudged.append({"proposal_id": pid, "status": status, "age_days": age})
        if nudged:
            conn.commit()
    return nudged


def _notify_stuck(
    proposal_id: int, *, status: str, subject: Any, sender: Any, age_days: Any
) -> None:
    try:
        import asyncio

        from app.bot.teams_format import display_sender, display_subject
        from app.bot.teams_publisher import push_approval_text_to_teams

        subj = display_subject(subject or "(no subject)")
        who = display_sender(sender or "unknown")
        text = (
            f"**Lexi — still waiting on you ({age_days}d)**\n"
            f"**{subj}** · from {who}\n"
            f"{_status_hint(status, proposal_id)}."
        )
        coro = push_approval_text_to_teams(text, proposal_id=proposal_id)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001 — a nudge must never break the cycle
        logger.debug("Stuck-proposal nudge skipped for %s: %s", proposal_id, exc)
