#!/usr/bin/env python3
"""Final pre-production dry-run — live calendar/inbox reads, no Outlook writes.

Forces LEXI_DRY_RUN for the entire run. Does NOT start Teams or change connections.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Safety first — no Composio mail/calendar writes this run
os.environ["LEXI_DRY_RUN"] = "true"
os.environ["LEXI_SUPPRESS_TEAMS_PUSH"] = "true"

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.agents.comms_agent import execute_lexi_approval
from app.agents.delegation import detect_delegation
from app.agents.inbound_reply import AWAITING_REPLY_PROMPT, begin_draft_reply
from app.agents.triage_agent import TriageResult, process_new_email
from app.config import settings
from app.integrations.outlook_inbox import search_inbox
from app.rules.validators import validate_proposal_slots
from app.scheduling.busy_intervals import slot_conflicts_busy
from app.scheduling.calendar_context import load_scheduling_calendar_context
from app.scheduling.calendar_intelligence import resolve_write_calendar_name
from app.scheduling.email_format import build_scheduling_reply, sender_first_name
from app.scheduling.meeting_type import effective_scheduling_intent, resolve_meeting_type
from app.scheduling.schedule_from_context import schedule_from_context
from app.scheduling.timezone_intel import detect_recipient_timezone
from app.storage.lexi_db import get_lexi_connection
from app.teams.commands import handle_teams_card_submit
from app.utils.teams_cards import (
    CARD_ACTION_SAVE_DRAFT,
    INPUT_DRAFT_ID,
    generate_approval_card,
)
from scripts.init_lexi_db import init_lexi_db

REPORT_PATH = ROOT / "docs" / "FINAL_PRE_PRODUCTION_DRY_RUN.json"

SCHEDULING_RE = re.compile(
    r"\b(schedule|meet|coffee|intro|call|dinner|happy\s*hour|30\s*min|60\s*min|"
    r"available|time to connect|grab lunch|teams|zoom)\b",
    re.I,
)

def _lexi_cc_address() -> str:
    return (settings.lexi_mailbox_email or "lexi@iconicfounders.com").strip().lower()


SYNTHETIC_CASES: list[dict[str, Any]] = [
    {
        "id": "cc_lexi_intro",
        "subject": "TEST — intro for John",
        "sender": "kory.mitchell@iconicfounders.com",
        "body": "John — looping in Lexi to find us a time for a 30-minute intro next week.",
        "cc_recipients": [],  # filled at runtime
        "voice_modes": ["lexi", "kory"],
    },
    {
        "id": "cold_inbound_intro",
        "subject": "TEST — quick intro request",
        "sender": "stranger@newbank.com",
        "body": "Kory, would you have 30 minutes for a Teams intro next week? I'm in Chicago (CT).",
        "voice_modes": ["kory"],
    },
]


def record(
    results: list[dict[str, Any]],
    *,
    phase: str,
    case_id: str,
    name: str,
    passed: bool,
    detail: str = "",
    evidence: Any = None,
) -> None:
    results.append(
        {
            "phase": phase,
            "id": case_id,
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "detail": detail,
            "evidence": evidence,
        }
    )
    mark = "PASS" if passed else "FAIL"
    print(
        f"  [{mark}] {phase}/{case_id}: {name}" + (f" — {detail}" if detail else ""),
        flush=True,
    )


def verify_slots(
    slots: list[dict[str, str]],
    *,
    busy: list[dict],
    intent: str,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    if len(slots) < 2:
        return False, f"need 2+ slots, got {len(slots)}"
    spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
    reserve = spec.calendar_block_minutes
    for i, slot in enumerate(slots, 1):
        if slot_conflicts_busy(slot, busy, reserve_minutes=reserve):
            return False, f"slot {i} overlaps calendar"
        val = validate_proposal_slots(
            [slot],
            intent=spec.type_key,
            meeting_format="in_person" if spec.type_key in {"coffee", "happy_hour", "dinner"} else "virtual",
            busy_events=busy,
        )
        if not val.valid:
            return False, f"slot {i}: {val.violations[:1]}"
    return True, f"{len(slots)} slots calendar-clean"


def phase_safety(results: list[dict]) -> None:
    print("\n=== Phase 0: Safety gates ===")
    from app.safety.outbound_guard import (
        escalation_email_allowed,
        outbound_writes_allowed,
        teams_push_allowed,
    )

    record(results, phase="safety", case_id="dry_run", name="LEXI_DRY_RUN on", passed=settings.lexi_dry_run)
    record(
        results,
        phase="safety",
        case_id="no_cal_writes",
        name="Outbound writes blocked",
        passed=not outbound_writes_allowed(),
    )
    record(
        results,
        phase="safety",
        case_id="teams_suppressed",
        name="Teams push suppressed",
        passed=not teams_push_allowed(),
    )
    record(
        results,
        phase="safety",
        case_id="escalation_staged",
        name="Escalation emails staged (not sent)",
        passed=not escalation_email_allowed() or settings.lexi_dry_run,
    )


def phase_inbox_scheduling(results: list[dict], calendar_context: dict[str, Any]) -> None:
    print("\n=== Phase 1: Live inbox → scheduling engine ===")
    busy = list(calendar_context.get("busy_events") or [])
    try:
        messages, log_id = search_inbox(query="schedule OR meeting OR intro OR coffee", top=30)
        record(
            results,
            phase="inbox",
            case_id="read",
            name="Read Kory inbox",
            passed=True,
            detail=f"{len(messages)} messages (log={log_id})",
        )
    except Exception as exc:
        record(results, phase="inbox", case_id="read", name="Read Kory inbox", passed=False, detail=str(exc))
        return

    candidates = []
    for msg in messages:
        subj = str(msg.get("subject") or "")
        body = str(msg.get("preview") or msg.get("body") or "")
        if subj.lower().startswith("invitation:"):
            continue
        if not SCHEDULING_RE.search(f"{subj}\n{body}"):
            continue
        if "newsletter" in subj.lower() or "unsubscribe" in body.lower():
            continue
        if subj.lower().startswith("accepted:") or "md&a" in subj.lower():
            continue
        if "keystone" in subj.lower() or "project " in subj.lower():
            continue
        if "iconicfounders.com" in str(msg.get("sender") or "").lower():
            continue
        candidates.append(msg)
        if len(candidates) >= 6:
            break

    if not candidates:
        record(
            results,
            phase="inbox",
            case_id="samples",
            name="Scheduling-like inbox samples",
            passed=False,
            detail="no scheduling emails found in top 30",
        )
        return

    for index, msg in enumerate(candidates, start=1):
        case_id = f"inbox_{index:02d}"
        subject = str(msg.get("subject") or "")[:120]
        sender = str(msg.get("sender") or "unknown@example.com")
        body = str(msg.get("preview") or msg.get("body") or "Can we schedule a call?")[:2000]
        intent = effective_scheduling_intent("unknown", subject=subject, body=body)

        outcome = schedule_from_context(
            subject=subject,
            body=body,
            intent=intent,
            sender_email=sender,
            use_llm_plan=False,
            calendar_context=calendar_context,
        )
        tz = detect_recipient_timezone(sender_email=sender, body=body)
        write_cal = resolve_write_calendar_name(intent=intent)

        if outcome.ok and outcome.slots:
            resolved_intent = (
                outcome.diagnostics.get("meeting_type")
                or intent
                or outcome.diagnostics.get("meeting_type", "referral_or_intro")
            )
            ok, detail = verify_slots(
                outcome.slots,
                busy=busy,
                intent=resolved_intent,
                subject=subject,
                body=body,
            )
            record(
                results,
                phase="inbox",
                case_id=case_id,
                name=subject[:60],
                passed=ok,
                detail=f"{detail}; tz={tz.tz_name()} ({tz.source}); write→{write_cal}",
                evidence={
                    "sender": sender,
                    "intent": intent,
                    "path": outcome.path,
                    "slots": outcome.slots[:3],
                    "formatted": outcome.formatted_slots[:3],
                },
            )
        elif intent in {"lunch", "lunch_request"} or "dinner" in (intent or ""):
            record(
                results,
                phase="inbox",
                case_id=case_id,
                name=subject[:60],
                passed=True,
                detail=f"correct no-slot/escalation: {outcome.failure_message[:80]}",
                evidence={"sender": sender, "intent": intent},
            )
        else:
            record(
                results,
                phase="inbox",
                case_id=case_id,
                name=subject[:60],
                passed=False,
                detail=outcome.failure_message[:120] or outcome.status,
                evidence={"sender": sender, "intent": intent, "path": outcome.path},
            )


def _insert_test_thread(
    *,
    thread_id: str,
    subject: str,
    sender: str,
    body: str,
) -> int:
    triage_intent = effective_scheduling_intent("unknown", subject=subject, body=body)
    triage = TriageResult(
        intent=triage_intent,
        priority="medium",
        confidence_score=0.9,
        justification="dry-run synthetic",
        source="dry_run",
    )
    with patch("app.agents.triage_agent._call_llm_triage", return_value=triage):
        proposal_id = process_new_email(
            {
                "thread_id": thread_id,
                "subject": subject,
                "sender": sender,
                "raw_body": body,
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return int(proposal_id or 0)


@contextmanager
def _fast_pipeline_context(calendar_context: dict[str, Any]):
    """Avoid Composio thread fetches, calendar reloads, and LLM latency."""
    from app.scheduling import scheduling_plan as scheduling_plan_mod

    _orig_plan = scheduling_plan_mod.build_scheduling_plan

    def _plan_no_llm(**kwargs: Any) -> Any:
        kwargs["use_llm"] = False
        return _orig_plan(**kwargs)

    def _cached_calendar(**_kwargs: Any) -> dict[str, Any]:
        return calendar_context

    with (
        patch("app.agents.scheduler_agent._load_calendar_context", side_effect=_cached_calendar),
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value=calendar_context,
        ),
        patch(
            "app.integrations.outlook_thread.fetch_conversation_context",
            return_value="",
        ),
        patch("app.scheduling.hermes_compose._load_thread_context", return_value=[]),
        patch("app.scheduling.hermes_compose._conversation_id_for_thread", return_value=None),
        patch(
            "app.scheduling.hermes_compose._hermes_offer_compose",
            side_effect=RuntimeError("dry-run: template only"),
        ),
        patch("app.scheduling.scheduling_plan.build_scheduling_plan", side_effect=_plan_no_llm),
    ):
        yield


def phase_full_pipeline(results: list[dict], calendar_context: dict[str, Any]) -> None:
    print("\n=== Phase 2: Full DB pipeline (draft → card → save → discard) ===")
    init_lexi_db()
    thread_id = f"dry-run-{uuid.uuid4().hex[:8]}"
    subject = "TEST — intro call — Denver family office"
    sender = "anjana.kummetha@iconicfounders.com"
    body = (
        "I'm with a Denver-based family office. Would you have 30 minutes "
        "sometime next week? Mornings work best."
    )

    proposal_id = _insert_test_thread(
        thread_id=thread_id,
        subject=subject,
        sender=sender,
        body=body,
    )
    if not proposal_id:
        record(results, phase="pipeline", case_id="triage", name="Create proposal", passed=False)
        return

    with get_lexi_connection() as conn:
        conn.execute(
            "UPDATE proposals SET recipient_timezone = ? WHERE id = ?",
            ("America/Denver", proposal_id),
        )
        conn.commit()

    with _fast_pipeline_context(calendar_context):
        with get_lexi_connection() as conn:
            row = conn.execute("SELECT status FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        record(
            results,
            phase="pipeline",
            case_id="awaiting_prompt",
            name="Stops at awaiting_reply_prompt",
            passed=row and row["status"] == AWAITING_REPLY_PROMPT,
            detail=str(row["status"] if row else None),
        )

        draft_result = begin_draft_reply(proposal_id, voice_mode="lexi")
        passed = bool(draft_result.get("ok"))
        if not passed:
            record(
                results,
                phase="pipeline",
                case_id="voice_lexi",
                name="Draft in Lexi voice (pipeline)",
                passed=False,
                detail=str(draft_result.get("error", ""))[:120],
            )
            return
        bundle = _fetch_bundle(proposal_id)
        draft = str(bundle.get("drafted_reply") or "")
        record(
            results,
            phase="pipeline",
            case_id="voice_lexi",
            name="Draft in Lexi voice (pipeline)",
            passed=passed and "lexi@iconicfounders.com" in draft.lower(),
            detail=draft[:100].replace("\n", " "),
        )

        sample_slots = [
            {"start": "2026-07-22T14:00:00-06:00", "end": "2026-07-22T14:30:00-06:00"},
            {"start": "2026-07-23T09:00:00-06:00", "end": "2026-07-23T09:30:00-06:00"},
        ]
        first = sender_first_name(sender)
        kory_body = build_scheduling_reply(
            recipient_first_name=first,
            slots=sample_slots,
            sender_email=sender,
            intent="referral_or_intro",
            subject=subject,
            voice_mode="kory",
            recipient_body=body,
        )
        lexi_body = build_scheduling_reply(
            recipient_first_name=first,
            slots=sample_slots,
            sender_email=sender,
            intent="referral_or_intro",
            subject=subject,
            voice_mode="lexi",
            recipient_body=body,
        )
        record(
            results,
            phase="pipeline",
            case_id="voice_kory",
            name="Kory voice sign-off",
            passed="let's win" in kory_body.lower() and "kory" in kory_body.lower(),
        )
        record(
            results,
            phase="pipeline",
            case_id="voice_lexi_compose",
            name="Lexi voice sign-off",
            passed="lexi@iconicfounders.com" in lexi_body.lower(),
        )

        bundle = _fetch_bundle(proposal_id)
        email_row = {
            "subject": bundle.get("subject"),
            "sender": bundle.get("sender"),
            "raw_body": bundle.get("raw_body"),
        }
        card = generate_approval_card(bundle, email_row, [])
        record(
            results,
            phase="pipeline",
            case_id="approval_card",
            name="Teams approval card builds",
            passed=card is not None and len(card.get("actions", [])) == 3,
            detail=str([a.get("title") for a in (card or {}).get("actions", [])]),
        )

        edited = "Hi Anju,\n\nUpdated draft for dry-run test.\n\nThank you,\nLexi"
        save = handle_teams_card_submit(
            {
                "action": CARD_ACTION_SAVE_DRAFT,
                "proposal_id": proposal_id,
                INPUT_DRAFT_ID: edited,
            }
        )
        record(
            results,
            phase="pipeline",
            case_id="save_draft",
            name="Save draft (Hermes card path)",
            passed=bool(save.get("ok")),
            detail=str(save.get("message", ""))[:80],
        )

        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT drafted_reply FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        record(
            results,
            phase="pipeline",
            case_id="save_persisted",
            name="Draft persisted in DB",
            passed=row and "Updated draft" in str(row["drafted_reply"]),
        )

        approve = execute_lexi_approval(
            proposal_id=proposal_id,
            decision="approved",
            selected_slot="",
            authorized_by="dry-run-test",
            decision_source="dry_run",
        )
        record(
            results,
            phase="pipeline",
            case_id="dry_send",
            name="Send offer (dry-run — no Outlook)",
            passed=approve.ok,
            detail=f"status={approve.status} email_sent={approve.email_sent}",
        )

        with get_lexi_connection() as conn:
            holds = conn.execute(
                "SELECT COUNT(*) AS c FROM holds WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        record(
            results,
            phase="pipeline",
            case_id="holds_db",
            name="Hold rows recorded (dry-run)",
            passed=holds and int(holds["c"]) >= 2,
            detail=f"holds={holds['c'] if holds else 0}",
        )

        discard = execute_lexi_approval(
            proposal_id=proposal_id,
            decision="rejected",
            selected_slot="",
            authorized_by="dry-run-test",
            decision_source="dry_run",
        )
        record(
            results,
            phase="pipeline",
            case_id="discard",
            name="Discard releases holds",
            passed=discard.ok and discard.status == "rejected",
            detail=f"holds_released={discard.holds_released}",
        )


def _fetch_bundle(proposal_id: int) -> dict[str, Any]:
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT p.*, e.subject, e.sender, e.raw_body
            FROM proposals p
            INNER JOIN email_threads e ON e.thread_id = p.thread_id
            WHERE p.id = ?
            """,
            (proposal_id,),
        ).fetchone()
    return dict(row) if row else {}


def phase_delegation(results: list[dict]) -> None:
    print("\n=== Phase 3: CC Lexi delegation detection ===")
    lexi_addr = _lexi_cc_address()
    for case in SYNTHETIC_CASES:
        cc = list(case.get("cc_recipients") or [])
        if case["id"] == "cc_lexi_intro" and not cc:
            cc = [lexi_addr]
        raw = {
            "subject": case["subject"],
            "body": case["body"],
            "sender": case["sender"],
            "cc_recipients": cc,
        }
        decision = detect_delegation(
            subject=case["subject"],
            body=case["body"],
            sender=case["sender"],
            raw_email=raw,
        )
        expect_delegate = case["id"] == "cc_lexi_intro"
        record(
            results,
            phase="delegation",
            case_id=case["id"],
            name="detect_delegation",
            passed=decision.is_delegation == expect_delegate,
            detail=f"delegation={decision.is_delegation} reason={decision.reason}",
        )


def phase_timezone(results: list[dict]) -> None:
    print("\n=== Phase 4: Timezone intelligence ===")
    samples = [
        ("bill.heermann@newportadvisors.co", "Denver family office", "known"),
        ("braden@nycapital.com", "I'm on the East Coast (ET)", "known"),
        ("unknown@weird-domain.zzz", "Can we meet next week?", "unknown"),
    ]
    for sender, body, expect_confidence in samples:
        tz = detect_recipient_timezone(sender_email=sender, body=body)
        if expect_confidence == "unknown":
            passed = tz.confidence == "unknown"
        else:
            passed = bool(tz.tz_name()) and tz.confidence != "unknown"
        record(
            results,
            phase="timezone",
            case_id=sender.split("@")[0][:20],
            name=f"TZ for {sender}",
            passed=passed,
            detail=f"{tz.tz_name()} source={tz.source} confidence={tz.confidence}",
        )


def phase_scenario_audit(results: list[dict]) -> None:
    print("\n=== Phase 5: Live scenario audit (12 cases) ===")
    script = ROOT / "scripts" / "audit_scheduling_scenarios.py"
    if not script.is_file():
        record(
            results,
            phase="scenarios",
            case_id="missing",
            name="audit_scheduling_scenarios.py",
            passed=False,
            detail="script not found",
        )
        return
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    report_path = ROOT / "docs" / "SCHEDULING_SCENARIO_AUDIT.json"
    passed = proc.returncode == 0
    detail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
    if report_path.is_file():
        try:
            audit = json.loads(report_path.read_text(encoding="utf-8"))
            summary = audit.get("summary") or {}
            passed = passed and int(summary.get("failed", 1)) == 0
            detail = [
                f"{summary.get('passed', '?')}/{summary.get('total', '?')} scenarios"
            ]
        except json.JSONDecodeError:
            passed = False
    record(
        results,
        phase="scenarios",
        case_id="audit",
        name="Scheduling scenario audit",
        passed=passed,
        detail=" | ".join(detail),
    )


def phase_unit_tests(results: list[dict]) -> None:
    print("\n=== Phase 6: Unit tests ===")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_meeting_type.py",
        "tests/test_slot_engine.py",
        "tests/test_pre_approval_gate.py",
        "tests/test_teams_card_submit.py",
        "tests/test_teams_cards.py",
        "tests/test_timezone_intel.py",
        "tests/test_validators.py",
        "-q",
        "--tb=no",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    passed = proc.returncode == 0
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
    record(
        results,
        phase="pytest",
        case_id="core",
        name="Core scheduling + Teams tests",
        passed=passed,
        detail=" | ".join(tail),
    )


def main() -> int:
    print("=" * 72)
    print("FINAL PRE-PRODUCTION DRY-RUN (no sends, no calendar writes, no Teams)")
    print("=" * 72)
    print(f"LEXI_DRY_RUN={settings.lexi_dry_run}  SUPPRESS_TEAMS={settings.lexi_suppress_teams_push}")

    results: list[dict[str, Any]] = []
    phase_safety(results)

    print("\n=== Loading calendar (Master + work Calendar) ===")
    calendar_context = load_scheduling_calendar_context()
    cal_ok = calendar_context.get("status") == "available"
    record(
        results,
        phase="calendar",
        case_id="load",
        name="Calendar context",
        passed=cal_ok,
        detail=f"{len(calendar_context.get('busy_events') or [])} blocking events",
    )
    if not cal_ok:
        print("FATAL: calendar unavailable")
    else:
        phase_inbox_scheduling(results, calendar_context)
        phase_full_pipeline(results, calendar_context)
        phase_delegation(results)
        phase_timezone(results)
        phase_scenario_audit(results)

    phase_unit_tests(results)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": {
            "lexi_dry_run": settings.lexi_dry_run,
            "teams_suppressed": settings.lexi_suppress_teams_push,
            "teams_connection": "NOT_STARTED — awaiting your go-live signal",
        },
        "summary": {"passed": passed, "failed": failed, "total": len(results)},
        "ready_for_teams_live": failed == 0,
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n{'=' * 72}")
    print(f"SUMMARY: {passed}/{len(results)} passed, {failed} failed")
    print(f"Report: {REPORT_PATH}")
    print(f"Ready for Teams live phase: {'YES' if failed == 0 else 'NO — fix failures first'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
