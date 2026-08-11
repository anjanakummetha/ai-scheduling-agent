#!/usr/bin/env python3
"""Lexi local simulation engine — full pipeline without network I/O.

Runs triage → scheduler → approval card preview → mocked Composio execution
using deterministic mocks. Safe for offline VPS prep and CI.

Usage:
    .venv/bin/python test_local_pipeline.py              # all scenarios
    .venv/bin/python test_local_pipeline.py --scenario a
    .venv/bin/python test_local_pipeline.py --scenario b
    .venv/bin/python test_local_pipeline.py --scenario c
    .venv/bin/python test_local_pipeline.py --keep-db    # skip DB reset
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The "no network I/O" promise above is not self-enforcing. This script runs as a
# subprocess (phase suite P5-01), so pytest's conftest guard never covers it, and
# app.config calls load_dotenv(override=False) — meaning it would otherwise inherit
# the developer's live .env, Teams credentials and all. A dev whose .env has
# LEXI_TEAMS_ENABLED=true could push real Adaptive Cards from a "simulation" run.
# Set before app.config is imported so these win over .env.
for _key, _value in {
    "LEXI_TEAMS_ENABLED": "false",
    "LEXI_SUPPRESS_TEAMS_PUSH": "true",
}.items():
    os.environ[_key] = _value

from app.agents.comms_agent import execute_lexi_approval, get_lexi_pending_queue
from app.agents.scheduler_agent import ScheduleResult, process_pending_schedules
from app.agents.triage_agent import TriageResult, process_new_email
from app.config import settings
from scripts.init_lexi_db import init_lexi_db

# ── Scenario A: external scheduling request ─────────────────────────────────

SCENARIO_A_EMAIL = {
    "thread_id": "sim-thread-scheduling-001",
    "subject": "Project sync tomorrow?",
    "sender": "client.partner@externalcorp.com",
    "received_at": "2026-06-01 14:00:00",
    "raw_body": (
        "Hi Kory, let's meet tomorrow at 2 PM EST to discuss the project. "
        "Let me know if that works or suggest another time."
    ),
}

SCENARIO_A_TRIAGE = TriageResult(
    intent="pitch",
    priority="medium",
    confidence_score=0.92,
    justification="External client proposes a specific meeting time for a project discussion.",
    source="local_simulation",
)

SCENARIO_A_SCHEDULE = ScheduleResult(
    slots=[
        {"start": "2026-06-02T12:00:00-06:00", "end": "2026-06-02T12:30:00-06:00"},
        {"start": "2026-06-02T14:00:00-06:00", "end": "2026-06-02T14:30:00-06:00"},
        {"start": "2026-06-03T10:00:00-06:00", "end": "2026-06-03T10:30:00-06:00"},
    ],
    drafted_reply=(
        "Hi there,\n\n"
        "Thanks for reaching out — a few options that work on my end:\n\n"
        "- Monday, June 2 at 12:00 PM MT (2:00 PM Eastern)\n"
        "- Monday, June 2 at 2:00 PM MT (4:00 PM Eastern)\n"
        "- Tuesday, June 3 at 10:00 AM MT\n\n"
        "Let me know which works best.\n\n"
        "Let's Win,\n"
        "Kory"
    ),
    confidence_score=0.88,
    source="local_simulation",
)

# ── Scenario B: non-scheduling ───────────────────────────────────────────────

SCENARIO_B_EMAIL = {
    "thread_id": "sim-thread-nosched-002",
    "subject": "Re: Notes",
    "sender": "colleague@externalcorp.com",
    "received_at": "2026-06-01 15:30:00",
    "raw_body": "Thanks for the notes, talk later!",
}

SCENARIO_B_TRIAGE = TriageResult(
    intent="non_scheduling",
    priority="low",
    confidence_score=0.97,
    justification="Acknowledgment only; no scheduling action requested.",
    source="local_simulation",
)

MOCK_CALENDAR_CONTEXT = {
    "status": "available",
    "source": "local_simulation",
    "busy_events": [],
    "range_start": "2026-06-01T00:00:00+00:00",
    "range_end": "2026-06-15T00:00:00+00:00",
}

KORY_AAD_OBJECT_ID = "46ed1889-8fb8-4add-a29a-495a81a2f1b0"


def trace(step: str, detail: str = "") -> None:
    prefix = "[Lexi]"
    if detail:
        print(f"{prefix} {step} — {detail}")
    else:
        print(f"{prefix} {step}")


def banner(title: str) -> None:
    width = 72
    print()
    print("=" * width)
    print(title.center(width))
    print("=" * width)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.lexi_database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def reset_database() -> None:
    trace("DB", f"Initializing {settings.lexi_database_path}")
    init_lexi_db()
    conn = connect()
    try:
        for table in ("approvals", "holds", "audit_log", "proposals", "email_threads"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN (?, ?, ?, ?, ?)",
            ("approvals", "holds", "audit_log", "proposals", "email_threads"),
        )
        conn.commit()
    finally:
        conn.close()


def count_rows(table: str) -> int:
    conn = connect()
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def run_scenario_a() -> int | None:
    """Valid scheduling email → pending_approval + Adaptive Card JSON preview."""
    banner("SCENARIO A — VALID SCHEDULING EMAIL")
    trace("INPUT", SCENARIO_A_EMAIL["raw_body"][:80] + "...")

    proposals_before = count_rows("proposals")

    with patch("app.agents.triage_agent._call_llm_triage", return_value=SCENARIO_A_TRIAGE):
        proposal_id = process_new_email(SCENARIO_A_EMAIL)

    trace("TRIAGE", f"intent={SCENARIO_A_TRIAGE.intent} priority={SCENARIO_A_TRIAGE.priority}")
    if proposal_id is None:
        print("[FAIL] Expected a proposal id for scheduling email.")
        return None
    trace("DB", f"Created proposal_id={proposal_id} status=pending_triage")

    with (
        patch("app.agents.scheduler_agent._load_calendar_context", return_value=MOCK_CALENDAR_CONTEXT),
        patch("app.agents.scheduler_agent._call_llm_scheduler", return_value=SCENARIO_A_SCHEDULE),
    ):
        processed = process_pending_schedules()

    trace("SCHEDULER", f"processed_ids={processed}")
    if proposal_id not in processed:
        print("[FAIL] Scheduler did not advance Scenario A proposal.")
        return None

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, intent_classification, drafted_reply FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row["status"] != "pending_approval":
            print(f"[FAIL] Expected pending_approval, got {row['status']}")
            return None
        trace("DB", f"status={row['status']} intent={row['intent_classification']}")
    finally:
        conn.close()

    queue = get_lexi_pending_queue()
    card_item = next((item for item in queue if item.proposal_id == proposal_id), None)
    if card_item is None:
        print("[FAIL] Proposal not found in pending approval queue.")
        return None

    trace("TEAMS", "Adaptive Card payload that would be sent to Teams:")
    print(json.dumps(card_item.approval_card, indent=2))
    trace("TEAMS", card_item.teams_summary_line())

    proposals_after = count_rows("proposals")
    if proposals_after != proposals_before + 1:
        print("[FAIL] Unexpected proposal count after Scenario A.")
        return None

    print("\n[PASS] Scenario A complete.")
    return proposal_id


def run_scenario_b() -> int:
    """Irrelevant email → non_scheduling / no_action, no DB rows."""
    banner("SCENARIO B — NON-SCHEDULING EMAIL (IGNORE)")
    trace("INPUT", SCENARIO_B_EMAIL["raw_body"])

    proposals_before = count_rows("proposals")
    threads_before = count_rows("email_threads")

    with patch("app.agents.triage_agent._call_llm_triage", return_value=SCENARIO_B_TRIAGE):
        proposal_id = process_new_email(SCENARIO_B_EMAIL)

    trace("TRIAGE", f"intent={SCENARIO_B_TRIAGE.intent} → action=no_action")
    if proposal_id is not None:
        print(f"[FAIL] Expected no proposal; got proposal_id={proposal_id}")
        return 1

    proposals_after = count_rows("proposals")
    threads_after = count_rows("email_threads")

    if proposals_after != proposals_before:
        print("[FAIL] Proposal row was created for non-scheduling email.")
        return 1
    if threads_after != threads_before:
        print("[FAIL] email_threads row was created for non-scheduling email.")
        return 1

    conn = connect()
    try:
        audit = conn.execute(
            """
            SELECT message, payload FROM audit_log
            WHERE reference_id = ? AND step_name = 'triage_detection'
            ORDER BY id DESC LIMIT 1
            """,
            (SCENARIO_B_EMAIL["thread_id"],),
        ).fetchone()
        if audit is None:
            print("[FAIL] Missing audit log for ignored email.")
            return 1
        trace("AUDIT", audit["message"])
        payload = json.loads(audit["payload"] or "{}")
        trace("AUDIT", f"logged action={payload.get('action')}")
    finally:
        conn.close()

    print("\n[PASS] Scenario B complete — no scheduling records created.")
    return 0


def run_scenario_c(proposal_id: int | None = None) -> int:
    """Mock Teams Approve → sends offer email; status offer_sent (invite is a second step)."""
    banner("SCENARIO C — MOCK TEAMS APPROVE → OUTLOOK HOLD")
    if proposal_id is None:
        proposal_id = run_scenario_a()
        if proposal_id is None:
            return 1

    queue = get_lexi_pending_queue()
    item = next((q for q in queue if q.proposal_id == proposal_id), None)
    if item is None or not item.proposed_slots:
        print("[FAIL] No pending slots to approve for Scenario C.")
        return 1

    selected_slot = str(item.proposed_slots[0]["start"])
    trace("APPROVAL", f"Simulating Adaptive Card submit: decision=approved slot={selected_slot}")
    trace("SECURITY", f"authorized_by={KORY_AAD_OBJECT_ID}")

    composio_calls: list[dict[str, Any]] = []

    def mock_create_calendar_event(action: dict[str, Any]) -> tuple[str, str]:
        composio_calls.append({"tool": "create_calendar_event", "action": action})
        return ("sim-outlook-event-001", "sim-composio-log-cal")

    def mock_delete_calendar_event(_event_id: str) -> str:
        composio_calls.append({"tool": "delete_calendar_event"})
        return "sim-composio-log-del"

    def mock_create_draft_reply(thread_id: str, body: str) -> tuple[str, str]:
        composio_calls.append(
            {"tool": "create_draft_reply", "thread_id": thread_id, "body_len": len(body)}
        )
        return ("sim-draft-001", "sim-composio-log-draft")

    def mock_send_draft(_draft_id: str) -> str:
        composio_calls.append({"tool": "send_draft"})
        return "sim-composio-log-send"

    with (
        patch("app.agents.comms_agent.create_calendar_event", side_effect=mock_create_calendar_event),
        patch("app.agents.comms_agent.delete_calendar_event", side_effect=mock_delete_calendar_event),
        patch("app.agents.comms_agent.create_draft_reply", side_effect=mock_create_draft_reply),
        patch("app.agents.comms_agent.send_draft", side_effect=mock_send_draft),
    ):
        result = execute_lexi_approval(
            proposal_id=proposal_id,
            decision="approved",
            selected_slot=selected_slot,
            authorized_by=KORY_AAD_OBJECT_ID,
            decision_source="teams_card_simulation",
        )

    trace("EXECUTION", json.dumps(result.to_dict(), indent=2))
    if not result.ok:
        print("[FAIL] execute_lexi_approval returned ok=False.")
        return 1

    if result.status != "offer_sent":
        print(f"[FAIL] Expected status offer_sent after Send offer, got {result.status}")
        return 1

    calendar_calls = [c for c in composio_calls if c.get("tool") == "create_calendar_event"]
    if not calendar_calls:
        print("[FAIL] Composio calendar create was not invoked.")
        return 1
    trace("COMPOSIO", f"Calendar block simulated via {len(calendar_calls)} create_calendar_event call(s)")

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None or row["status"] != "offer_sent":
            print(f"[FAIL] DB status not offer_sent (got {row['status'] if row else None})")
            return 1
        trace("DB", f"proposal_id={proposal_id} final_status=offer_sent")
    finally:
        conn.close()

    print("\n[PASS] Scenario C complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Lexi local pipeline simulation (no network).")
    parser.add_argument(
        "--scenario",
        choices=("a", "b", "c", "all"),
        default="all",
        help="Which scenario to run (default: all).",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Do not wipe lexi.db before running.",
    )
    args = parser.parse_args()

    banner("LEXI LOCAL SIMULATION ENGINE")
    trace("CONFIG", f"database={settings.lexi_database_path.resolve()}")
    trace("CONFIG", f"root={settings.lexi_database_path.parent.parent.resolve()}")

    if not args.keep_db:
        reset_database()

    exit_code = 0
    proposal_id: int | None = None

    if args.scenario in ("a", "all"):
        proposal_id = run_scenario_a()
        if proposal_id is None:
            exit_code = 1

    if exit_code == 0 and args.scenario in ("b", "all"):
        if run_scenario_b() != 0:
            exit_code = 1

    if exit_code == 0 and args.scenario in ("c", "all"):
        if run_scenario_c(proposal_id) != 0:
            exit_code = 1

    banner("SIMULATION FINISHED" if exit_code == 0 else "SIMULATION FAILED")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
