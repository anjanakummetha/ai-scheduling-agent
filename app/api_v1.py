"""Read-only JSON API for the CEO dashboard's "Lexi Assistant" panel (dashboard F1).

A lean, token-authenticated, READ-ONLY view of the agent's state — every handler
is a SELECT against data/lexi.db (plus the Phase 3 cost ledgers). No write, send,
or mutation exists on this surface. Bound to 127.0.0.1 and never proxied publicly.

Enable with LEXI_API_ENABLED=true; auth via `Authorization: Bearer $LEXI_API_TOKEN`.
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from app.storage.lexi_db import get_lexi_connection

router = APIRouter(prefix="/api/v1")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _clean_email(value: object) -> str:
    """Extract a clean email address from a sender value that may be a stringified dict."""
    if not value:
        return "unknown"
    m = _EMAIL_RE.search(str(value))
    return m.group(0) if m else str(value)[:80]


def _require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("LEXI_API_TOKEN", "").strip()
    if not expected:
        # Fail closed: if no token is configured, the API is unusable rather than open.
        raise HTTPException(status_code=503, detail="LEXI_API_TOKEN not configured")
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/health")
def health() -> dict[str, Any]:
    from app.storage.heartbeat import heartbeat_age_seconds

    db_ok = True
    try:
        with get_lexi_connection() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False
    age = heartbeat_age_seconds()
    return {
        "status": "ok" if (db_ok and (age is None or age <= 300)) else "degraded",
        "db_ok": db_ok,
        "worker_heartbeat_age_seconds": round(age, 1) if age is not None else None,
        "worker_heartbeat_stale": age is not None and age > 300,
    }


@router.get("/pending-approvals", dependencies=[Depends(_require_token)])
def pending_approvals() -> dict[str, Any]:
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.status, p.intent_classification, p.proposed_slots,
                   p.created_at, t.subject, t.sender
            FROM proposals AS p
            LEFT JOIN email_threads AS t ON t.thread_id = p.thread_id
            WHERE p.status IN ('pending_approval', 'awaiting_reply_prompt', 'needs_kory')
            ORDER BY p.created_at DESC
            LIMIT 50
            """
        ).fetchall()
    import json

    items = []
    for r in rows:
        try:
            slots = json.loads(r["proposed_slots"]) if r["proposed_slots"] else []
        except (TypeError, ValueError):
            slots = []
        items.append(
            {
                "id": r["id"],
                "status": r["status"],
                "subject": r["subject"] or "(no subject)",
                "requester": _clean_email(r["sender"]),
                "intent": r["intent_classification"],
                "proposed_slots": slots if isinstance(slots, list) else [],
                "created_at": r["created_at"],
            }
        )
    return {"count": len(items), "items": items}


@router.get("/unanswered-scheduling", dependencies=[Depends(_require_token)])
def unanswered_scheduling(min_hours: int = 24, max_days: int = 7) -> dict[str, Any]:
    """Scheduling asks staged but still unactioned — one batched line for the brief.

    Teams is reserved for decisions only Kory can make, so a cold scheduling
    email no longer raises a card. This is the counterweight: the same asks,
    aged and batched into the morning briefing rather than interrupting.

    `awaiting_reply_prompt` is the staged-and-waiting state — a proposal Lexi
    parked because nobody delegated it to her.

    Oldest first, since those have waited longest, but bounded by `max_days`:
    without that bound a daily brief would re-surface the same long-dead asks
    every morning and the section would stop being read.
    """
    min_hours = max(1, min(int(min_hours), 24 * 30))
    max_days = max(1, min(int(max_days), 90))
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.status, p.intent_classification, p.priority_tier,
                   p.created_at, t.subject, t.sender,
                   CAST((julianday('now') - julianday(p.created_at)) * 24 AS INTEGER) AS age_hours
            FROM proposals AS p
            LEFT JOIN email_threads AS t ON t.thread_id = p.thread_id
            WHERE p.status = 'awaiting_reply_prompt'
              AND p.created_at <= datetime('now', ?)
              AND p.created_at >= datetime('now', ?)
            ORDER BY p.created_at ASC
            LIMIT 25
            """,
            (f"-{min_hours} hours", f"-{max_days} days"),
        ).fetchall()

    items = [
        {
            "id": r["id"],
            "subject": r["subject"] or "(no subject)",
            "requester": _clean_email(r["sender"]),
            "intent": r["intent_classification"],
            "priority": r["priority_tier"],
            "age_hours": r["age_hours"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"count": len(items), "min_hours": min_hours, "items": items}


@router.get("/holds", dependencies=[Depends(_require_token)])
def holds() -> dict[str, Any]:
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT h.proposal_id, h.event_id, h.slot_start, h.slot_end, h.expires_at,
                   t.subject
            FROM holds AS h
            JOIN proposals AS p ON p.id = h.proposal_id
            LEFT JOIN email_threads AS t ON t.thread_id = p.thread_id
            WHERE COALESCE(h.expires_at, '') NOT IN ('released', '')
              AND COALESCE(h.event_id, '') NOT LIKE 'hold-pending-%'
            ORDER BY h.slot_start ASC
            LIMIT 50
            """
        ).fetchall()
    active = [
        {
            "proposal_id": r["proposal_id"],
            "title": r["subject"] or "Hold",
            "slot_start": r["slot_start"],
            "slot_end": r["slot_end"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]
    return {"count": len(active), "active": active}


@router.get("/audit", dependencies=[Depends(_require_token)])
def audit(limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, step_name, log_level, message
            FROM audit_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "items": [
            {
                "timestamp": r["timestamp"],
                "step": r["step_name"],
                "level": r["log_level"],
                "message": r["message"],
            }
            for r in rows
        ]
    }


@router.get("/briefing", dependencies=[Depends(_require_token)])
def briefing() -> dict[str, Any]:
    # Latest daily-briefing delivery record (read-only; does not regenerate).
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT timestamp, message FROM audit_log
            WHERE step_name LIKE '%briefing%'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    if not row:
        return {"available": False}
    return {"available": True, "delivered_at": row["timestamp"], "summary": row["message"]}


@router.get("/costs", dependencies=[Depends(_require_token)])
def costs() -> dict[str, Any]:
    out: dict[str, Any] = {"llm": None, "composio": None}
    try:
        from app.storage.llm_cost_log import cost_rollup

        month = cost_rollup(days=30)
        today = cost_rollup(days=1)
        out["llm"] = {
            "today_usd": today.get("est_usd"),
            "month_usd": month.get("est_usd"),
            "cache_hit_ratio": month.get("cache_hit_ratio"),
        }
    except Exception:
        pass
    try:
        from app.storage.composio_call_log import budget_status

        b = budget_status()
        out["composio"] = {
            "today_calls": b.get("today"),
            "month_calls": b.get("month_to_date"),
            "monthly_budget": b.get("monthly_budget"),
            "fraction_used": b.get("fraction_used"),
        }
    except Exception:
        pass
    return out
