"""Composio call counter (plan Phase 3) — guards the 200k calls/month budget.

Daily-bucket counter (one row per day) so the row count stays tiny. The
orchestrator/status surface can alarm when the month is tracking over budget.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from app.config import settings
from app.storage.lexi_db import get_lexi_connection

# 200k/month ≈ 6,600/day. Alarm at 80%.
_MONTHLY_BUDGET = int(os.getenv("LEXI_COMPOSIO_MONTHLY_BUDGET", "200000") or 200000)
_ALARM_FRACTION = 0.8


def _ensure_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS composio_call_daily (
            day TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def record_composio_call(n: int = 1) -> None:
    """Increment today's Composio call count (best-effort; never blocks a call).

    Uses its own short-timeout connection instead of get_lexi_connection: this
    runs inside every real Composio call, including calls made while another
    connection in the same process holds the write lock (e.g. the approval
    transaction around the send pipeline). Inheriting the standard 30s
    busy_timeout turned each of those calls into a 30s stall — long enough to
    blow the Teams gateway's 120s ceiling. Losing a count is fine; waiting is not.
    """
    try:
        conn = sqlite3.connect(settings.lexi_database_path, timeout=0.25)
        try:
            conn.execute("PRAGMA busy_timeout = 250")
            _ensure_table(conn)
            conn.execute(
                """
                INSERT INTO composio_call_daily(day, count)
                VALUES (date('now'), ?)
                ON CONFLICT(day) DO UPDATE SET count = count + ?
                """,
                (n, n),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def budget_status() -> dict[str, Any]:
    """Month-to-date Composio call count vs budget, with an alarm flag."""
    with get_lexi_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS c FROM composio_call_daily "
            "WHERE day >= date('now', 'start of month')"
        ).fetchone()
        today = conn.execute(
            "SELECT COALESCE(count, 0) AS c FROM composio_call_daily WHERE day = date('now')"
        ).fetchone()
    month = int(row["c"] or 0)
    frac = round(month / _MONTHLY_BUDGET, 3) if _MONTHLY_BUDGET else 0.0
    return {
        "month_to_date": month,
        "today": int((today or {"c": 0})["c"] or 0),
        "monthly_budget": _MONTHLY_BUDGET,
        "fraction_used": frac,
        "alarm": frac >= _ALARM_FRACTION,
    }
