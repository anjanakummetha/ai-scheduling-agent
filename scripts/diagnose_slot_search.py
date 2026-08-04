"""Replay the slot search for a subject/body and show WHY each candidate was
accepted or rejected, day by day. Read-only: live calendar reads, no writes.

Usage (on the box):
  .venv/bin/python scripts/diagnose_slot_search.py \
      --subject "[TEST] Intro call — LT-C1" \
      --body "Hi Kory — would love to grab 30 minutes for a quick intro video call sometime next week." \
      --intent referral_or_intro
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scheduling import slot_engine as se
from app.scheduling.busy_intervals import parse_event_datetime, slot_conflicts_busy
from app.scheduling.calendar_context import load_scheduling_calendar_context
from app.scheduling.scheduling_plan import build_scheduling_plan
from app.scheduling.scheduling_window import infer_scheduling_window
from app.rules.validators import validate_proposal_slots


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--intent", default="referral_or_intro")
    ap.add_argument(
        "--days",
        default="",
        help="comma-separated YYYY-MM-DD to trace (default: every day of the inferred window)",
    )
    args = ap.parse_args()

    ctx = load_scheduling_calendar_context(subject=args.subject, body=args.body)
    print(f"calendar status: {ctx.get('status')} | horizon_days: {ctx.get('horizon_days')}")

    plan = build_scheduling_plan(
        subject=args.subject, body=args.body, intent=args.intent, use_llm=False
    )
    now_mt = datetime.now(tz=se.MT)
    window = (plan.window if plan and plan.window else None) or infer_scheduling_window(
        subject=args.subject, body=args.body, now=now_mt
    )
    print(f"plan.window: {plan.window.label if plan and plan.window else None}")
    if window:
        print(f"effective window: '{window.label}' {window.start} .. {window.end} (source={window.source})")
    else:
        print("effective window: NONE (no constraint)")

    intent_key = se._normalize_intent(args.intent, subject=args.subject, body=args.body)
    spec = se.resolve_meeting_type(intent=args.intent, subject=args.subject, body=args.body)
    fmt = se.infer_meeting_format(intent_key, subject=args.subject, body=args.body)
    print(
        f"intent_key: {intent_key} | format: {fmt} | offer: {spec.duration_minutes}m "
        f"| reserve: {spec.calendar_block_minutes}m | type: {spec.type_key}"
    )
    time_window = se.infer_time_of_day_window(subject=args.subject, body=args.body)
    print(f"time-of-day window: {time_window}")
    earliest = now_mt + timedelta(hours=2)
    print(f"earliest allowed start: {earliest.isoformat()}")

    busy = list(ctx.get("busy_events") or [])
    prefs = se.load_scheduling_preferences()

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    if not days and window:
        cursor = window.start
        while cursor <= window.end:
            days.append(cursor.isoformat())
            cursor += timedelta(days=1)

    for day_str in days:
        day = datetime.fromisoformat(day_str).replace(
            tzinfo=se.MT, hour=0, minute=0, second=0, microsecond=0
        )
        print(f"\n=== {day_str} ({day.strftime('%A')}) ===")
        day_events = []
        for event in busy:
            start = parse_event_datetime(event.get("start"))
            end = parse_event_datetime(event.get("end"))
            if start and start.astimezone(se.MT).date().isoformat() == day_str:
                day_events.append((start.astimezone(se.MT), end.astimezone(se.MT) if end else None, event))
        if day_events:
            print("  busy events the engine sees:")
            for start, end, event in sorted(day_events, key=lambda item: item[0]):
                subject = str(event.get("subject") or event.get("summary") or "(no title)")[:60]
                show_as = event.get("showAs") or event.get("show_as") or "?"
                end_text = end.strftime("%H:%M") if end else "?"
                print(f"    {start.strftime('%H:%M')}-{end_text}  [{show_as}]  {subject}")
        else:
            print("  busy events the engine sees: none")

        candidates = se._candidate_start_times(
            day, intent_key, fmt, east_coast=False, urgent=False, flexible_afternoon=False
        )
        if not candidates:
            print("  candidate starts: NONE (day ruled unavailable by DAILY_AVAILABILITY)")
            continue
        for start_local in candidates:
            slot = se._slot_dict(start_local, spec.duration_minutes)
            rejects: list[str] = []
            if start_local < earliest:
                rejects.append("before now+2h")
            if time_window and not se.slot_start_in_time_window(
                start_local, time_window, block_minutes=spec.calendar_block_minutes
            ):
                rejects.append("outside time-of-day window")
            if slot_conflicts_busy(slot, busy, reserve_minutes=spec.calendar_block_minutes):
                rejects.append(f"busy-conflict (reserve={spec.calendar_block_minutes}m)")
            check = validate_proposal_slots(
                [slot],
                intent=intent_key,
                meeting_format=fmt,
                urgent=False,
                east_coast=False,
                busy_events=busy,
                preferences=prefs,
            )
            if not check.valid:
                rejects.append("validator: " + "; ".join(check.violations))
            verdict = "OK" if not rejects else "REJECT: " + " | ".join(rejects)
            print(f"  {start_local.strftime('%H:%M')} -> {verdict}")

    engine = se.propose_meeting_slots(
        ctx, intent=args.intent, subject=args.subject, body=args.body, plan=plan
    )
    print("\n=== ENGINE RESULT ===")
    print("slots:", json.dumps(engine.slots, indent=2, default=str))
    print("diagnostics:", json.dumps(engine.diagnostics, indent=2, default=str))


if __name__ == "__main__":
    main()
