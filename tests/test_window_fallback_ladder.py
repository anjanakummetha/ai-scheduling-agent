"""The widening ladder has to actually widen.

When the requested week yields too few slots, propose_meeting_slots retries with
+1w/+2w/+3w and finally with no window at all. That last step strips the window
from the plan — but find_valid_slots re-infers a window from the email text
whenever the plan carries none, so the fallback rebuilt the very constraint it
was escaping and the ladder became a no-op.

Coffee is where this bites: Kory has roughly one coffee slot a week, so a
single-week coffee request can essentially never reach the two-slot minimum.
"""

from app.scheduling.scheduling_plan import SchedulingPlan
from app.scheduling.window_fallback import _plan_without_window
from app.scheduling.scheduling_window import SchedulingWindow

from datetime import date


def _plan_with_window() -> SchedulingPlan:
    return SchedulingPlan(
        task_type="offer_times",
        window=SchedulingWindow(
            start=date(2026, 8, 17),
            end=date(2026, 8, 23),
            source="body",
            label="week of August 17",
        ),
        source="body",
    )


def test_plan_without_window_marks_itself_open_horizon() -> None:
    stripped = _plan_without_window(_plan_with_window())
    assert stripped is not None
    assert stripped.window is None
    # The marker is what stops find_valid_slots re-deriving the window.
    assert stripped.source == "open_horizon"


def test_plan_without_window_keeps_the_rest_of_the_plan() -> None:
    plan = _plan_with_window()
    stripped = _plan_without_window(plan)
    assert stripped.task_type == plan.task_type
    assert stripped.duration_minutes == plan.duration_minutes
    assert stripped.meeting_format == plan.meeting_format


def test_open_horizon_plan_suppresses_window_reinference() -> None:
    """find_valid_slots must not re-infer a window for an open_horizon plan."""
    import inspect

    from app.scheduling import slot_engine

    source = inspect.getsource(slot_engine.find_valid_slots)
    assert 'plan.source == "open_horizon"' in source, (
        "the open_horizon guard is what makes the ladder able to widen"
    )


def test_plan_without_window_handles_none() -> None:
    assert _plan_without_window(None) is None
