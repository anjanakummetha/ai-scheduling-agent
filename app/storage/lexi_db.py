"""SQLite connection helpers for the unified Lexi database."""

from __future__ import annotations

import sqlite3
import threading

from app.config import settings

# journal_mode is a persistent property of the DB file, so it only needs to be
# set once per process (the first connection flips the file to WAL and every
# later connection inherits it). Guarded so we don't run the PRAGMA on every
# open (audit 2026-08-15, D1).
_wal_enabled = False
_wal_lock = threading.Lock()


def _ensure_wal(conn: sqlite3.Connection) -> None:
    global _wal_enabled
    if _wal_enabled:
        return
    with _wal_lock:
        if _wal_enabled:
            return
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            _wal_enabled = True
        except sqlite3.Error:
            # A concurrent writer can hold the lock during the mode switch; the
            # next connection retries. Never fatal.
            pass


def get_lexi_connection() -> sqlite3.Connection:
    """Open a connection to data/lexi.db with foreign keys enabled.

    The worker, the MCP server, and the API are separate processes writing the
    same file; a Kory-triggered approval races the webhook ingester. WAL mode
    lets readers and writers proceed concurrently instead of blocking the whole
    file, and the busy timeout absorbs the brief remaining writer-vs-writer
    contention (audit 2026-08-15, D1).
    """
    settings.lexi_database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.lexi_database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    _ensure_wal(conn)
    return conn
