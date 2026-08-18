#!/usr/bin/env python3
"""Initialize the unified Lexi scheduling database at data/lexi.db.

Phase 1 schema: email_threads, proposals, holds, approvals, audit_log.

Usage:
    python scripts/init_lexi_db.py
    .venv/bin/python scripts/init_lexi_db.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "lexi.db"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS email_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL UNIQUE,
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    raw_body TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    status TEXT NOT NULL,
    intent_classification TEXT,
    priority_tier TEXT,
    rule_reasoning TEXT,
    proposed_slots TEXT,
    drafted_reply TEXT,
    confidence_score REAL,
    justification TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    FOREIGN KEY (thread_id) REFERENCES email_threads (thread_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    event_id TEXT,
    slot_start TEXT,
    slot_end TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (proposal_id) REFERENCES proposals (id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    authorized_by TEXT,
    modification_notes TEXT,
    decided_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (proposal_id) REFERENCES proposals (id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_name TEXT NOT NULL,
    reference_id TEXT,
    log_level TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    payload TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_email_threads_thread_id
    ON email_threads (thread_id);

CREATE INDEX IF NOT EXISTS idx_proposals_status
    ON proposals (status);

CREATE INDEX IF NOT EXISTS idx_proposals_thread_id
    ON proposals (thread_id);

CREATE INDEX IF NOT EXISTS idx_holds_proposal_id
    ON holds (proposal_id);

CREATE INDEX IF NOT EXISTS idx_approvals_proposal_id
    ON approvals (proposal_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_reference_id
    ON audit_log (reference_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
    ON audit_log (timestamp);

CREATE TABLE IF NOT EXISTS scheduling_sessions (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'hermes',
    status TEXT NOT NULL DEFAULT 'active',
    context_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduling_sessions_status
    ON scheduling_sessions (status);

CREATE TRIGGER IF NOT EXISTS trg_proposals_set_updated_at
AFTER UPDATE ON proposals
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE proposals
    SET updated_at = datetime('now')
    WHERE id = NEW.id;
END;
"""


def _migrate_scheduling_sessions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduling_sessions (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL DEFAULT 'hermes',
            status TEXT NOT NULL DEFAULT 'active',
            context_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scheduling_sessions_status
        ON scheduling_sessions (status)
        """
    )


def _migrate_email_thread_conversation(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(email_threads)").fetchall()}
    if "conversation_id" not in columns:
        conn.execute("ALTER TABLE email_threads ADD COLUMN conversation_id TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_email_threads_conversation_id
        ON email_threads (conversation_id)
        """
    )


def _migrate_proposal_recipient_timezone(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "recipient_timezone" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN recipient_timezone TEXT")


def _migrate_proposal_recipient_slot(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "recipient_selected_slot" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN recipient_selected_slot TEXT")


def _migrate_proposal_metadata(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    additions = {
        "voice_mode": "TEXT NOT NULL DEFAULT 'kory'",
        "send_channel": "TEXT NOT NULL DEFAULT 'kory'",
        "is_delegation": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE proposals ADD COLUMN {name} {ddl}")


def _migrate_proposal_teams_notify(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "teams_approval_notified_at" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN teams_approval_notified_at TEXT")


def _migrate_kory_memory(conn: sqlite3.Connection) -> None:
    from app.storage.kory_memory import ensure_kory_memory_table

    ensure_kory_memory_table(conn)


def _migrate_holds_expires_at(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(holds)").fetchall()
    }
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE holds ADD COLUMN expires_at TEXT")


def _migrate_email_thread_recipient_timezone(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(email_threads)").fetchall()}
    if "recipient_timezone" not in columns:
        conn.execute("ALTER TABLE email_threads ADD COLUMN recipient_timezone TEXT")


def _migrate_email_thread_headers_json(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(email_threads)").fetchall()}
    if "internet_headers_json" not in columns:
        conn.execute("ALTER TABLE email_threads ADD COLUMN internet_headers_json TEXT")


def _migrate_recipient_profiles(conn: sqlite3.Connection) -> None:
    from app.storage.recipient_profiles import ensure_recipient_profiles_table

    ensure_recipient_profiles_table(conn)


def _migrate_email_thread_sender_email(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(email_threads)").fetchall()}
    if "sender_email" not in columns:
        conn.execute("ALTER TABLE email_threads ADD COLUMN sender_email TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_email_threads_sender_email
        ON email_threads (sender_email)
        """
    )
    rows = conn.execute(
        "SELECT thread_id, sender FROM email_threads WHERE sender_email IS NULL OR sender_email = ''"
    ).fetchall()
    from app.storage.recipient_profiles import normalize_sender_email

    for row in rows:
        thread_id = row[0] if isinstance(row, tuple) else row["thread_id"]
        sender = row[1] if isinstance(row, tuple) else row["sender"]
        normalized = normalize_sender_email(str(sender or ""))
        if normalized:
            conn.execute(
                "UPDATE email_threads SET sender_email = ? WHERE thread_id = ?",
                (normalized, thread_id),
            )


def _migrate_proposal_reply_message_id(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "reply_message_id" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN reply_message_id TEXT")


def _migrate_proposal_kory_guidance(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "kory_scheduling_guidance" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN kory_scheduling_guidance TEXT")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "scheduling_note" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN scheduling_note TEXT")


def _migrate_proposal_invite_event(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    if "invite_event_id" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN invite_event_id TEXT")


def _migrate_teams_list_snapshots(conn: sqlite3.Connection) -> None:
    """Remembers the order the pending list was last rendered in, so a draft
    number means the list Kory was shown rather than whatever the queue says
    when he types. See app/bot/draft_numbering.py."""
    from app.bot.draft_numbering import ensure_snapshot_table

    ensure_snapshot_table(conn)


def _migrate_proposal_world_facts(conn: sqlite3.Connection) -> None:
    """Monotonic records of irreversible side effects.

    Separate from ``status`` on purpose. A status is a workflow position and
    may legitimately move backwards (a thread re-enters scheduling); an email
    that reached someone's inbox cannot be un-sent. Conflating the two is what
    let a sent offer look unsent and get sent twice. See
    ``app/scheduling/proposal_state.py``.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    for name in ("offer_sent_at", "invite_sent_at"):
        if name not in columns:
            conn.execute(f"ALTER TABLE proposals ADD COLUMN {name} TEXT")
    # Backfill from the workflow position for rows that predate these columns:
    # anything that reached offer_sent or beyond definitely had its offer sent.
    conn.execute(
        """
        UPDATE proposals
        SET offer_sent_at = COALESCE(updated_at, created_at, datetime('now'))
        WHERE offer_sent_at IS NULL
          AND status IN ('offer_sent', 'pending_invite', 'pending_reoffer', 'executed')
        """
    )
    conn.execute(
        """
        UPDATE proposals
        SET invite_sent_at = COALESCE(updated_at, created_at, datetime('now'))
        WHERE invite_sent_at IS NULL
          AND status = 'executed'
        """
    )


def _install_proposal_guard_triggers(
    conn: sqlite3.Connection, *, enforce_transitions: bool = True
) -> None:
    from app.scheduling.proposal_state import install_guard_triggers

    install_guard_triggers(conn, enforce_transitions=enforce_transitions)


def _migrate_approval_feedback(conn: sqlite3.Connection) -> None:
    from app.storage.learning_log import ensure_approval_feedback_table

    ensure_approval_feedback_table(conn)


def init_lexi_db(db_path: Path = DB_PATH, *, enforce_transitions: bool = True) -> None:
    """Create data/lexi.db and apply the Lexi Phase 1 schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed_before = db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_SQL)
        _migrate_holds_expires_at(conn)
        _migrate_email_thread_conversation(conn)
        _migrate_email_thread_recipient_timezone(conn)
        _migrate_email_thread_headers_json(conn)
        _migrate_email_thread_sender_email(conn)
        _migrate_proposal_recipient_slot(conn)
        _migrate_proposal_recipient_timezone(conn)
        _migrate_scheduling_sessions(conn)
        _migrate_proposal_metadata(conn)
        _migrate_proposal_teams_notify(conn)
        _migrate_proposal_reply_message_id(conn)
        _migrate_proposal_kory_guidance(conn)
        _migrate_proposal_invite_event(conn)
        _migrate_kory_memory(conn)
        _migrate_recipient_profiles(conn)
        _migrate_approval_feedback(conn)
        _migrate_proposal_world_facts(conn)
        _migrate_teams_list_snapshots(conn)
        _install_proposal_guard_triggers(conn, enforce_transitions=enforce_transitions)
        conn.commit()

        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            if row[0] not in {"sqlite_sequence"}
        ]
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()

    action = "verified" if existed_before else "created"
    print(f"[lexi] Database {action}: {db_path}", file=sys.stderr)
    print(f"[lexi] Tables ({len(tables)}): {', '.join(tables)}", file=sys.stderr)
    print(f"[lexi] Indexes ({len(indexes)}): {', '.join(indexes)}", file=sys.stderr)
    print("[lexi] Schema initialization complete.", file=sys.stderr)


def main() -> int:
    # Escape hatch, documented in app/scheduling/proposal_state.py: drops the
    # legal-transition trigger without a code deploy if a legitimate path ever
    # turns out to be missing from LEGAL_TRANSITIONS.
    enforce = "--no-transition-guard" not in sys.argv
    try:
        from app.config import settings

        init_lexi_db(settings.lexi_database_path, enforce_transitions=enforce)
    except sqlite3.Error as exc:
        print(f"[lexi] ERROR: database initialization failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
