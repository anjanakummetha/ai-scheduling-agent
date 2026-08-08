#!/usr/bin/env python3
"""Close out the four lingering [TEST] artifacts (open-list cleanup item):

  #7041  executed  [TEST] meeting on Aug 10  -> cancel via tracked event
  #6861  executed  [TEST] meeting on Aug 24 (attendee declined) -> cancel
  #6190  awaiting_reply_prompt  [TEST] Intro call? - LT-A1      -> reject
  #6244  needs_kory             [TEST] Coffee or a call - LT-B3 -> reject

Test proposals left in live states are the source of stale Teams pings:
any inbound mail on their threads fires a follow-up card at Kory.

Run on the box with the prod env exported. Uses the app's own
cancel_booked_meeting so holds/status/audit stay consistent.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.comms_agent import cancel_booked_meeting  # noqa: E402
from app.storage.lexi_db import get_lexi_connection  # noqa: E402

CANCEL = [7041, 6861]
REJECT = [6190, 6244]
SOURCE = "cleanup_stale_test_threads"


def main() -> int:
    for pid in CANCEL:
        result = cancel_booked_meeting(
            pid,
            reason="[TEST] artifact cleanup — post-go-live sweep",
            authorized_by="anjana",
            decision_source=SOURCE,
        )
        print(f"cancel #{pid}: {json.dumps(result)[:300]}")

    with get_lexi_connection() as conn:
        for pid in REJECT:
            row = conn.execute(
                "SELECT p.status, e.subject FROM proposals p "
                "JOIN email_threads e ON e.thread_id = p.thread_id WHERE p.id = ?",
                (pid,),
            ).fetchone()
            if not row:
                print(f"reject #{pid}: NOT FOUND — skipped")
                continue
            subject = str(row["subject"] or "")
            if "[TEST]" not in subject:
                print(f"reject #{pid}: subject {subject!r} is not [TEST] — skipped")
                continue
            conn.execute(
                "UPDATE proposals SET status = 'rejected', "
                "updated_at = datetime('now') WHERE id = ?",
                (pid,),
            )
            conn.execute(
                "INSERT INTO approvals (proposal_id, decision, decision_source, "
                "authorized_by, modification_notes, decided_at) "
                "VALUES (?, 'rejected', ?, 'anjana', ?, datetime('now'))",
                (pid, SOURCE, "[TEST] artifact cleanup — post-go-live sweep"),
            )
            conn.execute(
                "INSERT INTO audit_log (step_name, reference_id, log_level, message, "
                "payload, timestamp) VALUES ('cleanup_stale_test_threads', ?, 'INFO', "
                "?, NULL, datetime('now'))",
                (pid, f"Rejected stale test proposal ({row['status']}) — {subject}"),
            )
            print(f"reject #{pid}: {row['status']} -> rejected ({subject})")
        conn.commit()

    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT p.id, p.status, e.subject FROM proposals p "
            "JOIN email_threads e ON e.thread_id = p.thread_id "
            "WHERE e.subject LIKE '%[TEST]%' AND p.status NOT IN "
            "('rejected', 'cancelled', 'skipped', 'auto_skipped', 'no_reply_needed')"
        ).fetchall()
    print("remaining live [TEST] proposals:", [(r["id"], r["status"]) for r in rows])
    return 0


if __name__ == "__main__":
    sys.exit(main())
