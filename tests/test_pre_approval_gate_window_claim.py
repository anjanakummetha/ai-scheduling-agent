"""The gate must not claim it matched a window it never checked.

The old summary read "...rules pass, and match requested window" whenever there
were no warnings — unconditionally, with no window in play. That false assurance
is why out-of-window offers survived earlier test runs.
"""

from datetime import date

from app.scheduling.pre_approval_gate import PreApprovalReport
from app.scheduling.scheduling_window import SchedulingWindow

WINDOW = SchedulingWindow(
    start=date(2026, 8, 10), end=date(2026, 8, 14), source="body", label="August 10–14"
)


def test_summary_claims_the_window_only_when_it_was_verified() -> None:
    report = PreApprovalReport(ok=True, window_verified=True, window_label=WINDOW.label)
    summary = report.summary()
    assert "requested window" in summary
    assert "August 10–14" in summary


def test_summary_does_not_claim_a_window_when_none_was_requested() -> None:
    report = PreApprovalReport(ok=True)
    summary = report.summary()
    assert "match requested window" not in summary
    assert "No specific window was requested" in summary


def test_out_of_window_slots_block_the_gate() -> None:
    from app.scheduling.pre_approval_gate import verify_before_kory_approval

    # Two clean 60-minute coffee slots, but three weeks past the requested window.
    slots = [
        {"start": "2026-09-02T09:30:00-06:00", "end": "2026-09-02T10:30:00-06:00"},
        {"start": "2026-09-03T09:30:00-06:00", "end": "2026-09-03T10:30:00-06:00"},
    ]
    report = verify_before_kory_approval(
        slots=slots,
        calendar_context={"status": "available", "busy_events": []},
        intent="coffee",
        subject="Coffee",
        body="Can we do coffee August 10 to August 14?",
        window=WINDOW,
    )
    assert not report.ok
    assert any("outside requested window" in check for check in report.checks)
    assert not report.window_verified


def test_expanded_window_is_surfaced_not_silently_accepted() -> None:
    """The engine walks the window forward when a week is too full.

    That is often the useful answer, but it deviates from what the sender asked
    for, so it has to reach Kory's card rather than pass as a clean match.
    """
    from app.scheduling.pre_approval_gate import verify_before_kory_approval

    slots = [
        {"start": "2026-08-18T08:30:00-06:00", "end": "2026-08-18T09:30:00-06:00"},
        {"start": "2026-08-26T09:30:00-06:00", "end": "2026-08-26T10:30:00-06:00"},
    ]
    report = verify_before_kory_approval(
        slots=slots,
        calendar_context={"status": "available", "busy_events": []},
        intent="coffee",
        subject="Coffee",
        body="Coffee the week of the 10th?",
        window=WINDOW,
        window_expanded=True,
        original_window_label="week of August 10",
        expanded_window_label="week of August 17",
    )
    # Still offerable — Kory has ~1 coffee slot a week, so blocking would make
    # coffee scheduling unusable. But the deviation must be stated.
    assert report.ok
    assert not report.window_verified
    assert any("no availability for week of August 10" in w for w in report.warnings)
    # The offering label names the ACTUAL slot dates, not the ladder rung that
    # was searched (which can disagree with where the slots landed).
    assert "offering August 18 and August 26 instead" in " ".join(report.warnings)
    assert "match requested window" not in report.summary()
    assert "week of August 10" in report.rules_status_line()
