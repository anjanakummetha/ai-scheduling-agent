"""Per-contact context learned from email headers, body, or Kory approvals."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app.storage.lexi_db import get_lexi_connection


def normalize_sender_email(sender: str | None) -> str | None:
    """Extract lowercase email from a From header or bare address."""
    if not sender:
        return None
    text = sender.strip().lower()
    match = re.search(r"<([^>]+@[^>]+)>", text)
    if match:
        return match.group(1).strip()
    if "@" in text:
        return text
    return None


def ensure_recipient_profiles_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recipient_profiles (
            email TEXT PRIMARY KEY,
            timezone TEXT,
            timezone_source TEXT,
            display_name TEXT,
            notes TEXT,
            introducer_name TEXT,
            introducer_email TEXT,
            introducer_source TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recipient_profiles)").fetchall()}
    for col, ddl in (
        ("introducer_name", "ALTER TABLE recipient_profiles ADD COLUMN introducer_name TEXT"),
        ("introducer_email", "ALTER TABLE recipient_profiles ADD COLUMN introducer_email TEXT"),
        ("introducer_source", "ALTER TABLE recipient_profiles ADD COLUMN introducer_source TEXT"),
    ):
        if col not in columns:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recipient_profiles_updated
        ON recipient_profiles (updated_at)
        """
    )


def get_recipient_profile(email: str | None) -> dict[str, Any] | None:
    if not email or "@" not in email:
        return None
    key = normalize_sender_email(email) or email.strip().lower()
    with get_lexi_connection() as conn:
        ensure_recipient_profiles_table(conn)
        row = conn.execute(
            """
            SELECT email, timezone, timezone_source, display_name, notes,
                   introducer_name, introducer_email, introducer_source, updated_at
            FROM recipient_profiles WHERE email = ?
            """,
            (key,),
        ).fetchone()
    return dict(row) if row else None


def upsert_recipient_timezone(
    *,
    email: str,
    timezone: str,
    source: str,
    display_name: str = "",
) -> None:
    key = normalize_sender_email(email) or email.strip().lower()
    tz = timezone.strip()
    if not key or not tz:
        return
    with get_lexi_connection() as conn:
        ensure_recipient_profiles_table(conn)
        existing = conn.execute(
            "SELECT email FROM recipient_profiles WHERE email = ?",
            (key,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE recipient_profiles
                SET timezone = ?, timezone_source = ?,
                    display_name = COALESCE(NULLIF(?, ''), display_name),
                    updated_at = datetime('now')
                WHERE email = ?
                """,
                (tz, source, display_name.strip(), key),
            )
        else:
            conn.execute(
                """
                INSERT INTO recipient_profiles (email, timezone, timezone_source, display_name)
                VALUES (?, ?, ?, ?)
                """,
                (key, tz, source, display_name.strip() or None),
            )
        conn.commit()


def record_display_name(email: str | None, display_name: str | None) -> None:
    """Remember a contact's real name so greetings never guess from the address.

    Runs independently of timezone learning: without it a sender whose mailbox
    is a mashed local-part (anjanakummetha@) gets greeted "Hi Anjanakummetha".
    """
    key = normalize_sender_email(email) or (email or "").strip().lower()
    name = (display_name or "").strip()
    if not key or not name or "@" in name:
        return
    with get_lexi_connection() as conn:
        ensure_recipient_profiles_table(conn)
        existing = conn.execute(
            "SELECT email FROM recipient_profiles WHERE email = ?",
            (key,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE recipient_profiles
                SET display_name = COALESCE(NULLIF(display_name, ''), ?),
                    updated_at = datetime('now')
                WHERE email = ?
                """,
                (name, key),
            )
        else:
            conn.execute(
                "INSERT INTO recipient_profiles (email, display_name) VALUES (?, ?)",
                (key, name),
            )
        conn.commit()


def upsert_introducer(
    *,
    email: str,
    introducer_name: str,
    introducer_email: str | None = None,
    source: str = "inferred",
) -> None:
    key = normalize_sender_email(email) or email.strip().lower()
    name = introducer_name.strip()
    if not key or not name:
        return
    with get_lexi_connection() as conn:
        ensure_recipient_profiles_table(conn)
        existing = conn.execute(
            "SELECT email FROM recipient_profiles WHERE email = ?",
            (key,),
        ).fetchone()
        intro_email = (introducer_email or "").strip() or None
        if existing:
            conn.execute(
                """
                UPDATE recipient_profiles
                SET introducer_name = ?, introducer_email = COALESCE(?, introducer_email),
                    introducer_source = ?, updated_at = datetime('now')
                WHERE email = ?
                """,
                (name, intro_email, source, key),
            )
        else:
            conn.execute(
                """
                INSERT INTO recipient_profiles (
                    email, introducer_name, introducer_email, introducer_source
                ) VALUES (?, ?, ?, ?)
                """,
                (key, name, intro_email, source),
            )
        conn.commit()


def list_prior_email_threads(
    sender_email: str | None,
    *,
    limit: int = 20,
    exclude_thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recent Lexi-ingested threads from the same sender (for TZ / context reuse)."""
    key = normalize_sender_email(sender_email)
    if not key:
        return []
    with get_lexi_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(email_threads)").fetchall()}
        if "raw_body" not in columns:
            return []
        select_cols = ["thread_id", "raw_body", "received_at"]
        if "internet_headers_json" in columns:
            select_cols.insert(2, "internet_headers_json")
        if "sender_email" in columns:
            query = f"""
                SELECT {", ".join(select_cols)}
                FROM email_threads
                WHERE sender_email = ?
            """
            params: list[Any] = [key]
        else:
            query = f"""
                SELECT {", ".join(select_cols)}
                FROM email_threads
                WHERE lower(trim(sender)) = ?
                   OR lower(sender) LIKE ?
            """
            params = [key, f"%<{key}>%"]
        if exclude_thread_id:
            query += " AND thread_id != ?"
            params.append(exclude_thread_id.strip())
        query += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def humanize_email_local_part(email: str) -> str:
    """Best-effort name from an address alone, with no profile to consult."""
    local = (email or "").strip().split("@", 1)[0]
    cleaned = re.sub(r"[._\-]+", " ", local)
    cleaned = re.sub(r"\d+", "", cleaned).strip()
    return cleaned.title() if cleaned else local


def display_name_for_email(email: str | None) -> str:
    """The one place a contact's display name is resolved.

    Four modules grew their own copy of "name from email" and drifted: the
    profile store held "Anjana Kummetha" while a Teams card title still read
    "Anjanakummetha", because only some paths consulted the store. Prefer the
    learned name; fall back to the local part.
    """
    raw = (email or "").strip()
    if not raw:
        return "unknown"
    if "@" not in raw:
        return raw
    try:
        profile = get_recipient_profile(raw) or {}
        stored = str(profile.get("display_name") or "").strip()
        if stored:
            return stored
    except Exception:  # store unavailable — the address still has to render
        pass
    return humanize_email_local_part(raw)
