"""Regression tests for Run-1 live defects.

Composio's OUTLOOK_LIST_MESSAGES ignores the `filter` argument, so it returns
the newest messages in the folder regardless of conversation. Unrelated mail
then leaked into thread context and was mistaken for delegation.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.delegation import detect_delegation
from app.integrations.outlook_thread import filter_by_conversation
from app.scheduling.email_format import sender_first_name


def _msg(mid: str, conv: str | None) -> dict:
    payload: dict = {"id": mid, "subject": "s"}
    if conv is not None:
        payload["conversationId"] = conv
    return payload


def test_filter_drops_messages_from_other_conversations():
    messages = [
        _msg("a", "conv-1"),
        _msg("b", "conv-2"),
        _msg("c", "conv-1"),
        _msg("d", "conv-3"),
    ]
    kept = filter_by_conversation(messages, "conv-1")
    assert [m["id"] for m in kept] == ["a", "c"]


def test_filter_keeps_messages_missing_conversation_id():
    """Absent field is unverifiable, not a mismatch — dropping it breaks threads."""
    messages = [_msg("a", None), _msg("b", "other")]
    kept = filter_by_conversation(messages, "conv-1")
    assert [m["id"] for m in kept] == ["a"]


def test_filter_requires_a_conversation_id():
    assert filter_by_conversation([_msg("a", "conv-1")], "") == []


def test_unrelated_thread_text_does_not_trigger_delegation():
    """The live failure: another thread's 'Looping in Lexi' leaked into a cold
    outreach email and made Lexi draft meeting times for a stranger."""
    contaminated = (
        "I got your info from the YPO Construction Network directory.\n\n"
        "[Prior messages in this email chain]\n"
        "--- Earlier in thread ---\n"
        "Looping in Lexi to find us a time.\n"
    )
    leaked = detect_delegation(
        subject="YPO request - Construction Network",
        body=contaminated,
        sender="kory.mitchell@iconicfounders.com",
    )
    own_text = detect_delegation(
        subject="Intro",
        body="Looping in Lexi to find us a time.",
        sender="kory.mitchell@iconicfounders.com",
    )
    # Kory's own words still delegate; borrowed quoted text must not.
    assert own_text.is_delegation is True
    assert leaked.is_delegation is False, (
        "quoted text from another thread must not count as delegation"
    )


def test_run_together_mailbox_does_not_become_a_first_name():
    assert sender_first_name("anjanakummetha@gmail.com") == "there"
    assert sender_first_name("jsmith2018@gmail.com") == "there"
    # Normal addresses still resolve.
    assert sender_first_name("anjana.kummetha@gmail.com") == "Anjana"
    assert sender_first_name("kory@ifg.vc") == "Kory"


def test_offered_slots_are_chronological():
    """A live offer listed 11:00 AM above 7:00 AM because selection is by score."""
    from app.scheduling.slot_engine import find_valid_slots

    # Tuesday morning free except 08:30-10:00, so the engine's scoring prefers
    # the later slot first; the recipient should still read them in time order.
    busy = [
        {
            "subject": "Longevity appt",
            "start": {"dateTime": "2026-07-28T08:30:00", "timeZone": "America/Denver"},
            "end": {"dateTime": "2026-07-28T10:00:00", "timeZone": "America/Denver"},
            "blocking_class": "work_blocking",
        }
    ]
    res = find_valid_slots(
        {
            "status": "available",
            "busy_events": busy,
            "horizon_days": 14,
            "scheduling_timezone": "America/Denver",
        },
        intent="referral_or_intro",
        subject="Intro",
        body="30 minutes next week",
        reference_now=NOW,
    )
    starts = [s["start"] for s in res.slots]
    assert starts == sorted(starts), starts


NOW = datetime(2026, 7, 26, 13, 45, tzinfo=ZoneInfo("America/Denver"))  # Sunday


def _ctx(busy=None):
    return {
        "status": "available",
        "busy_events": busy or [],
        "horizon_days": 14,
        "scheduling_timezone": "America/Denver",
    }


def _blocked_day(day: int):
    return {
        "subject": "Blocked",
        "blocking_class": "work_blocking",
        "start": {"dateTime": f"2026-07-{day:02d}T06:00:00", "timeZone": "America/Denver"},
        "end": {"dateTime": f"2026-07-{day:02d}T20:00:00", "timeZone": "America/Denver"},
    }


def test_offer_spreads_across_days_when_calendar_allows():
    """All three options landing on one day means one bad day kills the offer."""
    from app.scheduling.slot_engine import find_valid_slots

    res = find_valid_slots(
        _ctx(),
        intent="referral_or_intro",
        subject="Intro",
        body="30 minutes next week",
        reference_now=NOW,
    )
    days = {s["start"][:10] for s in res.slots}
    assert len(res.slots) >= 2
    assert len(days) == len(res.slots), f"expected one slot per day, got {sorted(days)}"


def test_offer_still_fills_from_a_single_open_day():
    """Spreading is a preference, not a requirement — don't return one lonely slot."""
    from app.scheduling.slot_engine import find_valid_slots

    busy = [_blocked_day(d) for d in (27, 29, 30, 31)]
    res = find_valid_slots(
        _ctx(busy),
        intent="referral_or_intro",
        subject="Intro",
        body="30 minutes next week",
        reference_now=NOW,
    )
    assert len(res.slots) >= 2
    assert {s["start"][:10] for s in res.slots} == {"2026-07-28"}
