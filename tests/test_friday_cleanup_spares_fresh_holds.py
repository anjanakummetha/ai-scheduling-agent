"""The Friday sweep must not release a hold whose offer went out that Friday.

Kory's rule: by end of Friday, release next-week holds nobody answered — the
prospect had the business week. Live proposal 10563 (2026-08-22, a Friday
evening): Kory-side approval placed three holds at 9:35 PM MT and the sweep,
running one second later, released all three and deleted their calendar
events — right after Teams reported "3 holds placed". An offer approved ON
Friday has had no answer window; the sweep now only releases holds placed
before that Friday.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import app.jobs.hold_lifecycle as lifecycle
from app.scheduling.proposal_state import ProposalStatus
from app.storage.lexi_db import get_lexi_connection

MT = ZoneInfo("America/Denver")
THREAD = "friday-cleanup-thread"

# A Friday, 9:35 PM MT — exactly the live shape.
FRIDAY_EVENING_MT = datetime(2026, 8, 21, 21, 35, tzinfo=MT)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FRIDAY_EVENING_MT.astimezone(tz) if tz else FRIDAY_EVENING_MT.replace(tzinfo=None)


def _next_week_slot() -> str:
    monday = FRIDAY_EVENING_MT + timedelta(days=(7 - FRIDAY_EVENING_MT.weekday()))
    tuesday = monday.replace(hour=16, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return tuesday.isoformat()


@pytest.fixture
def offer_sent_proposal():
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id = ?", (THREAD,))
        conn.execute(
            "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender,"
            " sender_email, raw_body) VALUES (?,?,?,?,?)",
            (THREAD, "[TEST] friday cleanup", "Dana <dana@example.com>",
             "dana@example.com", "Coffee next week?"),
        )
        cur = conn.execute(
            "INSERT INTO proposals (thread_id, status, intent_classification,"
            " proposed_slots) VALUES (?,?,?,?)",
            (THREAD, ProposalStatus.OFFER_SENT, "referral_or_intro",
             json.dumps([{"start": _next_week_slot()}])),
        )
        pid = int(cur.lastrowid)
        conn.commit()
    yield pid
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM holds WHERE proposal_id = ?", (pid,))
        conn.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        conn.execute("DELETE FROM email_threads WHERE thread_id = ?", (THREAD,))
        conn.commit()


def _insert_hold(pid: int, *, created_at_utc: str) -> int:
    with get_lexi_connection() as conn:
        cur = conn.execute(
            "INSERT INTO holds (proposal_id, event_id, slot_start, slot_end,"
            " expires_at, created_at) VALUES (?,?,?,?,?,?)",
            (pid, f"evt-{created_at_utc}", _next_week_slot(),
             _next_week_slot(), "pending", created_at_utc),
        )
        conn.commit()
        return int(cur.lastrowid)


def _hold_state(hold_id: int) -> str:
    with get_lexi_connection() as conn:
        return conn.execute(
            "SELECT expires_at FROM holds WHERE id = ?", (hold_id,)
        ).fetchone()[0]


def _run_sweep():
    deleted: list[str] = []
    with patch.object(lifecycle, "datetime", _FrozenDatetime), \
         patch.object(lifecycle, "delete_calendar_event", side_effect=deleted.append):
        released = lifecycle._friday_cleanup_next_week_holds()
    return released, deleted


def test_a_hold_placed_that_friday_survives_the_sweep(offer_sent_proposal):
    # Placed 9:34 PM MT the same Friday = 03:34 UTC Saturday in SQLite's clock.
    fresh = _insert_hold(
        offer_sent_proposal,
        created_at_utc=(FRIDAY_EVENING_MT - timedelta(minutes=1))
        .astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
    )
    released, deleted = _run_sweep()
    assert released == 0, "the sweep released a hold placed minutes earlier"
    assert not deleted, "the sweep deleted a fresh hold's calendar event"
    assert _hold_state(fresh) == "pending"


def test_a_hold_that_sat_all_week_is_still_released(offer_sent_proposal):
    stale = _insert_hold(
        offer_sent_proposal,
        created_at_utc=(FRIDAY_EVENING_MT - timedelta(days=3))
        .astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
    )
    released, deleted = _run_sweep()
    assert released == 1, "the Friday rule stopped working entirely"
    assert len(deleted) == 1
    assert _hold_state(stale) == "released"


def test_an_unparseable_created_at_never_deletes_protection(offer_sent_proposal):
    odd = _insert_hold(offer_sent_proposal, created_at_utc="not-a-timestamp")
    released, deleted = _run_sweep()
    assert released == 0
    assert not deleted
    assert _hold_state(odd) == "pending"
