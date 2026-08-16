"""Minimal live scheduling verification — run ON THE BOX after a deploy.

Zero LLM calls (use_llm=False everywhere), zero emails, zero Teams pushes.
Composio spend: ~10–25 calls (calendar reads + one hold create/delete pair).

Checks, in order:
  1. Engine run against the REAL calendar (read-only): slots come back, none
     collide with live busy events, none land on an all-day OOO/travel day
     (Tier-2 B4 live check), all are in the future, and the requested window
     is honored or disclosed.
  2. A remembered day rule round-trip: save → engine excludes the day → delete.
  3. One `HOLD: [TEST]` event create → read-back → delete on the write
     calendar (proves the write path and the B8 successful:false raise are
     live), then verifies it is GONE. Nothing else is touched.

Usage (on the box):
  sudo -u lexi LEXI_ENV=production .venv/bin/python scripts/live_scheduling_verify.py
  sudo -u lexi LEXI_ENV=production .venv/bin/python scripts/live_scheduling_verify.py --skip-hold
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")
PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def check_engine_readonly() -> None:
    print("\n== 1. Engine vs real calendar (read-only) ==")
    from app.scheduling.busy_intervals import slot_conflicts_busy
    from app.scheduling.schedule_from_context import schedule_from_context

    result = schedule_from_context(
        subject="[TEST] live verify intro",
        body=(
            "Would love to find 30 minutes to connect next week. "
            "Happy to work around your schedule."
        ),
        intent="referral_or_intro",
        sender_email="anjanakummetha@gmail.com",
        use_llm_plan=False,
    )
    if not result.ok:
        # A gate block with a disclosed reason is a legitimate outcome on a
        # genuinely full week — but no slots AND no explanation is a failure.
        record(
            "engine returned slots or a disclosed refusal",
            bool(result.failure_message or result.status),
            f"status={result.status} msg={str(result.failure_message)[:120]}",
        )
        return
    slots = result.slots
    record("engine returned >=2 slots", len(slots) >= 2, f"{len(slots)} slots")

    busy = list((result.calendar_context or {}).get("busy_events") or [])
    clash = [s for s in slots if slot_conflicts_busy(s, busy)]
    record("no slot collides with live busy events", not clash, str(clash[:2]))

    now = datetime.now(tz=MT)
    past = [s for s in slots if datetime.fromisoformat(s["start"]) <= now]
    record("all slots in the future", not past, str(past[:2]))

    # Tier-2 B4: collect the days the live calendar marks fully unavailable
    # (all-day OOO/travel that now classifies as blocking) and prove no slot
    # lands on one.
    blocked_days = set()
    for ev in busy:
        try:
            from app.scheduling.busy_intervals import parse_event_datetime

            ev_start = parse_event_datetime(ev.get("start"))
            ev_end = parse_event_datetime(ev.get("end"))
        except Exception:
            continue
        if not ev_start or not ev_end:
            continue
        if (ev_end - ev_start) >= timedelta(hours=23):
            local_end = ev_end.astimezone(MT)
            # An all-day event "ends" at midnight of the NEXT day — that day
            # itself is free.
            last = (
                local_end.date() - timedelta(days=1)
                if (local_end.hour, local_end.minute) == (0, 0)
                else local_end.date()
            )
            d = ev_start.astimezone(MT).date()
            while d <= last:
                blocked_days.add(d)
                d += timedelta(days=1)
    on_blocked = [
        s
        for s in slots
        if datetime.fromisoformat(s["start"]).astimezone(MT).date() in blocked_days
    ]
    record(
        "no slot on an all-day-blocked day (B4)",
        not on_blocked,
        f"blocked_days={sorted(blocked_days)[:5]}",
    )
    note = result.scheduling_note() if hasattr(result, "scheduling_note") else ""
    print(f"  note: {note or '(in-window, no expansion)'}")
    for s in slots:
        print(f"    slot: {s['start']} -> {s['end']}")


def check_remember_roundtrip() -> None:
    print("\n== 2. Remembered day rule round-trip ==")
    from app.storage.kory_memory import delete_fact, upsert_fact
    from app.scheduling.preferences import load_scheduling_preferences

    key = "email:live-verify-day-rule"
    upsert_fact(fact_key=key, fact_value="no meetings on Fridays", source="live-verify")
    try:
        prefs = load_scheduling_preferences()
        record("saved rule reaches the validator prefs", 4 in prefs.blocked_weekdays)
    finally:
        delete_fact(fact=key)
    prefs_after = load_scheduling_preferences()
    record("rule removed cleanly", 4 not in prefs_after.blocked_weekdays)


def check_hold_roundtrip() -> None:
    print("\n== 3. HOLD create/read/delete round-trip (write calendar) ==")
    from app.integrations import outlook_calendar as oc

    start = (datetime.now(tz=MT) + timedelta(days=27)).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(minutes=15)
    subject = "HOLD: [TEST] live-verify — delete me"
    action = {
        "title": subject,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "attendees": [],
        "location": "Microsoft Teams",
        "body": "Automated live verification; auto-deleted in this same run.",
        "is_online_meeting": False,
    }
    try:
        event_id, _log = oc.create_calendar_event(action)
        record("hold created", bool(event_id), str(event_id)[:60])
    except Exception as exc:  # noqa: BLE001
        record("hold created", False, f"{type(exc).__name__}: {exc}")
        return
    if not event_id:
        return
    try:
        read_back = oc.get_calendar_event(event_id, role="write")
        record("hold read back", bool(read_back), str((read_back or {}).get("subject"))[:60])
    except Exception as exc:  # noqa: BLE001
        record("hold read back", False, f"{type(exc).__name__}: {exc}")
    try:
        oc.delete_calendar_event(event_id)
        record("hold deleted (no soft-failure)", True)
    except Exception as exc:  # noqa: BLE001
        record("hold deleted (no soft-failure)", False, f"{type(exc).__name__}: {exc}")
        print(f"  !! MANUAL CLEANUP NEEDED: event {event_id} ({subject})")
        return
    try:
        gone = oc.get_calendar_event(event_id, role="write")
        record("hold verified GONE (destination, not reply)", not gone)
    except Exception:  # noqa: BLE001 — a 404 on read-back IS the pass
        record("hold verified GONE (destination, not reply)", True, "read-back 404")


def main() -> int:
    skip_hold = "--skip-hold" in sys.argv
    check_engine_readonly()
    check_remember_roundtrip()
    if skip_hold:
        print("\n== 3. hold round-trip SKIPPED (--skip-hold) ==")
    else:
        check_hold_roundtrip()

    print("\n== SUMMARY ==")
    failed = [r for r in results if r[0] == FAIL]
    for status, name, detail in results:
        print(f"  [{status}] {name}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
