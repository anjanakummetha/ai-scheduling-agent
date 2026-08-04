"""Live C-1 finding: "next week" was answered with week-after-next slots and
proposals.scheduling_note stayed empty — the gate computed the expansion
warning and _build_schedule dropped it, so neither the card nor the text-only
message could show Kory the caveat."""

from __future__ import annotations

from unittest.mock import patch

from app.agents import scheduler_agent
from app.scheduling.pre_approval_gate import PreApprovalReport
from app.scheduling.schedule_from_context import ScheduleFromContextResult


def _proposal():
    return scheduler_agent.PendingProposal(
        proposal_id=1,
        thread_id="t-1",
        intent_classification="referral_or_intro",
        priority_tier="medium",
        triage_confidence=0.9,
        justification=None,
        rule_reasoning=None,
        subject="[TEST] Intro call — LT-C1",
        sender="anjana@example.com",
        received_at=None,
        raw_body="Would love 30 minutes next week.",
        voice_mode="lexi",
    )


def _engine_result(*, warnings: list[str], window_expanded: bool) -> ScheduleFromContextResult:
    gate = PreApprovalReport(ok=True)
    gate.warnings = list(warnings)
    return ScheduleFromContextResult(
        ok=True,
        path="slot_engine",
        status="ok",
        slots=[
            {"start": "2026-08-17T16:00:00+00:00", "end": "2026-08-17T16:30:00+00:00"},
            {"start": "2026-08-19T15:00:00+00:00", "end": "2026-08-19T15:30:00+00:00"},
        ],
        plan=None,
        calendar_context={},
        meeting_format="virtual",
        gate=gate,
        diagnostics={
            "window_expanded": window_expanded,
            "original_window": "week of August 10",
            "expanded_window": "week of August 17",
        },
    )


def _build(result: ScheduleFromContextResult):
    with (
        patch(
            "app.scheduling.schedule_from_context.schedule_from_context",
            return_value=result,
        ),
        patch(
            "app.scheduling.reply_composer.compose_scheduling_reply",
            return_value=("Hi Anjana,\n\nTimes below.\n\nThank you,\nLexi Knightly", "test"),
        ),
    ):
        return scheduler_agent._build_schedule(_proposal(), calendar_context={})


def test_gate_warnings_become_the_scheduling_note():
    schedule = _build(
        _engine_result(
            warnings=[
                "no availability for week of August 10 — offering week of August 17 instead"
            ],
            window_expanded=True,
        )
    )
    assert "week of August 10" in schedule.scheduling_note
    assert "week of August 17" in schedule.scheduling_note


def test_expansion_without_gate_warning_still_produces_a_note():
    schedule = _build(_engine_result(warnings=[], window_expanded=True))
    assert "no availability for week of August 10" in schedule.scheduling_note
    assert "offering week of August 17 instead" in schedule.scheduling_note


def test_clean_match_leaves_note_empty():
    result = _engine_result(warnings=[], window_expanded=False)
    result.diagnostics = {}
    schedule = _build(result)
    assert schedule.scheduling_note == ""


def test_travel_shift_produces_a_note(monkeypatch):
    """Live C-4: the travel shift replaced the sender's window BEFORE the
    engine ran, so the gate honestly saw slots in-window and no expansion
    warning ever fired — offers landed a week late with no caveat."""
    from datetime import date

    from app.scheduling import schedule_from_context as sfc
    from app.scheduling.scheduling_plan import SchedulingPlan
    from app.scheduling.scheduling_window import SchedulingWindow

    asked = SchedulingWindow(
        start=date(2026, 8, 11), end=date(2026, 8, 12), source="llm",
        label="Tuesday afternoon or Wednesday next week",
    )
    shifted = SchedulingWindow(
        start=date(2026, 8, 17), end=date(2026, 8, 23), source="travel_shift",
        label="week of August 17 (after travel)",
    )

    monkeypatch.setattr(
        sfc, "maybe_shift_plan_window",
        lambda plan, busy: SchedulingPlan(
            task_type=plan.task_type, window=shifted, source=plan.source,
            duration_minutes=plan.duration_minutes,
        ),
    )

    captured = {}
    real_gate = sfc.verify_before_kory_approval

    def spy_gate(**kwargs):
        captured.update(
            window_expanded=kwargs.get("window_expanded"),
            original=kwargs.get("original_window_label"),
            expanded=kwargs.get("expanded_window_label"),
        )
        return real_gate(**kwargs)

    monkeypatch.setattr(sfc, "verify_before_kory_approval", spy_gate)

    class _Engine:
        slots = [
            {"start": "2026-08-18T19:00:00+00:00", "end": "2026-08-18T19:30:00+00:00"},
            {"start": "2026-08-19T19:00:00+00:00", "end": "2026-08-19T19:30:00+00:00"},
        ]
        meeting_format = "virtual"
        diagnostics = {"scheduling_window": {"label": "week of August 17 (after travel)"}}

    monkeypatch.setattr(sfc, "propose_meeting_slots", lambda *a, **k: _Engine())
    monkeypatch.setattr(
        sfc, "build_scheduling_plan",
        lambda **k: SchedulingPlan(task_type="offer_times", window=asked),
    )

    result = sfc.schedule_from_context(
        subject="[TEST] Catching up",
        body="Tuesday afternoon or Wednesday next week?",
        intent="referral_or_intro",
        calendar_context={"status": "available", "busy_events": [], "horizon_days": 30},
        use_llm_plan=False,
        format_slots=False,
        try_inbound_availability=False,
    )
    assert captured["window_expanded"] is True
    assert "Tuesday afternoon or Wednesday next week" in captured["original"]
    assert "after travel" in captured["expanded"]
    note = result.scheduling_note()
    assert "Tuesday afternoon or Wednesday next week" in note
    assert "after travel" in note
