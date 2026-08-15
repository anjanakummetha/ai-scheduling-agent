"""Keep an edited draft's offered times, the proposal's slots, and the live
calendar in agreement.

Live defect (Kory's test, 2026-08-11, proposal 9187): the model hand-edited a
draft via lexi_update_proposal_draft to offer times the engine never produced.
Nothing validated them — one was already booked (the Alejandra Harvey coffee) —
and nothing synced proposals.proposed_slots, so the send placed holds for the
engine's OLD slots while the email offered different ones. Kory looked for
holds at the offered times and found nothing.

Two choke points close both halves:
  * update_proposal_draft calls verify_draft_slots and refuses an edit whose
    times are busy or rule-violating, then writes the validated times back to
    proposed_slots — the draft body and the slots can no longer diverge.
  * the approve/send path calls assert_draft_matches_slots as a final gate, so
    a divergent draft that slipped past (older proposal, direct DB edit) is
    refused at send rather than emailed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.scheduling.busy_intervals import (
    intervals_overlap,
    local_dt,
    parse_event_datetime,
    parse_iso_datetime,
)

logger = logging.getLogger(__name__)

_DEFAULT_DURATION_MINUTES = 30


def _fmt(dt: datetime) -> str:
    local = local_dt(dt)
    return local.strftime("%A, %B %-d at %-I:%M %p MT")


@dataclass
class DraftSlotCheck:
    ok: bool
    slots: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def conflicting_event_subject(
    slot: dict[str, str], busy_events: list[dict[str, Any]]
) -> str | None:
    """Subject of the first busy event overlapping the slot, for error text."""
    start = parse_iso_datetime(str(slot.get("start") or ""))
    end = parse_iso_datetime(str(slot.get("end") or ""))
    if not start or not end:
        return None
    for event in busy_events:
        ev_start = parse_event_datetime(event.get("start"))
        ev_end = parse_event_datetime(event.get("end"))
        if not ev_start or not ev_end:
            continue
        if intervals_overlap(start, end, ev_start, ev_end):
            return str(event.get("subject") or "a calendar event").strip()
    return None


def extract_offer_times_from_draft(
    body: str,
    *,
    reference: datetime | None = None,
    duration_minutes: int = _DEFAULT_DURATION_MINUTES,
) -> list[dict[str, str]]:
    """Times a draft offers, as slots. Reuses the inbound prose parser; a
    candidate without an explicit end gets the meeting duration."""
    from app.scheduling.inbound_availability import extract_inbound_time_candidates

    slots: list[dict[str, str]] = []
    for cand in extract_inbound_time_candidates(body or "", reference=reference):
        start = parse_iso_datetime(str(cand.get("start") or ""))
        if not start:
            continue
        end = parse_iso_datetime(str(cand.get("end") or ""))
        if not end or end <= start:
            end = start + timedelta(minutes=duration_minutes)
        slots.append({"start": start.isoformat(), "end": end.isoformat()})
    return slots


def verify_draft_slots(
    *,
    draft_body: str,
    intent: str | None,
    subject: str = "",
    thread_body: str = "",
    existing_slots: list[dict[str, str]] | None = None,
    calendar_context: dict[str, Any] | None = None,
    reference: datetime | None = None,
) -> DraftSlotCheck:
    """Validate every time the edited draft offers against the live calendar
    and Kory's rules. Returns the validated slots to store as proposed_slots.

    No times found in the draft -> ok, slots unchanged (a P.S.-style edit)."""
    from app.rules.validators import validate_proposal_slots
    from app.scheduling.busy_intervals import slot_conflicts_busy
    from app.scheduling.meeting_type import resolve_meeting_type

    meeting = resolve_meeting_type(intent=intent, subject=subject, body=thread_body)
    parsed = extract_offer_times_from_draft(
        draft_body,
        reference=reference,
        duration_minutes=meeting.duration_minutes,
    )
    if not parsed:
        return DraftSlotCheck(
            ok=True,
            slots=list(existing_slots or []),
            warnings=[
                "No meeting times were detected in the edited draft; the staged "
                "slots are unchanged."
            ],
        )

    if calendar_context is None:
        from app.scheduling.calendar_context import load_scheduling_calendar_context

        calendar_context = load_scheduling_calendar_context(
            subject=subject, body=draft_body
        )

    check = DraftSlotCheck(ok=True, slots=parsed)
    busy = list((calendar_context or {}).get("busy_events") or [])
    if (calendar_context or {}).get("status") != "available":
        # Reading the calendar is the safety step — never validate blind.
        check.ok = False
        check.conflicts.append(
            "Could not read Kory's calendar to verify the edited times — "
            "nothing was changed. Try again once the calendar is reachable."
        )
        return check

    from datetime import timezone

    now = reference or datetime.now(timezone.utc)
    for slot in parsed:
        start = parse_iso_datetime(slot["start"])
        label = _fmt(start) if start else str(slot.get("start"))
        if start and start <= now:
            check.ok = False
            check.conflicts.append(f"{label} is in the past.")
            continue
        if slot_conflicts_busy(slot, busy, reserve_minutes=meeting.calendar_block_minutes):
            clash = conflicting_event_subject(slot, busy)
            check.ok = False
            check.conflicts.append(
                f"{label} is already booked"
                + (f' — overlaps "{clash}"' if clash else "")
                + "."
            )

    rules = validate_proposal_slots(
        parsed,
        intent=intent,
        busy_events=busy,
    )
    if not rules.valid:
        check.ok = False
        check.conflicts.extend(rules.violations)
    check.warnings.extend(rules.warnings)
    return check


def draft_matches_slots(
    *,
    draft_body: str,
    proposed_slots: list[dict[str, str]],
    reference: datetime | None = None,
) -> tuple[bool, str]:
    """Final send gate: the times the draft offers must be exactly the staged
    slots (which is what holds are placed for). Empty parse passes — a draft
    with no parseable times can't contradict the slots."""
    parsed = extract_offer_times_from_draft(draft_body, reference=reference)
    if not parsed or not proposed_slots:
        return True, ""

    def _starts(slots: list[dict[str, str]]) -> set[datetime]:
        # parse_iso_datetime normalizes to aware UTC, so instants compare directly.
        out = set()
        for s in slots:
            dt = parse_iso_datetime(str(s.get("start") or ""))
            if dt:
                out.add(dt)
        return out

    draft_starts = _starts(parsed)
    slot_starts = _starts(proposed_slots)
    if draft_starts <= slot_starts:
        return True, ""
    extra = sorted(draft_starts - slot_starts)
    extras = ", ".join(_fmt(dt) for dt in extra)
    return False, (
        f"The draft offers times that are not in the staged slots ({extras}). "
        "Nothing was sent and no holds were touched. Run "
        "lexi_update_proposal_draft with the final draft first — it validates "
        "each time against the live calendar and re-stages the slots so the "
        "calendar holds match the email."
    )
