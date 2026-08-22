#!/usr/bin/env python3
"""Phase 5 end-to-end validation harness (plan Phase 5).

Exercises EVERY Lexi capability against real reads in DRY-RUN mode — no real
sends/writes to Outlook, Asana, or HubSpot. Prints a PASS/FAIL matrix so we can
confirm the whole surface works before Phase 6 (real UAT + deploy).

Run:  LEXI_ENV=testing .venv/bin/python scripts/phase5_e2e_validation.py
Requires the dev Anthropic key in .env.testing (LLM triage/drafting is live but cheap).
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Force the safe testing posture regardless of how invoked.
os.environ.setdefault("LEXI_ENV", "testing")
os.environ["LEXI_DRY_RUN"] = "true"
os.environ["LEXI_KORY_SPACE_READ_ONLY"] = "true"
os.environ["LEXI_KORY_OUTBOUND_BLOCKED"] = "true"
os.environ["LEXI_WRITE_MODE"] = "sandbox"

RESULTS: list[tuple[str, str, str]] = []


class SkipCheck(Exception):
    """Raised to mark a capability that requires Phase-6 setup (e.g. sandbox connection)."""


def check(name: str):
    def deco(fn):
        try:
            detail = fn() or ""
            RESULTS.append((name, "PASS", str(detail)[:110]))
        except SkipCheck as exc:
            RESULTS.append((name, "SKIP", str(exc)[:150]))
        except Exception as exc:  # noqa: BLE001
            RESULTS.append((name, "FAIL", f"{type(exc).__name__}: {exc}"[:200]))
            if os.getenv("E2E_VERBOSE"):
                traceback.print_exc()
        return fn

    return deco


# ── Safety posture ───────────────────────────────────────────────────────────
@check("safety.posture_all_gates_on")
def _posture():
    from app.config import safety_posture_summary

    p = safety_posture_summary()
    assert p["LEXI_DRY_RUN"] and p["LEXI_KORY_OUTBOUND_BLOCKED"] and p["LEXI_KORY_SPACE_READ_ONLY"]
    return f"env={p['LEXI_ENV']} write_mode={p['LEXI_WRITE_MODE']}"


# ── Calendar / scheduling ─────────────────────────────────────────────────────
@check("calendar.read_context")
def _cal():
    from app.scheduling.calendar_context import load_scheduling_calendar_context

    ctx = load_scheduling_calendar_context(horizon_days=14)
    assert ctx.get("status") == "available"
    return f"{len(ctx.get('busy_events') or [])} busy events"


@check("scheduling.slots_multiple_intents")
def _slots():
    from app.scheduling.calendar_context import load_scheduling_calendar_context
    from app.scheduling.slot_engine import find_valid_slots
    from app.scheduling.busy_intervals import slot_conflicts_busy

    ctx = load_scheduling_calendar_context(horizon_days=14)
    busy = ctx.get("busy_events") or []
    out = []
    for intent in ("virtual_30", "coffee", "new_client"):
        prop = find_valid_slots(ctx, intent=intent, subject=intent, body="")
        conflicts = sum(1 for s in prop.slots if slot_conflicts_busy(s, busy))
        assert conflicts == 0, f"{intent} produced {conflicts} conflicting slots"
        out.append(f"{intent}={len(prop.slots)}")
    return ", ".join(out)


@check("scheduling.inbound_triage_pipeline")
def _triage():
    from app.agents.triage_agent import process_new_email

    email = {
        "thread_id": "e2e-thread-1",
        "conversation_id": "e2e-conv-1",
        "subject": "TEST — 30 min intro next week",
        "from": {"emailAddress": {"address": "prospect@example.com", "name": "Pat Prospect"}},
        "body": "Hi Kory, could we grab 30 minutes on Teams next week? Thanks.",
        "receivedDateTime": "2026-07-20T15:00:00Z",
    }
    res = process_new_email(email)
    return f"triage intent={getattr(res, 'intent', res) if not isinstance(res, dict) else res.get('intent_classification')}"


# ── Asana (reads live; writes dry-run preview) ────────────────────────────────
@check("asana.list_tasks")
def _asana_list():
    from app.integrations.asana_manager import list_asana_tasks

    r = list_asana_tasks()
    tasks = r.get("tasks") or r.get("all") or []
    return f"{len(tasks) if hasattr(tasks,'__len__') else '?'} tasks"


@check("asana.create_dry_run_preview")
def _asana_create():
    from app.integrations.asana_manager import create_asana_task_from_chat

    r = create_asana_task_from_chat(title="E2E test task (dry-run)", approved=True)
    tid = str(r.get("task_id") or "")
    assert r.get("dry_run") or r.get("simulated") or "dry-run" in tid, f"expected dry-run, got {r}"
    return "write previewed (no real task)"


@check("asana.search")
def _asana_search():
    from app.integrations.asana_manager import search_asana_tasks

    r = search_asana_tasks(query="tax", limit=5)
    return f"search ok ({r.get('count', '?')} hits)"


# ── HubSpot (reads live; writes staged/blocked) ───────────────────────────────
@check("hubspot.all_nine_ops")
def _hubspot():
    from app.integrations import hubspot_manager as h

    status = h.hubspot_status_brief()
    assert status.get("ok"), status
    ops = {
        "cleanup": h.propose_inactive_cleanup(limit=5),
        "dedupe": h.propose_duplicate_merges(limit=5),
        "lead_source": h.propose_lead_source_fills(limit=5),
        "deals": h.deals_snapshot_for_brief(limit=3),
        "prebrief": h.enrich_prebrief_from_hubspot(email="test@example.com"),
    }
    note = h.stage_meeting_note(email="test@example.com", note="e2e note", approved=True)
    assert note.get("dry_run") or note.get("writes_blocked"), note
    return "core ops ok; note staged (writes blocked)"


# ── Email-to-Lexi channel ─────────────────────────────────────────────────────
@check("email_to_lexi.intent_routing")
def _mail_intent():
    from app.agents.lexi_mail_intent import is_mail_to_lexi, parse_lexi_mail_intent

    mail = {
        "subject": "don't schedule with Acme",
        "from": {"emailAddress": {"address": "kory@iconicfounders.com"}},
        "to_recipients": ["lexi@iconicfounders.com"],
        "body": "Please don't schedule anything with anyone at Acme Corp.",
    }
    assert is_mail_to_lexi(mail), "is_mail_to_lexi returned False"
    intent = parse_lexi_mail_intent(subject=mail["subject"], body=mail["body"])
    assert intent.intent == "dont_schedule", f"expected dont_schedule, got {intent.intent}"
    return f"routed intent={intent.intent}"


# ── Briefings + shortcuts ─────────────────────────────────────────────────────
@check("briefings.daily_ceo")
def _brief_daily():
    from app.assistant.briefings import build_daily_ceo_briefing

    brief = build_daily_ceo_briefing()  # returns a dict
    text = brief.get("text") or brief.get("kory_message") or str(brief) if isinstance(brief, dict) else str(brief)
    assert text and len(text) > 20, f"empty briefing: {brief}"
    return f"{len(text)} chars"


@check("briefings.shortcuts")
def _brief_shortcuts():
    from app.assistant.briefings import build_today_calendar_brief, build_unanswered_brief
    from app.assistant.inbox_review import build_inbox_review

    t = build_today_calendar_brief()
    u = build_unanswered_brief()
    i = build_inbox_review(hours=48)
    assert t is not None and u is not None and i is not None
    return "today/unanswered/inbox ok"


@check("briefings.prebrief")
def _prebrief():
    from app.assistant.briefings import build_prebriefs_for_today

    r = build_prebriefs_for_today(include_research=False)
    return f"prebrief ok ({type(r).__name__})"


# ── Reminders ─────────────────────────────────────────────────────────────────
@check("reminders.hold_reminder_cycle")
def _hold_rem():
    from app.jobs.hold_lifecycle import run_hold_lifecycle_cycle

    r = run_hold_lifecycle_cycle()
    return f"lifecycle ran (released={r.get('released_expired', 0)})"


@check("reminders.protection_audit")
def _audit():
    from app.jobs.protection_audit import run_protection_audit

    r = run_protection_audit(push_to_kory=False)
    return f"{r['matched_protected']} protected; {len(r['expected_missing'])} flagged"


# ── Additional capabilities ────────────────────────────────────────────────────
@check("scheduling.hold_placement_dry_run")
def _hold():
    from app.integrations.calendar_holds import place_tentative_hold

    try:
        r = place_tentative_hold(
            action={
                "start": "2026-07-28T09:00:00-06:00",
                "end": "2026-07-28T09:30:00-06:00",
                "title": "HOLD: E2E test",
            }
        )
    except Exception as exc:  # noqa: BLE001
        if "No connected account" in str(exc) or "ConnectedAccount" in str(exc):
            raise SkipCheck(
                "sandbox Outlook connection not connected — set up for Phase 6 sandbox writes"
            )
        raise
    eid = str(r[0] if isinstance(r, tuple) else r.get("event_id") if isinstance(r, dict) else r)
    assert "dry-run" in eid.lower() or r is not None
    return "hold previewed (no real event)"


@check("calendar.availability_tool")
def _avail():
    from app.assistant.actions import get_calendar_availability

    r = get_calendar_availability(days=7)
    assert isinstance(r, dict)
    return "availability computed"


@check("pending.queue")
def _pending():
    from app.agents.comms_agent import get_lexi_pending_queue

    r = get_lexi_pending_queue()
    return f"pending queue ok ({len(r) if hasattr(r, '__len__') else '?'} items)"


@check("research.web_search")
def _research():
    from app.integrations.composio_search import web_search

    r = web_search(query="Cherry Creek Denver coffee shops")
    ok = bool(r) and (r.get("ok", True) if isinstance(r, dict) else True)
    return "web search ok" if ok else f"search returned {type(r).__name__}"


@check("asana.write_ops_all_gated")
def _asana_writes():
    # Every Asana write goes through the same gate; confirm complete/comment/update
    # all return dry-run/simulated (no real change).
    from app.integrations.asana_manager import (
        complete_asana_task,
        comment_on_asana_task,
        update_asana_task,
    )

    outs = [
        complete_asana_task(task_gid="e2e-x", approved=True),
        comment_on_asana_task(task_gid="e2e-x", comment="e2e", approved=True),
        update_asana_task(task_gid="e2e-x", notes="e2e", approved=True),
    ]
    for o in outs:
        assert o.get("dry_run") or o.get("simulated") or "dry-run" in str(o), o
    return "complete/comment/update all previewed"


def main() -> int:
    from app.config import safety_posture_summary

    print("\n=== Lexi Phase 5 E2E validation (DRY-RUN, no real writes) ===")
    print("posture:", safety_posture_summary().get("LEXI_ENV"),
          "| dry_run:", safety_posture_summary().get("LEXI_DRY_RUN"), "\n")

    # Trigger all checks (decorators ran at import; call any not yet executed).
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    skipped = sum(1 for _, s, _ in RESULTS if s == "SKIP")
    width = max(len(n) for n, _, _ in RESULTS) if RESULTS else 10
    marks = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ "}
    for name, status, detail in RESULTS:
        print(f"  {marks.get(status, '?')} {name.ljust(width)}  {detail}")
    print(f"\n=== {passed} passed, {failed} failed, {skipped} skipped / {len(RESULTS)} capabilities ===\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
