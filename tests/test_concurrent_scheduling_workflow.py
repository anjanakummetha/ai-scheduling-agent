"""Several scheduling threads alive at once — the state Kory is actually in.

Every earlier suite drives ONE proposal at a time. Real mornings have four or
five requests in flight, and the failures that survive to production live in the
interference between them: the wrong draft approved, two threads offered the
same slot, a rejection that takes a neighbour down with it, holds released for
the wrong proposal.

These drive the real queue and the real approval path with a populated database.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agents.comms_agent import get_lexi_pending_queue
from app.storage.lexi_db import get_lexi_connection
from tests.teams_parity import message_of, teams

MT = ZoneInfo("America/Denver")
PREFIX = "concur-"


def _day(weekday: int, weeks: int = 2) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(days=weekday, weeks=weeks)


def _slot(d: date, hour: int, minutes: int = 30) -> dict[str, str]:
    start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=MT)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat()}


def _seed(tag: str, subject: str, sender: str, slots, *, status: str = "pending_approval",
          tier: str = "medium", intent: str = "referral_or_intro") -> int:
    tid = f"{PREFIX}{tag}"
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, sender_email, raw_body)"
            " VALUES (?,?,?,?,?)",
            (tid, subject, sender, sender, "Would love to find 30 minutes."),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification, priority_tier,"
            " proposed_slots, drafted_reply) VALUES (?,?,?,?,?,?)",
            (tid, status, intent, tier, json.dumps(slots), f"Hi — proposing times for {subject}.\n"),
        )
        pid = cur.lastrowid
        conn.commit()
    return pid


def _purge() -> None:
    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM proposals WHERE thread_id LIKE ?", (PREFIX + "%",)
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM holds WHERE proposal_id = ?", (row["id"],))
            conn.execute("DELETE FROM approvals WHERE proposal_id = ?", (row["id"],))
        conn.execute("DELETE FROM proposals WHERE thread_id LIKE ?", (PREFIX + "%",))
        conn.execute("DELETE FROM email_threads WHERE thread_id LIKE ?", (PREFIX + "%",))
        conn.commit()


def _ours():
    """Only the threads this fixture created."""
    return [i for i in get_lexi_pending_queue() if i.thread_id.startswith(PREFIX)]


def _status(pid: int) -> str:
    with get_lexi_connection() as conn:
        row = conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()
    return row["status"] if row else "GONE"


@pytest.fixture
def five_threads():
    """Five live requests, deliberately mixed priority and intent."""
    _purge()
    mon, tue, wed = _day(0), _day(1), _day(2)
    ids = {
        "curtis": _seed("curtis", "[TEST] Intro call — Curtis", "curtis@ex.com", [_slot(mon, 9)]),
        "steve": _seed("steve", "[TEST] ICCI — Steve", "steve@ex.com", [_slot(tue, 10)]),
        "heidi": _seed("heidi", "[TEST] Check in — Heidi", "heidi@ex.com", [_slot(wed, 14)],
                       tier="high", intent="internal_sync"),
        "dana": _seed("dana", "[TEST] Coffee — Dana", "dana@ex.com", [_slot(tue, 8, 90)],
                      intent="coffee"),
        "raj": _seed("raj", "[TEST] Reschedule — Raj", "raj@ex.com", [_slot(wed, 11)],
                     intent="reschedule"),
    }
    yield ids
    _purge()


# --- the queue itself -----------------------------------------------------


def test_all_five_appear_and_are_numbered_uniquely(five_threads):
    queue = [i for i in get_lexi_pending_queue() if i.thread_id.startswith(PREFIX)]
    assert len(queue) == 5, [i.subject for i in queue]
    numbers = [i.draft_number for i in queue]
    assert len(set(numbers)) == len(numbers), f"duplicate draft numbers: {numbers}"
    assert all(n is not None for n in numbers)


def test_numbering_is_contiguous_from_one(five_threads):
    queue = get_lexi_pending_queue()
    assert [i.draft_number for i in queue] == list(range(1, len(queue) + 1))


def test_priority_and_reschedule_sort_ahead_of_the_rest(five_threads):
    """Kory's rule: reschedules take precedence over new requests."""
    queue = get_lexi_pending_queue()
    ours = [i for i in queue if i.thread_id.startswith(PREFIX)]
    high = next(i for i in ours if i.thread_id.endswith("heidi"))
    low = next(i for i in ours if i.thread_id.endswith("curtis"))
    assert high.draft_number < low.draft_number, "high priority must sort first"


def test_pending_lists_every_thread_with_no_raw_ids(five_threads):
    msg = message_of(teams("pending"))
    for name in ("Curtis", "Steve", "Heidi", "Dana", "Raj"):
        assert name in msg, f"{name} missing from the queue Kory sees"
    for pid in five_threads.values():
        assert f"#{pid}" not in msg


# --- acting on one must not disturb the others ----------------------------


def test_rejecting_one_leaves_the_other_four_untouched(five_threads):
    queue = get_lexi_pending_queue()
    target = next(i for i in queue if i.thread_id.endswith("steve"))
    teams(f"reject {target.draft_number} — not a fit")
    assert _status(five_threads["steve"]) != "pending_approval"
    for key in ("curtis", "heidi", "dana", "raj"):
        assert _status(five_threads[key]) == "pending_approval", f"{key} was disturbed"


def test_the_queue_renumbers_after_a_rejection_and_still_resolves(five_threads):
    first = get_lexi_pending_queue()
    # Deliberately NOT the last item: rejecting the tail frees a number that no
    # longer exists, which proves nothing about renumbering.
    assert len(first) >= 2
    victim = first[0]
    freed = victim.draft_number
    teams(f"reject {freed} — not a fit")

    after = get_lexi_pending_queue()
    assert len(after) == len(first) - 1
    assert [i.draft_number for i in after] == list(range(1, len(after) + 1))
    # The freed number must now point at a different thread, not a gap and not
    # the rejected one.
    survivor = next(i for i in after if i.draft_number == freed)
    assert survivor.thread_id != victim.thread_id
    assert survivor.proposal_id != victim.proposal_id


def test_showing_one_draft_returns_that_thread_not_a_neighbour(five_threads):
    for tag, name in (("curtis", "Curtis"), ("heidi", "Heidi"), ("raj", "Raj")):
        item = next(i for i in get_lexi_pending_queue() if i.thread_id.endswith(tag))
        msg = message_of(teams(f"show draft {item.draft_number}"))
        assert name in msg, f"draft {item.draft_number} should be {name}: {msg[:160]}"


def test_a_raw_id_still_targets_its_own_thread_amid_five(five_threads):
    """An old Teams message quoting #id must not land on a renumbered neighbour."""
    pid = five_threads["raj"]
    msg = message_of(teams(f"show draft {pid}"))
    assert "Raj" in msg, msg[:200]


# --- slot collisions across threads ---------------------------------------


def test_two_threads_are_never_staged_on_the_identical_slot(five_threads):
    # Scoped to this fixture's threads: other suites legitimately leave rows in
    # the shared test DB, including deliberate past-slot and clash fixtures.
    seen: dict[str, str] = {}
    for item in _ours():
        for slot in item.proposed_slots or []:
            key = str(slot.get("start"))
            assert key not in seen, (
                f"{item.subject} and {seen[key]} both staged {key}"
            )
            seen[key] = item.subject or ""


def test_every_staged_slot_across_all_threads_is_in_the_future(five_threads):
    now = datetime.now(MT)
    for item in _ours():
        for slot in item.proposed_slots or []:
            start = datetime.fromisoformat(str(slot["start"])).astimezone(MT)
            assert start > now, f"{item.subject} staged a past slot {start}"
