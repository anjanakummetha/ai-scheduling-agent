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
            lambda guidance="": SchedulingPreferences(lunch_allowed=True),
        )
        assert hermes_compose._policy_block_reason("lunch") == ""


class TestRetryAcceptsEscalatedStatus:
    """Live I-2 defect: escalation writes status=needs_kory, but the retry tool
    only accepted needs_scheduling_guidance — Kory's guidance could never be
    applied to the proposals the escalation flow produced."""

    def test_needs_kory_passes_the_status_gate(self, monkeypatch):
        from app.agents import inbound_reply as ir

        monkeypatch.setattr(
            ir, "_fetch_proposal_bundle", lambda pid: {"status": "needs_kory"}
        )
        calls = {}

        def _no_db(*a, **k):
            raise RuntimeError("stop-before-db")

        monkeypatch.setattr(ir, "get_lexi_connection", _no_db)
        try:
            ir.retry_scheduling_with_guidance(1, "lunch approved")
        except RuntimeError as exc:
            calls["reached_db"] = str(exc) == "stop-before-db"
        assert calls.get("reached_db"), "needs_kory must pass the status gate"

    def test_rejected_passes_the_status_gate(self, monkeypatch):
        """'reject #N' then 'redo it' must route through the real pipeline —
        when the gate refused rejected proposals, Hermes hand-composed an
        unstaged draft in chat with no id and no approval path (2026-08-05)."""
        from app.agents import inbound_reply as ir

        monkeypatch.setattr(
            ir, "_fetch_proposal_bundle", lambda pid: {"status": "rejected"}
        )

        def _no_db(*a, **k):
            raise RuntimeError("stop-before-db")

        monkeypatch.setattr(ir, "get_lexi_connection", _no_db)
        reached = False
        try:
            ir.retry_scheduling_with_guidance(1, "redo in my voice")
        except RuntimeError as exc:
            reached = str(exc) == "stop-before-db"
        assert reached, "rejected must pass the status gate"

    def test_already_sent_status_still_rejected(self, monkeypatch):
        from app.agents import inbound_reply as ir

        monkeypatch.setattr(
            ir, "_fetch_proposal_bundle", lambda pid: {"status": "offer_sent"}
        )
        out = ir.retry_scheduling_with_guidance(1, "anything")
        assert out["ok"] is False
        assert "not awaiting" in out["error"]


class TestGuidanceOverridesPolicy:
    """Live I-2 defect chain: Kory approved a lunch exception in Teams and the
    validator still stripped every lunch slot — guidance reached the draft
    prompt but never the preferences the engine enforces."""

    def test_lunch_approved_phrasing_matches(self):
        from app.scheduling.preferences import _LUNCH_YES

        for phrase in (
            "Lunch approved for this one — offer free lunch times.",
            "Lunch is fine for this one.",
            "I'm fine with lunch here.",
            "Make a lunch exception.",
        ):
            assert _LUNCH_YES.search(phrase), phrase

    def test_negative_still_wins(self):
        from app.scheduling.preferences import load_scheduling_preferences

        prefs = load_scheduling_preferences(guidance="No lunch meetings please.")
        assert prefs.lunch_allowed is False

    def test_guidance_flips_lunch_for_this_run(self):
        from app.scheduling.preferences import load_scheduling_preferences

        assert load_scheduling_preferences().lunch_allowed is False
        prefs = load_scheduling_preferences(
            guidance="Lunch approved for this one — offer free lunch times the week of Aug 17-21."
        )
        assert prefs.lunch_allowed is True

    def test_plan_guidance_reaches_engine_preferences(self):
        from unittest.mock import patch

        from app.scheduling import slot_engine as se
        from app.scheduling.scheduling_plan import SchedulingPlan

        captured = {}
        real_loader = se.load_scheduling_preferences

        def spy(guidance=""):
            captured["guidance"] = guidance
            return real_loader(guidance=guidance)

        plan = SchedulingPlan(source="open_horizon", kory_guidance="Lunch approved for this one.")
        with patch.object(se, "load_scheduling_preferences", side_effect=spy):
            se.find_valid_slots(
                {"status": "available", "busy_events": [], "horizon_days": 3},
                intent="lunch_request",
                subject="lunch",
                body="lunch sometime?",
                plan=plan,
            )
        assert captured["guidance"] == "Lunch approved for this one."

    def test_policy_reason_clears_when_exception_granted(self):
        from app.scheduling.hermes_compose import _policy_block_reason

        assert _policy_block_reason("lunch") != ""
        assert _policy_block_reason("lunch", guidance="Lunch approved for this one.") == ""

    def test_fallback_plans_carry_guidance(self):
        from app.scheduling.scheduling_plan import SchedulingPlan
        from app.scheduling.scheduling_window import SchedulingWindow
        from app.scheduling.window_fallback import _plan_without_window, _shift_plan_window
        from datetime import date

        plan = SchedulingPlan(
            window=SchedulingWindow(
                start=date(2026, 8, 17), end=date(2026, 8, 23), source="body", label="w"
            ),
            kory_guidance="Lunch approved.",
        )
        assert _shift_plan_window(plan, week_offset=1).kory_guidance == "Lunch approved."
        assert _plan_without_window(plan).kory_guidance == "Lunch approved."


class TestLunchMenuAndGuidedMinimum:
    """Live C-3/I-2: approved lunches were unschedulable because the candidate
    menu skips noon, and the 2-slot minimum re-escalated a search Kory had
    already directed."""

    def test_lunch_intent_gets_noon_candidates(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.scheduling import slot_engine as se

        day = datetime(2026, 8, 21, tzinfo=ZoneInfo("America/Denver"))
        starts = se._candidate_start_times(
            day, "lunch", "in_person", east_coast=False, urgent=False
        )
        times = {s.strftime("%H:%M") for s in starts}
        assert "12:00" in times and "12:30" in times

    def test_guided_search_accepts_single_slot(self):
        from unittest.mock import patch

        from app.scheduling import slot_engine as se
        from app.scheduling.scheduling_plan import SchedulingPlan
        from app.scheduling.scheduling_window import SchedulingWindow
        from datetime import date

        plan = SchedulingPlan(
            window=SchedulingWindow(
                start=date(2026, 8, 17), end=date(2026, 8, 21), source="llm", label="guided"
            ),
            kory_guidance="Lunch approved for this one.",
        )
        one_slot = se.SlotProposal(
            intent="lunch",
            meeting_format="in_person",
            slots=[{"start": "2026-08-21T12:00:00-06:00", "end": "2026-08-21T13:00:00-06:00"}],
        )
        with patch.object(se, "find_valid_slots", return_value=one_slot) as fv:
            result = se.propose_meeting_slots(
                {"status": "available", "busy_events": []},
                intent="lunch",
                subject="s",
                body="b",
                plan=plan,
            )
        assert fv.call_count == 1, "must NOT walk the ladder past Kory's directed week"
        assert len(result.slots) == 1
        assert result.diagnostics["status"] == "ok"

    def test_undirected_search_still_requires_two(self):
        from unittest.mock import patch

        from app.scheduling import slot_engine as se
        from app.scheduling.scheduling_plan import SchedulingPlan
        from app.scheduling.scheduling_window import SchedulingWindow
        from datetime import date

        plan = SchedulingPlan(
            window=SchedulingWindow(
                start=date(2026, 8, 17), end=date(2026, 8, 21), source="body", label="w"
            ),
        )
        one_slot = se.SlotProposal(intent="lunch", meeting_format="in_person",
            slots=[{"start": "2026-08-21T12:00:00-06:00", "end": "2026-08-21T13:00:00-06:00"}])
        with patch.object(se, "find_valid_slots", return_value=one_slot) as fv:
            se.propose_meeting_slots(
                {"status": "available", "busy_events": []},
                intent="lunch", subject="s", body="b", plan=plan,
            )
        assert fv.call_count > 1, "without guidance the ladder must still search wider"


class TestGateAcceptsGuidedSingleSlot:
    def test_single_slot_passes_with_guidance(self):
        from app.scheduling.pre_approval_gate import verify_before_kory_approval
        from app.scheduling.scheduling_plan import SchedulingPlan

        plan = SchedulingPlan(kory_guidance="Lunch approved for this one.")
        report = verify_before_kory_approval(
            slots=[{"start": "2026-08-21T11:30:00-06:00", "end": "2026-08-21T12:30:00-06:00"}],
            calendar_context={"status": "available", "busy_events": []},
            plan=plan,
            intent="lunch_request",
            subject="lunch",
            body="lunch sometime? Kory approved.",
        )
        assert not any("need at least" in c for c in report.checks)

    def test_single_slot_still_blocked_without_guidance(self):
        from app.scheduling.pre_approval_gate import verify_before_kory_approval

        report = verify_before_kory_approval(
            slots=[{"start": "2026-08-21T11:30:00-06:00", "end": "2026-08-21T12:30:00-06:00"}],
            calendar_context={"status": "available", "busy_events": []},
            plan=None,
            intent="referral_or_intro",
            subject="s",
            body="b",
        )
        assert report.ok is False
        assert any("need at least 2" in c for c in report.checks)


def test_gate_rule_check_honors_guidance_end_to_end():
    """The gate's own validator pass was the sixth copy of the lunch rule."""
    from app.scheduling.pre_approval_gate import verify_before_kory_approval
    from app.scheduling.scheduling_plan import SchedulingPlan

    plan = SchedulingPlan(kory_guidance="Lunch approved for this one.")
    report = verify_before_kory_approval(
        slots=[{"start": "2026-08-21T12:00:00-06:00", "end": "2026-08-21T13:00:00-06:00"}],
        calendar_context={"status": "available", "busy_events": []},
        plan=plan,
        intent="lunch_request",
        subject="lunch",
        body="lunch sometime?",
    )
    assert not any("exception-only" in c for c in report.checks), report.checks


class TestGuidanceSlotMinimumClassifier:
    """Live defect 2026-08-05: 'redo the draft in my voice' — pure style
    guidance — silently dropped the 2-slot minimum to 1 and Kory got a
    one-slot offer when a second free slot existed."""

    def test_style_guidance_keeps_the_minimum(self):
        from app.scheduling.preferences import guidance_relaxes_slot_minimum

        assert not guidance_relaxes_slot_minimum("redo the draft in my voice")
        assert not guidance_relaxes_slot_minimum("redo the draft in Kory's voice")
        assert not guidance_relaxes_slot_minimum("make it warmer and shorter")
        assert not guidance_relaxes_slot_minimum("")

    def test_constraining_guidance_relaxes_the_minimum(self):
        from app.scheduling.preferences import guidance_relaxes_slot_minimum

        assert guidance_relaxes_slot_minimum("Lunch approved for this one.")
        assert guidance_relaxes_slot_minimum("try Friday")
        assert guidance_relaxes_slot_minimum("offer next week instead")
        assert guidance_relaxes_slot_minimum("only mornings")
        assert guidance_relaxes_slot_minimum("9:30 works")
        assert guidance_relaxes_slot_minimum("make an exception here")

    def test_style_guidance_still_requires_two_slots_in_gate(self):
        from datetime import date

        from app.scheduling.pre_approval_gate import verify_before_kory_approval
        from app.scheduling.scheduling_plan import SchedulingPlan, SchedulingWindow

        plan = SchedulingPlan(
            window=SchedulingWindow(
                start=date(2026, 8, 24), end=date(2026, 8, 28), source="llm", label="guided"
            ),
            kory_guidance="redo the draft in my voice",
        )
        report = verify_before_kory_approval(
            slots=[
                {"start": "2026-08-26T09:30:00-06:00", "end": "2026-08-26T10:30:00-06:00"}
            ],
            intent="coffee",
            subject="coffee",
            body="coffee",
            calendar_context={"status": "available", "busy_events": []},
            plan=plan,
        )
        assert report.ok is False
        assert any("at least 2" in c for c in report.checks)
