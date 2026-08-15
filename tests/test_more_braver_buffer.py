"""Kory's rule (2026-08-11, kory_memory: more_braver_buffer): nothing may start
within 30 minutes of a "More Braver" meeting ending."""

from __future__ import annotations

from app.rules.validators import validate_proposal_slots

_BUSY = [
    {
        "subject": "More Braver (copy)",
        "start": {"dateTime": "2026-08-20T10:00:00-06:00"},
        "end": {"dateTime": "2026-08-20T14:00:00-06:00"},
    }
]


def _slot(start: str, end: str) -> dict[str, str]:
    return {"start": start, "end": end}


def test_slot_inside_buffer_rejected():
    check = validate_proposal_slots(
        [_slot("2026-08-20T14:15:00-06:00", "2026-08-20T14:45:00-06:00")],
        intent="virtual_30",
        busy_events=_BUSY,
    )
    assert not check.valid
    assert any("More Braver" in v and "buffer" in v for v in check.violations)


def test_slot_exactly_at_event_end_rejected():
    check = validate_proposal_slots(
        [_slot("2026-08-20T14:00:00-06:00", "2026-08-20T14:30:00-06:00")],
        intent="virtual_30",
        busy_events=_BUSY,
    )
    assert not check.valid


def test_slot_after_buffer_allowed():
    check = validate_proposal_slots(
        [_slot("2026-08-20T14:30:00-06:00", "2026-08-20T15:00:00-06:00")],
        intent="virtual_30",
        busy_events=_BUSY,
    )
    assert check.valid, check.violations


def test_unrelated_meeting_no_buffer():
    busy = [
        {
            "subject": "WOB (copy)",
            "start": {"dateTime": "2026-08-20T10:00:00-06:00"},
            "end": {"dateTime": "2026-08-20T14:00:00-06:00"},
        }
    ]
    check = validate_proposal_slots(
        [_slot("2026-08-20T14:05:00-06:00", "2026-08-20T14:35:00-06:00")],
        intent="virtual_30",
        busy_events=busy,
    )
    assert check.valid, check.violations
