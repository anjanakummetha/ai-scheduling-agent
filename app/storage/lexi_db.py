"""SQLite connection helpers for the unified Lexi database."""

from __future__ import annotations

import sqlite3

from app.config import settings


def get_lexi_connection() -> sqlite3.Connection:
    """Open a connection to data/lexi.db with foreign keys enabled.

    The worker, the MCP server, and the API are separate processes writing the
    same file; a Kory-triggered approval races the webhook ingester. Without a
    busy timeout the loser gets an instant "database is locked" mid-execution.
    """
    settings.lexi_database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.lexi_database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn
