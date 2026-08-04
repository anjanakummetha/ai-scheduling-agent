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
