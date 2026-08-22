"""The confirm-time check must not count a hold's calendar mirror as a clash.

Live proposal 10563 (2026-08-22): `send invite` for the accepted slot was
refused because "HOLD: Intro call w/ Anjanakummetha (The Turn Podcast)
(copy)" occupied the window — Lexi's OWN hold for that very slot, seen
through the Master-calendar mirror. Own holds are excluded by event id, but
the mirror carries a different id, so the fail-closed named-calendar read
(B5) surfaced it and the core booking step refused. Fail-safe, and wrong:
whenever holds mirror to Master, every single booking would be blocked.

A HOLD-titled event at exactly the chosen window is protection, not a
commitment. A genuinely different event at that time must still refuse.
"""

from __future__ import annotations

from unittest.mock import patch

from app.agents.comms_agent import _confirm_time_conflict

SLOT = {"start": "2026-09-01T08:30:00-06:00", "end": "2026-09-01T09:30:00-06:00"}
PROPOSAL = {"id": 10563, "holds": [{"event_id": "own-hold-id"}]}


def _event(subject: str, start: str = SLOT["start"], end: str = SLOT["end"]):
    return {"subject": subject, "start": {"dateTime": start}, "end": {"dateTime": end}}


def _with_conflicts(events):
    return patch(
        "app.integrations.outlook_calendar.has_conflict",
        return_value=(bool(events), events, []),
    )


def test_the_holds_mirror_copy_does_not_block_the_invite():
    mirror = _event("HOLD: Intro call w/ Anjanakummetha (The Turn Podcast) (copy)")
    with _with_conflicts([mirror]):
        assert _confirm_time_conflict(PROPOSAL, SLOT) is None, (
            "the hold's own mirror was counted as a clash (live 10563)"
        )


def test_a_mirror_in_utc_spelling_still_matches():
    """Graph often returns Z/UTC datetimes; -06:00 vs Z is the same instant."""
    mirror = _event(
        "HOLD: Intro call w/ Anjanakummetha (copy)",
        start="2026-09-01T14:30:00Z",
        end="2026-09-01T15:30:00Z",
    )
    with _with_conflicts([mirror]):
        assert _confirm_time_conflict(PROPOSAL, SLOT) is None


def test_a_real_meeting_at_that_time_still_refuses():
    real = _event("Board prep — do not move")
    with _with_conflicts([real]):
        clash = _confirm_time_conflict(PROPOSAL, SLOT)
    assert clash is not None
    assert "Board prep" in str(clash.get("kory_message"))


def test_a_hold_at_a_different_time_still_refuses():
    """A HOLD elsewhere in the day overlapping oddly is NOT this slot's own
    protection — only an exact-window match is excludable."""
    other = _event(
        "HOLD: Coffee w/ someone else",
        start="2026-09-01T08:00:00-06:00",
        end="2026-09-01T09:00:00-06:00",
    )
    with _with_conflicts([other]):
        assert _confirm_time_conflict(PROPOSAL, SLOT) is not None
