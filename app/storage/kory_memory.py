"""Explicit long-term facts Kory states (not chat thread memory)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from app.storage.lexi_db import get_lexi_connection


def ensure_kory_memory_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kory_memory (
            id TEXT PRIMARY KEY,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'teams',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_kory_memory_fact_key
        ON kory_memory (fact_key)
        """
    )


def upsert_fact(*, fact_key: str, fact_value: str, source: str = "teams") -> dict[str, Any]:
    key = fact_key.strip().lower()
    value = fact_value.strip()
    if not key or not value:
        return {"ok": False, "error": "fact_key and fact_value are required."}

    with get_lexi_connection() as conn:
        ensure_kory_memory_table(conn)
        existing = conn.execute(
            "SELECT id FROM kory_memory WHERE fact_key = ?",
            (key,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE kory_memory
                SET fact_value = ?, source = ?, updated_at = datetime('now')
                WHERE fact_key = ?
                """,
                (value, source, key),
            )
            fact_id = existing["id"]
        else:
            fact_id = uuid.uuid4().hex[:16]
            conn.execute(
                """
                INSERT INTO kory_memory (id, fact_key, fact_value, source)
                VALUES (?, ?, ?, ?)
                """,
                (fact_id, key, value, source),
            )
        conn.commit()
    return {"ok": True, "id": fact_id, "fact_key": key, "fact_value": value}


def delete_fact(*, fact: str) -> dict[str, Any]:
    """Remove a stored fact by key, id, or unique substring of key/value.

    Refuses ambiguity: deleting the wrong scheduling rule is worse than asking
    Kory which one he meant. Returns the deleted fact so the confirmation can
    quote exactly what was forgotten.
    """
    needle = fact.strip().lower()
    if not needle:
        return {"ok": False, "error": "Say which fact to forget."}

    with get_lexi_connection() as conn:
        ensure_kory_memory_table(conn)
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, fact_key, fact_value FROM kory_memory"
            ).fetchall()
        ]
        exact = [
            r for r in rows if r["id"] == fact.strip() or r["fact_key"] == needle
        ]
        matches = exact or [
            r
            for r in rows
            if needle in r["fact_key"].lower() or needle in r["fact_value"].lower()
        ]
        if not matches:
            return {
                "ok": False,
                "error": f"No stored fact matches {fact!r}.",
                "facts": [
                    {"fact_key": r["fact_key"], "fact_value": r["fact_value"]}
                    for r in rows[:20]
                ],
            }
        if len(matches) > 1:
            return {
                "ok": False,
                "error": (
                    f"{fact!r} matches {len(matches)} stored facts — which one? "
                    + "; ".join(r["fact_key"] for r in matches[:5])
                ),
                "candidates": [
                    {"fact_key": r["fact_key"], "fact_value": r["fact_value"]}
                    for r in matches[:5]
                ],
            }
        target = matches[0]
        conn.execute("DELETE FROM kory_memory WHERE id = ?", (target["id"],))
        conn.commit()
    return {
        "ok": True,
        "deleted": {
            "fact_key": target["fact_key"],
            "fact_value": target["fact_value"],
        },
    }


def list_facts(*, limit: int = 50) -> list[dict[str, Any]]:
    with get_lexi_connection() as conn:
        ensure_kory_memory_table(conn)
        rows = conn.execute(
            """
            SELECT id, fact_key, fact_value, source, created_at, updated_at
            FROM kory_memory
            ORDER BY COALESCE(updated_at, created_at) DESC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def facts_prompt_block(*, limit: int = 20) -> str:
    facts = list_facts(limit=limit)
    if not facts:
        return ""
    lines = ["KORY MEMORY (explicit facts — override defaults when relevant):"]
    for item in facts:
        lines.append(f"- {item['fact_key']}: {item['fact_value']}")
    return "\n".join(lines)
