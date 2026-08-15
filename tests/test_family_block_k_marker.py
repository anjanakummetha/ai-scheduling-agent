"""Kory's family-calendar rule (2026-08-11, kory_memory: family_calendar_blocks).

A family event only blocks him when the title carries a standalone "K" or
"Kory" — he always marks the ones he attends. Titles below are real ones from
the live Master calendar (read 2026-08-15). Ambiguous events must keep
blocking: only explicit family-signal titles qualify for the free rule.
"""

from __future__ import annotations

from app.scheduling.calendar_intelligence import (
    EventBlockingClass,
    classify_event,
)


def _event(subject: str, calendar: str = "Kory Master Calendar (ALL)") -> dict:
    return {
        "subject": subject,
        "calendar_name": calendar,
        "start": {"dateTime": "2026-08-20T15:30:00", "timeZone": "America/Denver"},
        "end": {"dateTime": "2026-08-20T16:30:00", "timeZone": "America/Denver"},
    }


def test_family_event_with_k_marker_blocks():
    out = classify_event(_event("Back to School Night ( B & K) (copy)"))
    assert out.blocks_kory is True
    assert out.blocking_class == EventBlockingClass.PERSONAL_KORY_BLOCKING


def test_family_event_b_plus_k_blocks():
    out = classify_event(_event("B plus K with liz noon mt  (copy)"))
    assert out.blocks_kory is True


def test_family_event_without_k_is_free():
    out = classify_event(_event("B @ Electing Women Social (copy)"))
    assert out.blocks_kory is False
    assert out.blocking_class == EventBlockingClass.FAMILY_NON_ATTENDING


def test_nanny_block_is_free():
    out = classify_event(_event("Nanny Erica 4 - 9pm (copy)"))
    assert out.blocks_kory is False


def test_generic_family_block_without_k_is_free():
    out = classify_event(_event("Family — school fundraiser"))
    assert out.blocks_kory is False


def test_generic_family_block_with_kory_blocks():
    out = classify_event(_event("Family dinner — Kory joining"))
    assert out.blocks_kory is True


def test_ambiguous_master_event_still_blocks():
    # No family signal, no K marker — a medical appointment must never be
    # opened up by the family rule.
    out = classify_event(_event("Denver heart 1030 routine 2 year follow up (copy)"))
    assert out.blocks_kory is True


def test_work_signal_beats_family_wording():
    # "IFG family day" carries a work signal; the family rule must not free it.
    out = classify_event(_event("IFG family day"))
    assert out.blocks_kory is True


def test_family_calendar_source_without_k_is_free():
    event = _event("Bridget book club", calendar="Family Google")
    event["source"] = "family_calendar"
    out = classify_event(event)
    assert out.blocks_kory is False


def test_do_not_move_still_blocks_regardless():
    out = classify_event(_event("Bridget — DO NOT MOVE"))
    assert out.blocks_kory is True
    assert out.blocking_class == EventBlockingClass.FAMILY_DO_NOT_MOVE


def test_kid_only_camp_still_free():
    out = classify_event(_event("Maclain Riding Lesson (copy)"))
    assert out.blocks_kory is False
