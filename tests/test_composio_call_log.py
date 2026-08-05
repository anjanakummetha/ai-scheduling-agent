"""Composio call counter must never stall a real call (LT-D1 defect).

During the first live approve, the send pipeline ran inside the approval
transaction; record_composio_call inherited the 30s busy_timeout and turned
every Composio call into a 30s stall, blowing the gateway's 120s ceiling and
killing the send mid-pipeline.
"""

from __future__ import annotations

import sqlite3
import time

from app.config import settings
from app.storage.composio_call_log import record_composio_call


def test_record_returns_fast_while_write_lock_held():
    # Simulate the approval transaction: another connection holds the write lock.
    locker = sqlite3.connect(settings.lexi_database_path)
    try:
        locker.execute(
            "CREATE TABLE IF NOT EXISTS composio_call_daily ("
            "day TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)"
        )
        locker.commit()
        locker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        record_composio_call()
        elapsed = time.monotonic() - started
    finally:
        locker.rollback()
        locker.close()
    assert elapsed < 2.0, f"counter blocked a Composio call for {elapsed:.1f}s"


def test_record_increments_when_unlocked():
    record_composio_call(3)
    conn = sqlite3.connect(settings.lexi_database_path)
    try:
        row = conn.execute(
            "SELECT count FROM composio_call_daily WHERE day = date('now')"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] >= 3
