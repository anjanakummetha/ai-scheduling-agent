"""Final full-flow live E2E — run ON THE BOX. LT-FF (2026-08-16).

Drives the REAL pipeline below the model, against the real calendar and the
real send path, mirroring the Curtis thread:

  1. Inbound ask ("Thursday or Friday <next-week dates>") -> engine stages
     an offer (pending_approval).
  2. Kory guidance "Either day works at 9 mountain" -> retry restages BOTH
     days at 9:00.
  3. Approve -> email SENDS (to anjanakummetha@gmail.com ONLY) -> holds
     placed -> verified by reading the calendar back.
  4. Counterpart reply picks a day -> pending_invite.
  5. Cleanup: release holds (verified GONE on the calendar), delete rows.

Teams pushes are monkeypatched to no-ops so Kory sees nothing. The invite
phase is NOT executed (it would put a real meeting on the calendar); the
pick-parse in step 4 is the assertion that matters.

Usage:
  sudo -u lexi env LEXI_ENV=production PYTHONPATH=$PWD .venv/bin/python \
      scripts/live_full_flow_e2e.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")
RECIPIENT = "anjanakummetha@gmail.com"
STAMP = time.strftime("%Y%m%d%H%M%S")
THREAD_ID = f"lexi-outbound-test-ltff-{STAMP}"
SUBJECT = f"[TEST] Quick call? — LT-FF-{STAMP}"

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _mute_teams() -> None:
    import app.bot.teams_publisher as tp

    for fn in (
        "schedule_teams_approval_push",
        "schedule_teams_reply_prompt_push",
        "schedule_teams_invite_prompt_push",
        "schedule_teams_reoffer_prompt_push",
        "schedule_teams_scheduling_guidance_push",
        "schedule_teams_hold_reminder_push",
    ):
        if hasattr(tp, fn):
            setattr(tp, fn, lambda *a, **k: None)


def main() -> int:
    _mute_teams()
    from app.storage.lexi_db import get_lexi_connection

    # Next week's Thursday/Friday, so the ask is always future-dated.
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    thu, fri = monday + timedelta(days=3), monday + timedelta(days=4)
    body = (
        "Thanks for reaching out! I'd love to catch up. Could we schedule a "
        f"call for Thursday or Friday {thu.strftime('%B')} {thu.day} or "
        f"{fri.day}? Let me know."
    )

    proposal_id = None
    try:
        # ── 1. Stage the inbound ask through the real engine ──
        with get_lexi_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_threads (thread_id, conversation_id, "
                "subject, sender, sender_email, raw_body) VALUES (?, ?, ?, ?, ?, ?)",
                (THREAD_ID, THREAD_ID, SUBJECT, f"Anjana Kummetha <{RECIPIENT}>", RECIPIENT, body),
            )
            cur = conn.execute(
                "INSERT INTO proposals (thread_id, status, intent_classification, "
                "voice_mode, send_channel, recipient_timezone) "
                "VALUES (?, 'pending_triage', 'referral_or_intro', 'lexi', 'lexi', "
                "'America/Denver')",
                (THREAD_ID,),
            )
            proposal_id = cur.lastrowid
            conn.commit()

        from app.agents.scheduler_agent import process_proposal_schedule

        staged = process_proposal_schedule(proposal_id)
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT status, proposed_slots, drafted_reply FROM proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
            # keep the worker's own push cycle away from this proposal
            conn.execute(
                "UPDATE proposals SET teams_approval_notified_at=datetime('now') WHERE id=?",
                (proposal_id,),
            )
            conn.commit()
        record(
            "1. engine staged the ask",
            bool(staged) and row["status"] == "pending_approval",
            f"status={row['status']}",
        )
        slots = json.loads(row["proposed_slots"] or "[]")
        in_window = all(
            thu <= datetime.fromisoformat(s["start"]).astimezone(MT).date() <= fri
            for s in slots
        )
        record("1b. slots within the asked days", bool(slots) and in_window,
               str([s["start"] for s in slots]))

        # ── 2. Kory's real shorthand guidance ──
        from app.agents.inbound_reply import retry_scheduling_with_guidance

        r = retry_scheduling_with_guidance(
            proposal_id, "That's perfect. Either day works at 9 mountain."
        )
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT status, proposed_slots, drafted_reply FROM proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
            conn.execute(
                "UPDATE proposals SET teams_approval_notified_at=datetime('now') WHERE id=?",
                (proposal_id,),
            )
            conn.commit()
        slots = json.loads(row["proposed_slots"] or "[]")
        starts = sorted(s["start"] for s in slots)
        allowed = {f"{thu}T09:00:00-06:00", f"{fri}T09:00:00-06:00"}
        ok2 = bool(starts) and r.get("ok") and all(s in allowed for s in starts)
        note = "both days" if set(starts) == allowed else "one day (other busy live)"
        record(
            "2. '9 mountain' guidance staged 9:00 on the asked day(s)",
            ok2,
            f"staged={starts} [{note}]",
        )
        draft_ok = row["drafted_reply"] and "9:00" in row["drafted_reply"]
        record("2b. draft offers 9:00", bool(draft_ok))

        # ── 3. Approve -> real send to Anjana + holds ──
        from app.agents.comms_agent import execute_lexi_approval

        result = execute_lexi_approval(
            proposal_id=proposal_id,
            decision="approved",
            selected_slot=starts[0],
            authorized_by="kory",
            decision_source="live_e2e_test",
        )
        record(
            "3. approve sent the offer",
            result.ok and result.email_sent and result.status == "offer_sent",
            f"errors={result.errors}",
        )
        placed = result.holds_placed_times or []
        record("3b. holds placed for every offered time", len(placed) == len(starts), str(placed))

        # verify against the destination: hold events really on the calendar
        from app.integrations import outlook_calendar as oc

        with get_lexi_connection() as conn:
            hold_rows = conn.execute(
                "SELECT event_id FROM holds WHERE proposal_id=? "
                "AND expires_at != 'released'",
                (proposal_id,),
            ).fetchall()
        event_ids = [h["event_id"] for h in hold_rows if h["event_id"]]
        on_calendar = 0
        for eid in event_ids:
            try:
                if oc.get_calendar_event(eid, role="write"):
                    on_calendar += 1
            except Exception:
                pass
        record("3c. hold events verified ON the calendar", on_calendar == len(event_ids) == len(starts),
               f"{on_calendar}/{len(event_ids)}")

        # ── 4. Counterpart reply picks a day (Heidi-style, no send) ──
        from app.agents.lexi_thread_followup import try_handle_lexi_thread_followup

        reply = {
            "message_id": f"{THREAD_ID}-reply1",
            # The reply belongs to the SAME thread as the offer. Giving it a
            # "-reply1" thread_id made the follow-up handler resolve a
            # different proposal, so the pick was written somewhere the driver
            # never looked and step 4 reported a failure that production does
            # not have (tests/test_reply_accepts_offered_slot.py drives this
            # path hermetically and passes on every phrasing).
            "thread_id": THREAD_ID,
            "conversation_id": THREAD_ID,
            "subject": f"Re: {SUBJECT}",
            "sender": RECIPIENT,
            "raw_body": (
                f"{datetime.fromisoformat(starts[0]).strftime('%A')} the "
                f"{datetime.fromisoformat(starts[0]).day} at 9 works for me. "
                "Looking forward to it!"
            ),
        }
        handled = try_handle_lexi_thread_followup(reply) or {}
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT status, recipient_selected_slot FROM proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        raw_pick = row["recipient_selected_slot"] or ""
        try:  # stored as a JSON slot dict, not a bare ISO string
            picked = str(json.loads(raw_pick).get("start", ""))
        except (TypeError, ValueError):
            picked = str(raw_pick)
        record(
            "4. reply parsed as the 9:00 pick",
            row["status"] == "pending_invite" and picked == starts[0],
            f"status={row['status']} picked={picked[:25]} action={handled.get('action')}",
        )

    finally:
        # ── 5. Cleanup: release holds, verify gone, delete rows ──
        print("\n== cleanup ==")
        try:
            from app.integrations import outlook_calendar as oc
            from app.storage.lexi_db import get_lexi_connection

            with get_lexi_connection() as conn:
                hold_rows = conn.execute(
                    "SELECT event_id FROM holds WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchall() if proposal_id else []
            gone = 0
            ids = [h["event_id"] for h in hold_rows if h["event_id"]]
            for eid in ids:
                try:
                    oc.delete_calendar_event(eid)
                except Exception as exc:  # noqa: BLE001
                    print(f"  !! delete failed for {eid[:30]}: {exc}")
            for eid in ids:
                try:
                    if not oc.get_calendar_event(eid, role="write"):
                        gone += 1
                except Exception:
                    gone += 1
            record("5. holds deleted and verified GONE", gone == len(ids), f"{gone}/{len(ids)}")
            with get_lexi_connection() as conn:
                if proposal_id:
                    conn.execute("DELETE FROM holds WHERE proposal_id=?", (proposal_id,))
                    conn.execute("DELETE FROM approvals WHERE proposal_id=?", (proposal_id,))
                    conn.execute("DELETE FROM proposals WHERE id=?", (proposal_id,))
                conn.execute("DELETE FROM email_threads WHERE thread_id=?", (THREAD_ID,))
                conn.commit()
            record("5b. DB rows removed", True)
        except Exception as exc:  # noqa: BLE001
            record("5. cleanup", False, f"{type(exc).__name__}: {exc}")

    print("\n== SUMMARY ==")
    failed = [r for r in results if r[0] == "FAIL"]
    for status, name, _ in results:
        print(f"  [{status}] {name}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed  (proposal {proposal_id})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
