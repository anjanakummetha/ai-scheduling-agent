"""Verify slots + draft before Kory sees a Teams approval card."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.rules.validators import ValidationResult, validate_proposal_slots
from app.scheduling.busy_intervals import slot_conflicts_busy, slot_interval
from app.scheduling.meeting_type import (
    calendar_block_minutes_for_context,
    offer_duration_minutes_for_context,
    resolve_meeting_type,
)
from app.scheduling.scheduling_plan import SchedulingPlan
from app.scheduling.scheduling_window import slot_date_in_window
from app.scheduling.slot_engine import infer_meeting_format

MIN_SLOT_OPTIONS = 2


def offered_dates_label(slots: list[dict[str, Any]] | None) -> str:
    """Human label for the offered slot dates ("August 26" / "August 26 and
    September 2"), straight from the slots themselves — the only source that
    can't disagree with what Kory is shown."""
    from datetime import datetime

    days: list[str] = []
    for slot in slots or []:
        try:
            start = datetime.fromisoformat(str(slot["start"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        label = f"{start.strftime('%B')} {start.day}"
        if label not in days:
            days.append(label)
    if not days:
        return ""
    if len(days) == 1:
        return days[0]
    return ", ".join(days[:-1]) + f" and {days[-1]}"


@dataclass
class PreApprovalReport:
    ok: bool
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meeting_type_key: str = ""
    meeting_type_label: str = ""
    rules_passed: bool = False
    # Only true when a requested window was actually found AND every slot checked
    # against it. Without this the summary claimed "match requested window" even
    # when no window had been parsed — false assurance on every un-windowed offer.
    window_verified: bool = False
    window_label: str = ""

    def summary(self) -> str:
        if self.ok and not self.warnings:
            if self.window_verified:
                return (
                    "Calendar verified — slots clear conflicts, rules pass, and fall inside "
                    f"the requested window ({self.window_label})."
                )
            return (
                "Calendar verified — slots clear conflicts and rules pass. "
                "No specific window was requested, so these are the next available times."
            )
        parts = []
        if not self.ok:
            parts.append("BLOCKED: " + "; ".join(self.checks))
        if self.warnings:
            parts.append("Warnings: " + "; ".join(self.warnings))
        return " ".join(parts) or "ok"

    def rules_status_line(self) -> str:
        """User-facing Teams line — never show Composio calendar-visibility noise."""
        if not self.ok:
            return f"Rules: blocked — {'; '.join(self.checks[:2])}"
        visible = [
            w
            for w in self.warnings
            if "not visible via Composio" not in w
            and "configured calendars" not in w.lower()
        ]
        if visible:
            return f"Rules: pass (with warnings — {'; '.join(visible[:2])})"
        if self.rules_passed or self.ok:
            return "Rules: pass"
        return "Rules: not verified"


def _gate_preferences(plan: SchedulingPlan | None):
    from app.scheduling.preferences import load_scheduling_preferences

    guidance = getattr(plan, "kory_guidance", "") if plan is not None else ""
    return load_scheduling_preferences(guidance=guidance)


def verify_before_kory_approval(
    *,
    slots: list[dict[str, str]],
    calendar_context: dict[str, Any],
    plan: SchedulingPlan | None = None,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
    meeting_format: str | None = None,
    window_expanded: bool = False,
    window: Any = None,
    original_window_label: str = "",
    expanded_window_label: str = "",
) -> PreApprovalReport:
    """Fail closed unless calendar is readable and slots pass conflict + Kory rules."""
    busy = list(calendar_context.get("busy_events") or [])
    report = PreApprovalReport(ok=True)

    meeting_spec = resolve_meeting_type(
        intent=intent,
        subject=subject,
        body=body,
    )
    report.meeting_type_key = meeting_spec.type_key
    report.meeting_type_label = meeting_spec.label

    if calendar_context.get("status") != "available":
        report.ok = False
        detail = calendar_context.get("error") or calendar_context.get("source") or "unknown"
        report.checks.append(f"calendar unavailable ({detail})")
        return report

    # Missing named calendars are operational noise for Kory's Teams card —
    # keep them out of user-facing warnings (still available on calendar_context).
    _ = list(calendar_context.get("calendars_unavailable") or [])

    # A Kory-directed search ("lunch approved — offer that week") may honestly
    # yield one slot; blocking it here re-escalated the question he had just
    # answered (live I-2, third layer of the same 2-slot rule).
    from app.scheduling.preferences import guidance_relaxes_slot_minimum

    required = (
        1
        if (plan is not None and guidance_relaxes_slot_minimum(getattr(plan, "kory_guidance", "")))
        else MIN_SLOT_OPTIONS
    )
    if len(slots) < required:
        report.ok = False
        report.checks.append(f"need at least {required} slots (got {len(slots)})")
        return report

    fmt = meeting_format or infer_meeting_format(
        meeting_spec.type_key,
        subject=subject,
        body=body,
    )
    expected_block = offer_duration_minutes_for_context(
        intent=intent,
        subject=subject,
        body=body,
        plan_duration_minutes=(plan.duration_minutes if plan else None),
    )

    for index, slot in enumerate(slots, start=1):
        if slot_conflicts_busy(slot, busy):
            report.ok = False
            report.checks.append(f"slot {index} conflicts with Kory calendar")

        interval = slot_interval(slot)
        if interval:
            start, end = interval
            actual_minutes = int((end - start).total_seconds() // 60)
            if actual_minutes != expected_block:
                report.ok = False
                report.checks.append(
                    f"slot {index} block is {actual_minutes} min; expected {expected_block} min "
                    f"for {meeting_spec.label}"
                )

    # The window the engine actually used, which is often inferred from the email
    # rather than carried on the plan. Keying this off plan.window alone meant a
    # sender's stated timeframe was never enforced whenever the plan lacked one.
    effective_window = window or (plan.window if plan else None)
    if window_expanded:
        # The engine walked the window forward (+1w, +2w, +3w, then open horizon)
        # because the requested one had too few slots. That is often the useful
        # answer, but it is a deviation from what the sender asked for, so it
        # must reach Kory's card instead of passing as a clean match.
        requested = original_window_label or (effective_window.label if effective_window else "")
        # Name the dates actually offered — the ladder's rung label says which
        # window it SEARCHED, not where the surviving slots landed (live defect:
        # note said "week of August 18", slots were Aug 26 – Sep 8). A trailing
        # parenthetical reason on the label ("(after travel)") is disclosure,
        # not a date claim — keep it.
        offering = offered_dates_label(slots)
        if offering and expanded_window_label:
            import re as _re

            reason = _re.search(r"\(([^)]+)\)\s*$", expanded_window_label)
            if reason:
                offering = f"{offering} ({reason.group(1)})"
        offering = offering or expanded_window_label or ""
        report.window_verified = False
        if requested:
            report.window_label = requested
            # Labels like "in the next few weeks" carry their own preposition —
            # "no availability for in ..." is word salad.
            lead = (
                f"no availability {requested}"
                if requested.lower().startswith(("in ", "over ", "during "))
                else f"no availability for {requested}"
            )
            report.warnings.append(
                lead
                + (f" — offering {offering} instead" if offering else " — offering the next open times")
            )
    elif effective_window:
        report.window_label = effective_window.label
        outside = [
            index
            for index, slot in enumerate(slots, start=1)
            if not slot_date_in_window(slot, effective_window)
        ]
        for index in outside:
            report.ok = False
            report.checks.append(
                f"slot {index} outside requested window ({effective_window.label})"
            )
        report.window_verified = not outside

    rule_check = ValidationResult(valid=True)
    for slot in slots:
        check = validate_proposal_slots(
            [slot],
            intent=meeting_spec.type_key,
            meeting_format=fmt,
            urgent=bool(plan.urgency if plan else False),
            busy_events=busy,
            batch_slots=[slot],
            # Guidance-aware, or this re-check blocks the exception Kory
            # granted after the engine already honored it (live I-2 — this
            # was the sixth independent copy of the lunch rule).
            preferences=_gate_preferences(plan),
        )
        if not check.valid:
            rule_check.valid = False
        rule_check.rules_checked.extend(check.rules_checked)
        rule_check.warnings.extend(check.warnings)
        for violation in check.violations:
            if violation not in rule_check.violations:
                rule_check.violations.append(violation)
    report.rules_passed = rule_check.valid
    if not rule_check.valid:
        report.ok = False
        for violation in rule_check.violations[:4]:
            if violation not in report.checks:
                report.checks.append(violation)
    report.warnings.extend(rule_check.warnings)

    return report
