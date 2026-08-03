"""Kory-facing messages when the requested scheduling window has no slots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduling.scheduling_plan import SchedulingPlan
from app.scheduling.scheduling_window import SchedulingWindow
from app.scheduling.slot_engine import MIN_SLOT_OPTIONS, SlotProposal, propose_meeting_slots

MT = ZoneInfo(settings.scheduling_timezone)


@dataclass(frozen=True)
class WindowFallbackResult:
    proposal: SlotProposal
    shifted: bool
    requested_label: str | None
    actual_label: str | None
    kory_note: str
    suggested_guidance: str | None = None


def _week_label_for_slot(slot: dict[str, str]) -> str | None:
    try:
        start = datetime.fromisoformat(str(slot["start"]).replace("Z", "+00:00")).astimezone(MT)
    except (TypeError, ValueError, KeyError):
        return None
    monday = start.date().fromordinal(start.date().toordinal() - start.weekday())
    return f"week of {monday.strftime('%B')} {monday.day}"


def _label_from_slots(slots: list[dict[str, str]]) -> str | None:
    if not slots:
        return None
    return _week_label_for_slot(slots[0])


def _plan_without_window(plan: SchedulingPlan | None) -> SchedulingPlan | None:
    """Drop the window so the engine may search the open horizon.

    source="open_horizon" is load-bearing: find_valid_slots re-infers a window
    from the email text whenever the plan carries none, so without this marker
    the fallback re-derived the very window it was trying to widen past and the
    whole ladder became a no-op.
    """
    if plan is None:
        return None
    return SchedulingPlan(
        task_type=plan.task_type,
        window=None,
        duration_minutes=plan.duration_minutes,
        meeting_format=plan.meeting_format,
        urgency=plan.urgency,
        draft_context=plan.draft_context,
        source="open_horizon",
        raw=dict(plan.raw),
    )


def _shift_plan_window(plan: SchedulingPlan, *, week_offset: int) -> SchedulingPlan:
    from datetime import timedelta

    window = plan.window
    if not window:
        return plan
    delta = timedelta(days=7 * week_offset)
    new_start = window.start + delta
    shifted = SchedulingWindow(
        start=new_start,
        end=window.end + delta,
        source="fallback",
        # Name the actual week. "(+3w)" made Kory do the arithmetic to find out
        # a request for August was being answered with September, and this label
        # is what lands on his approval card.
        label=f"week of {new_start.strftime('%B')} {new_start.day}",
    )
    return SchedulingPlan(
        task_type=plan.task_type,
        window=shifted,
        duration_minutes=plan.duration_minutes,
        meeting_format=plan.meeting_format,
        urgency=plan.urgency,
        draft_context=plan.draft_context,
        source="window_fallback",
        raw=dict(plan.raw),
    )


def propose_with_window_fallback(
    calendar_context: dict[str, Any],
    *,
    intent: str | None,
    subject: str = "",
    body: str = "",
    meeting_format: str | None = None,
    plan: SchedulingPlan | None = None,
) -> WindowFallbackResult:
    """Search only the requested window. Wider search requires Kory approval first."""
    primary = propose_meeting_slots(
        calendar_context,
        intent=intent,
        subject=subject,
        body=body,
        meeting_format=meeting_format,
        plan=plan,
    )
    requested = (plan.window.label if plan and plan.window else None) or _infer_requested_label(
        body, subject
    )
    return WindowFallbackResult(
        proposal=primary,
        shifted=False,
        requested_label=requested,
        actual_label=_label_from_slots(primary.slots) if primary.slots else None,
        kory_note="",
        suggested_guidance=None,
    )


def _infer_requested_label(subject: str, body: str) -> str | None:
    from app.scheduling.scheduling_window import infer_scheduling_window

    window = infer_scheduling_window(subject=subject, body=body)
    return window.label if window else None


def _shift_note(requested: str | None, actual: str | None) -> str:
    if requested and actual:
        return (
            f"They asked for {requested}, but Kory's calendar is full then — "
            f"offering times the {actual} instead."
        )
    if actual:
        return f"Offering times the {actual}."
    return "Offering the next available times on Kory's calendar."


def _guidance_from_label(label: str | None) -> str | None:
    if not label:
        return None
    return f"offer the {label}"


def build_failure_kory_message(
    *,
    intent: str,
    requested_label: str | None,
) -> str:
    """Plain-language Teams message when no slots in the requested window — ask Kory first."""
    kind = {
        "coffee": "coffee",
        "referral_or_intro": "intro",
        "happy_hour": "happy hour",
        "dinner_request": "dinner",
        "dinner": "dinner",
        "new_client": "meeting",
        "podcast": "podcast pre-interview",
    }.get((intent or "").lower(), "meeting")

    window = requested_label or "that window"
    return (
        f"I couldn't find a {kind} slot for {window}. "
        "Should I try a different week?"
    )
