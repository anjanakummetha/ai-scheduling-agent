"""Go-live readiness probe — READ-ONLY. Calls every real system Kory depends on.

Connection status saying ACTIVE is not the same as the integration working, so
this actually exercises each one and times it. Nothing here writes.

    .venv/bin/python scripts/go_live_readiness.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def probe(label, fn):
    started = time.time()
    try:
        detail = fn()
        print(f"  {label:24} OK    {time.time() - started:5.1f}s   {detail}")
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
        failures.append(label)
        print(f"  {label:24} FAIL  {time.time() - started:5.1f}s   "
              f"{type(exc).__name__}: {str(exc)[:100]}")


def outlook_calendar():
    from app.integrations.outlook_calendar import get_calendar_events

    events, _ = get_calendar_events("2026-08-06T00:00:00-06:00", "2026-08-07T00:00:00-06:00")
    return f"{len(events)} events today"


def outlook_mail():
    from app.integrations.composio_client import execute_tool

    r = execute_tool("OUTLOOK_LIST_MESSAGES", {"user_id": "me", "top": 3}, role="read")
    data = r.get("data") or {}
    return f"{len(data.get('value') or data.get('messages') or [])} recent messages"


def hubspot():
    from app.integrations import hubspot_manager as hs

    found = hs.search_contacts(limit=3, owner_id=hs.kory_owner_id())
    total = found.get("total")
    scope = f"of {total}" if total is not None else "of an unreported total"
    return f"{found.get('count')} {scope} Kory-owned contacts"


def asana():
    from app.integrations.asana_manager import list_asana_tasks

    result = list_asana_tasks(limit=5)
    tasks = result.get("tasks") if isinstance(result, dict) else result
    return f"{len(tasks or [])} task(s) readable"


def scheduling_engine():
    from app.scheduling.preferences import load_scheduling_preferences

    prefs = load_scheduling_preferences()
    return f"prefs loaded, {len(prefs.memory_facts or [])} memory fact(s)"


print("=== LIVE READ PROBES (read-only, hits each real system) ===")
probe("outlook calendar", outlook_calendar)
probe("outlook mail", outlook_mail)
probe("hubspot", hubspot)
probe("asana", asana)
probe("scheduling prefs", scheduling_engine)

print()
if failures:
    print(f"RESULT: {len(failures)} PROBE(S) FAILED -> {', '.join(failures)}")
    sys.exit(1)
print("RESULT: all probes passed")
