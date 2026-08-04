"""Live C-2 defects: "this week or next" collapsed to this week, "early
morning, even 7 AM" got an 8:00 floor (branch-order bug), and the delegation
path (hermes_orchestrator) never persisted the gate's scheduling_note."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.scheduling.scheduling_window import (
    infer_scheduling_window,
    infer_time_of_day_window,
)

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 4, 9, 0, tzinfo=MT)  # Tuesday


class TestThisWeekOrNext:
    def test_compound_phrase_spans_both_weeks(self):
        window = infer_scheduling_window(
            body="could we find 30 minutes early morning one day this week or next?",
            now=NOW,
        )
        assert window is not None
        assert window.label == "this week or next"
        assert window.start == date(2026, 8, 5)  # tomorrow (skip today)
        assert window.end == date(2026, 8, 16)  # end of NEXT week

    def test_or_next_week_variant(self):
        window = infer_scheduling_window(body="sometime this week or next week?", now=NOW)
        assert window is not None
        assert window.end == date(2026, 8, 16)

    def test_bare_this_week_unchanged(self):
        window = infer_scheduling_window(body="can we meet this week?", now=NOW)
        assert window is not None
        assert window.label == "this week"
        assert window.end == date(2026, 8, 9)

    def test_bare_next_week_unchanged(self):
        window = infer_scheduling_window(body="sometime next week?", now=NOW)
        assert window is not None
        assert window.label == "next week"
        assert window.start == date(2026, 8, 10)
        assert window.end == date(2026, 8, 16)


class TestEarlyMorningWindow:
    def test_early_morning_starts_at_seven(self):
        window = infer_time_of_day_window(body="early morning works best for us")
        assert window is not None
        assert window.label == "early morning"
        assert (window.start_hour, window.start_minute) == (7, 0)

    def test_even_seven_am_lowers_generic_mornings(self):
        window = infer_time_of_day_window(
            body="mornings work best — even 7 AM works for us"
        )
        assert window is not None
        assert (window.start_hour, window.start_minute) == (7, 0)

    def test_explicit_six_am_floors_at_seven(self):
        window = infer_time_of_day_window(body="early morning, even 6 AM is fine")
        assert window is not None
        assert (window.start_hour, window.start_minute) == (7, 0)

    def test_later_am_mention_never_shrinks(self):
        window = infer_time_of_day_window(body="mornings, ideally 10 AM")
        assert window is not None
        assert (window.start_hour, window.start_minute) == (8, 0)

    def test_c2_live_phrase_end_to_end(self):
        window = infer_time_of_day_window(
            body=(
                "could we find 30 minutes early morning one day this week or next? "
                "The earlier the better — even 7 AM works for us."
            )
        )
        assert window is not None
        assert (window.start_hour, window.start_minute) == (7, 0)


class TestSchedulingNoteOnDelegationPath:
    def test_result_scheduling_note_from_gate_warnings(self):
        from app.scheduling.pre_approval_gate import PreApprovalReport
        from app.scheduling.schedule_from_context import ScheduleFromContextResult

        gate = PreApprovalReport(ok=True)
        gate.warnings = ["no availability for this week or next — offering week of August 17 instead"]
        result = ScheduleFromContextResult(ok=True, gate=gate)
        assert "offering week of August 17" in result.scheduling_note()

    def test_persist_proposal_draft_stores_note(self, tmp_path, monkeypatch):
        import sqlite3

        from app.scheduling import hermes_orchestrator as ho

        db = tmp_path / "t.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE proposals (id INTEGER PRIMARY KEY, status TEXT, "
            "drafted_reply TEXT, proposed_slots TEXT, voice_mode TEXT, "
            "recipient_timezone TEXT, scheduling_note TEXT, updated_at TEXT)"
        )
        conn.execute("INSERT INTO proposals (id, status) VALUES (1, 'triaged')")
        conn.commit()
        conn.close()

        def _conn():
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            return c

        monkeypatch.setattr(ho, "get_lexi_connection", _conn)
        ho._persist_proposal_draft(
            1,
            draft="Hi",
            slots=[{"start": "s", "end": "e"}],
            voice_mode="lexi",
            recipient_timezone=None,
            scheduling_note="no availability for this week — offering week of August 17 instead",
        )
        with _conn() as check:
            row = check.execute("SELECT scheduling_note FROM proposals WHERE id=1").fetchone()
        assert "offering week of August 17" in row["scheduling_note"]

    def test_persist_clears_stale_note_when_clean(self, tmp_path, monkeypatch):
        import sqlite3

        from app.scheduling import hermes_orchestrator as ho

        db = tmp_path / "t.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE proposals (id INTEGER PRIMARY KEY, status TEXT, "
            "drafted_reply TEXT, proposed_slots TEXT, voice_mode TEXT, "
            "recipient_timezone TEXT, scheduling_note TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO proposals (id, status, scheduling_note) VALUES (1, 'triaged', 'stale warning')"
        )
        conn.commit()
        conn.close()

        def _conn():
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            return c

        monkeypatch.setattr(ho, "get_lexi_connection", _conn)
        ho._persist_proposal_draft(
            1,
            draft="Hi",
            slots=[],
            voice_mode="lexi",
            recipient_timezone=None,
            scheduling_note="",
        )
        with _conn() as check:
            row = check.execute("SELECT scheduling_note FROM proposals WHERE id=1").fetchone()
        assert row["scheduling_note"] is None


class TestPolicyBlockReason:
    """Live C-3 defect: a lunch escalation blamed the calendar and offered to
    move Kory's meetings, when his own exception-only rule was the cause."""

    def test_lunch_blocked_by_default_rule(self):
        from app.scheduling.hermes_compose import _policy_block_reason

        reason = _policy_block_reason("lunch")
        assert "exception-only" in reason
        assert "regardless of calendar availability" in reason

    def test_non_lunch_types_have_no_policy_reason(self):
        from app.scheduling.hermes_compose import _policy_block_reason

        assert _policy_block_reason("referral_or_intro") == ""
        assert _policy_block_reason("coffee") == ""

    def test_lunch_allowed_by_memory_clears_reason(self, monkeypatch):
        from app.scheduling import hermes_compose
        from app.scheduling.preferences import SchedulingPreferences

        monkeypatch.setattr(
            "app.scheduling.preferences.load_scheduling_preferences",
            lambda: SchedulingPreferences(lunch_allowed=True),
        )
        assert hermes_compose._policy_block_reason("lunch") == ""
