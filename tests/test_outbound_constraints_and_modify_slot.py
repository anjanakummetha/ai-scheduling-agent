"""Two send-path fixes from the scheduling analysis.

1. Kory's constraints ("mornings her time", "next week") must reach the slot
   engine — the outbound path sent a canned body, so a Boston-morning request
   got noon-ET slots (live, proposal 7997).
2. modify_and_approve sent end==start for a novel time; the resolver accepted
   the zero-length slot and would book a 0-minute meeting.
"""

from __future__ import annotations

import app.agents.outbound_agent as oa
from app.agents.comms_agent import _resolve_selected_slot, _slot_with_derived_end

PROPOSAL = {
    "id": 1,
    "proposed_slots": [
        {"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"},
        {"start": "2026-08-17T10:00:00-06:00", "end": "2026-08-17T10:30:00-06:00"},
    ],
    "holds": [],
}


def test_constraints_reach_the_slot_engine(monkeypatch):
    seen = {}

    class FakeEngine:
        slots = [
            {"start": "2026-08-11T07:00:00-06:00", "end": "2026-08-11T07:30:00-06:00"},
            {"start": "2026-08-12T07:00:00-06:00", "end": "2026-08-12T07:30:00-06:00"},
        ]

    def fake_propose(ctx, *, intent, subject, body, **kw):
        seen["body"] = body
        return FakeEngine()

    monkeypatch.setattr("app.scheduling.slot_engine.propose_meeting_slots", fake_propose)
    monkeypatch.setattr(oa, "build_scheduling_reply", lambda **kw: "draft")
    monkeypatch.setattr(oa, "sender_first_name", lambda e: "Anjana")

    oa._build_outbound_schedule(
        recipient_email="anjanakummetha@gmail.com",
        subject="[TEST] intro call",
        meeting_intent="meeting",
        duration_minutes=30,
        authorized_by="kory",
        calendar_context={},
        constraints="mornings her time, she's in Boston, next week",
    )
    assert "mornings her time" in seen["body"]
    assert "Boston" in seen["body"]


def test_novel_time_inherits_offered_duration():
    out = _resolve_selected_slot(
        PROPOSAL, '{"start": "2026-08-11T15:00:00-06:00", "end": "2026-08-11T15:00:00-06:00"}'
    )
    assert out["start"] == "2026-08-11T15:00:00-06:00"
    assert out["end"] == "2026-08-11T15:30:00-06:00"  # 30 min inherited, not zero


def test_novel_time_without_end_also_derives():
    out = _resolve_selected_slot(PROPOSAL, '{"start": "2026-08-11T15:00:00-06:00"}')
    assert out["end"] == "2026-08-11T15:30:00-06:00"


def test_matching_offered_time_keeps_its_real_end():
    out = _resolve_selected_slot(
        PROPOSAL, '{"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:00:00-06:00"}'
    )
    assert out["end"] == "2026-08-10T10:30:00-06:00"


def test_valid_explicit_end_is_respected():
    out = _resolve_selected_slot(
        PROPOSAL, '{"start": "2026-08-11T15:00:00-06:00", "end": "2026-08-11T16:00:00-06:00"}'
    )
    assert out["end"] == "2026-08-11T16:00:00-06:00"


def test_no_offered_slots_falls_back_to_30_minutes():
    out = _slot_with_derived_end({"proposed_slots": []}, "2026-08-11T15:00:00-06:00")
    assert out["end"] == "2026-08-11T15:30:00-06:00"
