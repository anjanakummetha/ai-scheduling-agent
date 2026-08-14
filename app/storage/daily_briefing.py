"""The CEO briefing Kory was actually sent this morning.

Kory reads the briefing at 4:45 AM and then asks Lexi to act on what is in it —
"change those two tasks from my briefing". Lexi had no idea what he meant: the
dashboard composes the briefing, Lexi only sends it, and nothing kept a copy.

So the send stores it. What is kept is the email he *received*, not a fresh
regeneration — by mid-morning the two differ (meetings move, mail arrives), and
answering from a regenerated briefing would contradict the page he is reading.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage.lexi_db import get_lexi_connection


def ensure_daily_briefing_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_briefings (
            briefing_date TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            body_text TEXT NOT NULL,
            body_html TEXT,
            sent_at TEXT NOT NULL DEFAULT (datetime('now')),
            message_id TEXT
        )
        """
    )


def _today() -> str:
    return datetime.now(tz=ZoneInfo(settings.scheduling_timezone)).date().isoformat()


def save_briefing(
    *,
    subject: str,
    body_text: str,
    body_html: str = "",
    message_id: str = "",
    briefing_date: str = "",
) -> dict[str, Any]:
    """Record the briefing that was just sent. Re-sending the same day replaces it."""
    day = (briefing_date or "").strip() or _today()
    text = (body_text or "").strip()
    if not text:
        return {"ok": False, "error": "briefing body is empty — nothing stored"}
    with get_lexi_connection() as conn:
        ensure_daily_briefing_table(conn)
        conn.execute(
            """
            INSERT INTO daily_briefings
                (briefing_date, subject, body_text, body_html, sent_at, message_id)
            VALUES (?, ?, ?, ?, datetime('now'), ?)
            ON CONFLICT(briefing_date) DO UPDATE SET
                subject=excluded.subject,
                body_text=excluded.body_text,
                body_html=excluded.body_html,
                sent_at=excluded.sent_at,
                message_id=excluded.message_id
            """,
            (day, (subject or "CEO Daily Briefing").strip(), text, body_html or "", message_id or ""),
        )
        conn.commit()
    return {"ok": True, "briefing_date": day, "characters": len(text)}


def get_briefing(briefing_date: str = "") -> dict[str, Any] | None:
    """The briefing for a day (default today), or None if none was stored."""
    day = (briefing_date or "").strip() or _today()
    with get_lexi_connection() as conn:
        ensure_daily_briefing_table(conn)
        row = conn.execute(
            """
            SELECT briefing_date, subject, body_text, sent_at, message_id
            FROM daily_briefings WHERE briefing_date = ?
            """,
            (day,),
        ).fetchone()
    return dict(row) if row else None


def latest_briefing(*, within_days: int = 4) -> dict[str, Any] | None:
    """The most recent briefing, so a Monday question about Friday's still lands."""
    cutoff = (date.fromisoformat(_today()) - timedelta(days=within_days)).isoformat()
    with get_lexi_connection() as conn:
        ensure_daily_briefing_table(conn)
        row = conn.execute(
            """
            SELECT briefing_date, subject, body_text, sent_at, message_id
            FROM daily_briefings WHERE briefing_date >= ?
            ORDER BY briefing_date DESC LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
    return dict(row) if row else None


def prune_briefings(*, keep_days: int = 30) -> int:
    """Briefings age out — this is a working memory, not an archive."""
    cutoff = (date.fromisoformat(_today()) - timedelta(days=keep_days)).isoformat()
    with get_lexi_connection() as conn:
        ensure_daily_briefing_table(conn)
        cursor = conn.execute("DELETE FROM daily_briefings WHERE briefing_date < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount or 0


def html_to_text(html: str) -> str:
    """Readable plain text from the briefing HTML, for when no text part is supplied."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)\s*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html

    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
